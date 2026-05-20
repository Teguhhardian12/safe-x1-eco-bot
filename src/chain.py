"""On-chain actions: transfer, swap, add-liquidity, deploy.

Safety contract:
- approve uses EXACT amounts and is followed by a revoke-to-0 tx
- add-liquidity computes amount0Min/amount1Min from the pool's current
  price (slot0) and configured slippage, never 0
- maxFeePerGas = 2 * baseFee + priorityFee (not baseFee + 1)
- nonce is fetched as `pending` count and refetched on `nonce too low`

The original bot violated all four of these.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Optional

from eth_account import Account
from eth_typing import HexStr
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.types import TxParams, TxReceipt

from . import constants as C
from .abi import (
    DEPLOY_ROUTER_ABI,
    ERC20_ABI,
    MINT_ROUTER_ABI,
    POOL_V3_ABI,
    SWAP_ROUTER_ABI,
)
from .printer import Printer


class ChainError(Exception):
    pass


def calc_paired_amount(
    pools: list[dict],
    have_token_symbol: str,
    have_amount_wei: int,
) -> int:
    """Compute the IDEAL paired token amount for add-liquidity.

    Unlike `calc_amount_out_min`, this does NOT subtract fee tier or slippage —
    in `mint()`, the pool just consumes tokens at the spot ratio (no swap fee).
    Subtracting fee/slippage here would underpay the paired side and trigger
    `amount1Min` revert if the on-chain price hasn't moved.
    """
    from decimal import Decimal, ROUND_DOWN

    if not pools:
        raise ChainError("no pools to compute paired amount from")
    pool = max(pools, key=lambda p: int(p.get("liquidity") or 0))

    sym0 = (pool.get("token0") or {}).get("symbol")
    sym1 = (pool.get("token1") or {}).get("symbol")
    sqrt_price = Decimal(pool["sqrtPrice"])

    price = (sqrt_price ** 2) / (Decimal(2) ** 192)
    have = Decimal(have_amount_wei)

    if have_token_symbol == sym0:
        paired = have * price
    elif have_token_symbol == sym1:
        paired = have / price
    else:
        raise ChainError(f"have_token {have_token_symbol!r} not in pool ({sym0}/{sym1})")

    return int(paired.to_integral_value(rounding=ROUND_DOWN))


def calc_amount_out_min(
    pools: list[dict],
    token_in_symbol: str,
    amount_in_wei: int,
    slippage_bps: int = C.DEFAULT_SLIPPAGE_BPS,
) -> int:
    """Compute minOut from pool sqrtPrice, fee tier, and slippage.

    Mirrors vonssy/X1-Ecochain-BOT bot.py L907-940. Picks the deepest pool
    by liquidity. Direction is inferred by matching token_in_symbol against
    pool.token0/token1.symbol — caller is responsible for using the same
    symbols the subgraph returns ("WX1T" / "USDT").
    """
    from decimal import Decimal, ROUND_DOWN

    if not pools:
        raise ChainError("no pools to compute amount_out_min from")
    pool = max(pools, key=lambda p: int(p.get("liquidity") or 0))

    sym0 = (pool.get("token0") or {}).get("symbol")
    sym1 = (pool.get("token1") or {}).get("symbol")
    sqrt_price = Decimal(pool["sqrtPrice"])
    fee_tier = Decimal(pool.get("feeTier") or C.DEFAULT_FEE_TIER)

    price = (sqrt_price ** 2) / (Decimal(2) ** 192)
    amount_in = Decimal(amount_in_wei)

    if token_in_symbol == sym0:
        amount_out = amount_in * price
    elif token_in_symbol == sym1:
        amount_out = amount_in / price
    else:
        raise ChainError(f"token_in {token_in_symbol!r} not in pool ({sym0}/{sym1})")

    fee_multiplier = Decimal(1) - (fee_tier / Decimal(1_000_000))
    amount_out *= fee_multiplier

    slippage_multiplier = Decimal(1) - (Decimal(slippage_bps) / Decimal(C.BPS_DENOMINATOR))
    amount_out *= slippage_multiplier

    return int(amount_out.to_integral_value(rounding=ROUND_DOWN))


@dataclass(frozen=True)
class GasPolicy:
    priority_fee_wei: int
    """Tip paid to the validator (priority fee per gas)."""

    @classmethod
    def from_gwei(cls, gwei: float) -> "GasPolicy":
        return cls(priority_fee_wei=int(gwei * 1e9))

    def fees(self, base_fee: int) -> tuple[int, int]:
        """Return (maxFeePerGas, maxPriorityFeePerGas).

        maxFee = 2 * baseFee + priority. Headroom of 2x baseFee absorbs
        moderate gas spikes between estimation and inclusion; the original
        bot used baseFee + 1, which fails the moment baseFee ticks up.
        """
        return (2 * base_fee + self.priority_fee_wei, self.priority_fee_wei)


class ChainContext:
    def __init__(
        self,
        w3: Web3,
        account: Account,
        chain_id: int,
        gas: GasPolicy,
        printer: Optional[Printer] = None,
    ) -> None:
        self.w3 = w3
        self.account = account
        self.address = account.address
        self.chain_id = chain_id
        self.gas = gas
        self.printer = printer or Printer()

    @classmethod
    def connect(
        cls,
        rpc_url: str,
        chain_id: int,
        private_key: str,
        priority_fee_gwei: float,
        printer: Optional[Printer] = None,
    ) -> "ChainContext":
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        # POA chains (X1 testnet uses Clique-like extraData)
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not w3.is_connected():
            raise ChainError(f"Cannot connect to {rpc_url}")
        account = Account.from_key(private_key)
        return cls(
            w3=w3,
            account=account,
            chain_id=chain_id,
            gas=GasPolicy.from_gwei(priority_fee_gwei),
            printer=printer,
        )

    # --- Helpers ---

    def _nonce(self) -> int:
        return self.w3.eth.get_transaction_count(self.address, "pending")

    def _base_fee(self) -> int:
        latest = self.w3.eth.get_block("latest")
        return latest.get("baseFeePerGas") or 0

    def _build_eip1559(self, gas_limit: int, to: Optional[str] = None, value: int = 0, data: bytes = b"") -> TxParams:
        max_fee, priority = self.gas.fees(self._base_fee())
        tx: TxParams = {
            "from": self.address,
            "nonce": self._nonce(),
            "chainId": self.chain_id,
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "value": value,
        }
        if to is not None:
            tx["to"] = Web3.to_checksum_address(to)
        if data:
            tx["data"] = data
        return tx

    def _send_and_wait(self, tx: TxParams, *, retry_on_nonce: bool = True) -> TxReceipt:
        try:
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except ValueError as e:
            msg = str(e).lower()
            if retry_on_nonce and ("nonce too low" in msg or "replacement transaction" in msg):
                self.printer.warn(f"nonce too low — refetching and retrying once: {e}")
                tx["nonce"] = self._nonce()
                return self._send_and_wait(tx, retry_on_nonce=False)
            raise ChainError(f"Send failed: {e}") from e

        self.printer.debug(f"tx sent: {tx_hash.hex()}")
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt["status"] != 1:
            raise ChainError(f"tx reverted: {tx_hash.hex()}")
        return receipt

    # --- ERC-20 helpers ---

    def _erc20(self, token: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)

    def balance_of(self, token: str, who: Optional[str] = None) -> int:
        return self._erc20(token).functions.balanceOf(who or self.address).call()

    def native_balance(self) -> int:
        return self.w3.eth.get_balance(self.address)

    def decimals(self, token: str) -> int:
        return self._erc20(token).functions.decimals().call()

    def allowance(self, token: str, spender: str) -> int:
        return self._erc20(token).functions.allowance(self.address, spender).call()

    def _approve(self, token: str, spender: str, amount: int, gas_limit: int = C.GAS_LIMIT_APPROVE) -> TxReceipt:
        contract = self._erc20(token)
        data = contract.encode_abi("approve", args=[Web3.to_checksum_address(spender), amount])
        tx = self._build_eip1559(gas_limit=gas_limit, to=token, data=data)
        return self._send_and_wait(tx)

    # --- Public actions ---

    def transfer_native(self, to: str, amount_wei: int) -> str:
        tx = self._build_eip1559(gas_limit=C.GAS_LIMIT_TRANSFER, to=to, value=amount_wei)
        receipt = self._send_and_wait(tx)
        return receipt["transactionHash"].hex()

    def transfer_erc20(self, token: str, to: str, amount: int) -> str:
        contract = self._erc20(token)
        data = contract.encode_abi("transfer", args=[Web3.to_checksum_address(to), amount])
        tx = self._build_eip1559(gas_limit=C.GAS_LIMIT_ERC20_TRANSFER, to=token, data=data)
        receipt = self._send_and_wait(tx)
        return receipt["transactionHash"].hex()

    def swap_exact_in_native(
        self,
        router: str,
        token_in: str,
        token_out: str,
        fee: int,
        amount_in: int,
        amount_out_min: int,
        deadline_seconds: int = 600,
    ) -> str:
        """Swap native X1T -> ERC20 via the router.

        X1's swap router auto-wraps native to WX1T when ``token_in`` is
        WX1T and ``msg.value == amount_in``. No approve needed because the
        wrapping happens inside the router from the supplied native value.
        """
        if amount_out_min <= 0:
            raise ChainError("amount_out_min must be > 0 — refuse 0-slippage swap")

        contract = self.w3.eth.contract(address=Web3.to_checksum_address(router), abi=SWAP_ROUTER_ABI)
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            self.address,
            int(time()) + deadline_seconds,
            amount_in,
            amount_out_min,
            0,
        )
        data = contract.encode_abi("exactInputSingle", args=[params])
        tx = self._build_eip1559(
            gas_limit=C.GAS_LIMIT_SWAP, to=router, value=amount_in, data=data
        )
        receipt = self._send_and_wait(tx)
        return receipt["transactionHash"].hex()

    def swap_exact_in(
        self,
        router: str,
        token_in: str,
        token_out: str,
        fee: int,
        amount_in: int,
        amount_out_min: int,
        deadline_seconds: int = 600,
    ) -> str:
        """ERC20-in swap: approve EXACT amount_in, swap, then revoke approval."""
        if amount_out_min <= 0:
            raise ChainError("amount_out_min must be > 0 — refuse 0-slippage swap")

        self._ensure_allowance(token_in, router, amount_in, kind="swap")

        contract = self.w3.eth.contract(address=Web3.to_checksum_address(router), abi=SWAP_ROUTER_ABI)
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            self.address,
            int(time()) + deadline_seconds,
            amount_in,
            amount_out_min,
            0,
        )
        data = contract.encode_abi("exactInputSingle", args=[params])
        tx = self._build_eip1559(gas_limit=C.GAS_LIMIT_SWAP, to=router, data=data)
        receipt = self._send_and_wait(tx)
        try:
            self._revoke(token_in, router)
        except ChainError as e:
            self.printer.warn(f"approve revoke failed (allowance left exposed): {e}")
        return receipt["transactionHash"].hex()

    def add_liquidity_v3_native(
        self,
        manager: str,
        token0: str,
        token1: str,
        fee: int,
        amount0: int,
        amount1_native: int,
        slippage_bps: int,
        tick_lower: int = C.TICK_LOWER,
        tick_upper: int = C.TICK_UPPER,
        deadline_seconds: int = 600,
    ) -> str:
        """Add liquidity where token1 is auto-wrapped from native value.

        token0 is an ERC20 (e.g. USDT) — we approve EXACT amount0 then revoke.
        token1 (e.g. WX1T) is supplied via ``msg.value = amount1_native``;
        the manager wraps it. amount0Min/amount1Min computed from
        ``slippage_bps`` (NEVER 0).
        """
        if slippage_bps <= 0 or slippage_bps > C.BPS_DENOMINATOR:
            raise ChainError(f"slippage_bps out of range: {slippage_bps}")

        self._ensure_allowance(token0, manager, amount0, kind="add-liq token0")

        amount0_min = amount0 * (C.BPS_DENOMINATOR - slippage_bps) // C.BPS_DENOMINATOR
        amount1_min = amount1_native * (C.BPS_DENOMINATOR - slippage_bps) // C.BPS_DENOMINATOR
        if amount0_min == 0 or amount1_min == 0:
            raise ChainError("min amounts collapsed to 0 — increase amounts or reduce slippage")

        contract = self.w3.eth.contract(address=Web3.to_checksum_address(manager), abi=MINT_ROUTER_ABI)
        params = (
            Web3.to_checksum_address(token0),
            Web3.to_checksum_address(token1),
            fee,
            tick_lower,
            tick_upper,
            amount0,
            amount1_native,
            amount0_min,
            amount1_min,
            self.address,
            int(time()) + deadline_seconds,
        )
        data = contract.encode_abi("mint", args=[params])
        tx = self._build_eip1559(
            gas_limit=C.GAS_LIMIT_MINT, to=manager, value=amount1_native, data=data
        )
        receipt = self._send_and_wait(tx)

        try:
            self._revoke(token0, manager)
        except ChainError as e:
            self.printer.warn(f"revoke failed for {token0}: {e}")
        return receipt["transactionHash"].hex()

    def add_liquidity_v3(
        self,
        manager: str,
        pool: str,
        token0: str,
        token1: str,
        fee: int,
        amount0: int,
        amount1: int,
        slippage_bps: int,
        deadline_seconds: int = 600,
    ) -> str:
        """ERC20-only add liquidity: approve EXACT (token0, token1), mint, revoke both."""
        if slippage_bps <= 0 or slippage_bps > C.BPS_DENOMINATOR:
            raise ChainError(f"slippage_bps out of range: {slippage_bps}")

        self._ensure_allowance(token0, manager, amount0, kind="add-liq token0")
        self._ensure_allowance(token1, manager, amount1, kind="add-liq token1")

        amount0_min = amount0 * (C.BPS_DENOMINATOR - slippage_bps) // C.BPS_DENOMINATOR
        amount1_min = amount1 * (C.BPS_DENOMINATOR - slippage_bps) // C.BPS_DENOMINATOR
        assert amount0_min > 0 and amount1_min > 0, "min amounts collapsed to 0 — increase amounts or reduce slippage"

        contract = self.w3.eth.contract(address=Web3.to_checksum_address(manager), abi=MINT_ROUTER_ABI)
        params = (
            Web3.to_checksum_address(token0),
            Web3.to_checksum_address(token1),
            fee,
            C.TICK_LOWER,
            C.TICK_UPPER,
            amount0,
            amount1,
            amount0_min,
            amount1_min,
            self.address,
            int(time()) + deadline_seconds,
        )
        data = contract.encode_abi("mint", args=[params])
        tx = self._build_eip1559(gas_limit=C.GAS_LIMIT_MINT, to=manager, data=data)
        receipt = self._send_and_wait(tx)

        for tok in (token0, token1):
            try:
                self._revoke(tok, manager)
            except ChainError as e:
                self.printer.warn(f"revoke failed for {tok}: {e}")
        return receipt["transactionHash"].hex()

    def deploy_token(
        self,
        deploy_router: str,
        payable_addr: str,
        value_wei: int,
        creation_code: bytes,
    ) -> tuple[str, str]:
        """Call sendAndDeploy(payable, value, creationCode), return (tx_hash, deployed_address).

        The token address is at receipt.logs[1].address per X1's deploy
        router behaviour (the first log is the deploy router event, the
        second is the new ERC20 contract's first emission).
        """
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(deploy_router), abi=DEPLOY_ROUTER_ABI)
        data = contract.encode_abi(
            "sendAndDeploy",
            args=[Web3.to_checksum_address(payable_addr), value_wei, creation_code],
        )
        tx = self._build_eip1559(gas_limit=C.GAS_LIMIT_DEPLOY, to=deploy_router, value=value_wei, data=data)
        receipt = self._send_and_wait(tx)
        deployed = self._extract_deployed_address(receipt)
        if not deployed:
            raise ChainError(f"deploy receipt missing token address: tx={receipt['transactionHash'].hex()}")
        return receipt["transactionHash"].hex(), deployed

    def query_pool_price(self, pool: str) -> tuple[int, str, str]:
        """Return (sqrtPriceX96, token0, token1) from the pool."""
        c = self.w3.eth.contract(address=Web3.to_checksum_address(pool), abi=POOL_V3_ABI)
        slot0 = c.functions.slot0().call()
        token0 = c.functions.token0().call()
        token1 = c.functions.token1().call()
        return slot0[0], token0, token1

    # --- Internals ---

    def _ensure_allowance(self, token: str, spender: str, needed: int, kind: str) -> None:
        current = self.allowance(token, spender)
        if current >= needed:
            self.printer.debug(f"{kind}: existing allowance {current} >= {needed}, reusing")
            return
        if current > 0:
            # Some tokens (USDT) require allowance to be 0 before changing it.
            self.printer.debug(f"{kind}: zeroing existing allowance {current}")
            self._approve(token, spender, 0)
        self.printer.debug(f"{kind}: approving exact {needed}")
        self._approve(token, spender, needed)

    def _revoke(self, token: str, spender: str) -> None:
        current = self.allowance(token, spender)
        if current == 0:
            return
        self.printer.debug(f"revoking allowance {token} -> {spender} (was {current})")
        self._approve(token, spender, 0)

    def _extract_deployed_address(self, receipt: TxReceipt) -> Optional[str]:
        # X1's deploy router emits the new token contract's address as the
        # `address` field on the second log entry (logs[1]). Fall back to
        # scanning topics, then receipt.contractAddress.
        logs = receipt.get("logs", [])
        if len(logs) >= 2 and logs[1].get("address"):
            return Web3.to_checksum_address(logs[1]["address"])
        for log in logs:
            for topic in log.get("topics", [])[1:]:
                hexed: HexStr = topic.hex() if hasattr(topic, "hex") else topic
                if isinstance(hexed, str) and len(hexed) == 66:
                    candidate = "0x" + hexed[-40:]
                    if int(candidate, 16) != 0:
                        return Web3.to_checksum_address(candidate)
        if receipt.get("contractAddress"):
            return Web3.to_checksum_address(receipt["contractAddress"])
        return None
