"""CLI entry point and main loop.

Workflow per account:
  1. unlock keystore (password)
  2. /signin: GET message -> personal_sign -> POST {signature, address, ref_code}
  3. /me: read points, rank, ref_points
  4. /quests: enumerate; for each quest dispatch by type
       - faucet     -> request_faucet
       - transfer   -> on-chain native send to a random recipient
       - swap       -> on-chain swap WX1T <-> USDT (needs pool data)
       - liquidity  -> on-chain V3 mint position
       - tc         -> deploy a fresh ERC20 via sendAndDeploy
     skip: one_time + is_completed; daily + is_completed_today;
           type in {nomis, symbiosis, 7ion}; missing twitter/discord links
  5. complete_quest(quest_id) for each successful action
  6. wipe key from memory, inter-account delay
"""
from __future__ import annotations

import argparse
import asyncio
import random
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from . import constants as C
from .chain import ChainContext, ChainError, calc_amount_out_min, calc_paired_amount
from .client import APIError, X1Client
from .config import Config, ConfigError, load_config
from .deploy import generate_creation_code, generate_token_params
from .keystore import (
    KeystoreError,
    cli_create_keystore,
    list_keystores,
    prompt_password,
    unlock,
)
from .printer import Printer
from .utils import delay, mask_address


# Order quests run in. Faucet first so the wallet has X1T before any
# on-chain action; tc (deploy) right after faucet because it requires a
# fixed 100 X1T — running it before percent-based actions guarantees the
# balance is high enough.
_QUEST_PRIORITY = {
    "faucet": 0,
    "tc": 1,
    "swap": 2,
    "transfer": 3,
    "liquidity": 4,
}

# Quest types we explicitly opt out of even if their handler exists.
_QUEST_DISABLED: set[str] = set()


def _quest_sort_key(quest: dict[str, Any]) -> int:
    qtype = str(quest.get("type") or "").lower()
    return _QUEST_PRIORITY.get(qtype, 99)


# Only on-chain daily quests are dispatched. Everything else (social,
# partner integrations, default click-only quests) is dropped before the
# delay loop so multi-wallet runs stay fast.
_DISPATCHABLE_TYPES = frozenset(_QUEST_PRIORITY.keys())


def _filter_dispatchable(quests: list[dict[str, Any]], printer: Printer) -> list[dict[str, Any]]:
    keep: list[dict[str, Any]] = []
    dropped = 0
    for q in quests:
        qtype = str(q.get("type") or "").lower()
        if qtype in _DISPATCHABLE_TYPES:
            keep.append(q)
        else:
            dropped += 1
    if dropped:
        printer.info(f"dropped {dropped} non-onchain quest(s)")
    return keep


def _resolve_proxy(cfg: Config, address: str, printer: Printer) -> Optional[str]:
    """Pick the proxy URL for this wallet.

    Order: proxy_map[address] > proxy_map["default"] > cfg.proxy_url > None.
    Logs which one was picked so a misconfigured map is obvious in -v output.
    """
    addr_lower = address.lower()
    if addr_lower in cfg.proxy_map:
        printer.debug(f"proxy: per-wallet entry for {mask_address(address)}")
        return cfg.proxy_map[addr_lower]
    if "default" in cfg.proxy_map:
        printer.debug(f"proxy: map default for {mask_address(address)}")
        return cfg.proxy_map["default"]
    if cfg.proxy_url:
        printer.debug(f"proxy: env PROXY_URL for {mask_address(address)}")
        return cfg.proxy_url
    return None


REF_CODE = ""  # set to your own X1 referral code if you want to attribute signups


def _random_recipient() -> str:
    return Account.create().address


async def run_account(
    keystore_path: Path,
    password: str,
    cfg: Config,
    *,
    dry_run: bool,
    printer: Printer,
) -> None:
    try:
        with unlock(keystore_path, password) as (address, private_key):
            printer.info(f"unlocked {mask_address(address)}")
            chain = ChainContext.connect(
                rpc_url=cfg.rpc_url,
                chain_id=cfg.chain_id,
                private_key=private_key,
                priority_fee_gwei=cfg.priority_fee_gwei,
                printer=printer,
            )

            proxy_url = _resolve_proxy(cfg, address, printer)
            async with X1Client(
                api_base=cfg.api_base,
                subgraph_url=cfg.subgraph_url,
                proxy_url=proxy_url,
                printer=printer,
            ) as client:
                token = await _signin(chain, client, address, printer)
                client.set_token(token)
                await _show_stats(client, printer)
                quests = await client.quests_list()
                printer.info(f"{len(quests)} quest(s) returned")
                quests = _filter_dispatchable(quests, printer)
                quests.sort(key=_quest_sort_key)
                for i, quest in enumerate(quests):
                    if i > 0:
                        await delay(C.DEFAULT_QUEST_DELAY, jitter=C.DEFAULT_QUEST_JITTER)
                    await _run_quest(quest, chain, client, cfg, dry_run=dry_run, printer=printer)
    except KeystoreError as e:
        printer.error(f"keystore error for {keystore_path.name}: {e}")
    except ChainError as e:
        printer.error(f"chain error for {keystore_path.name}: {e}")
    except APIError as e:
        printer.error(f"api error for {keystore_path.name}: {e}")


async def _signin(chain: ChainContext, client: X1Client, address: str, printer: Printer) -> str:
    message = await client.auth_message(address)
    encoded = encode_defunct(text=message)
    signed = chain.account.sign_message(encoded)
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    token = await client.auth_signin(address, sig, ref_code=REF_CODE)
    printer.success(f"signed in as {mask_address(address)}")
    return token


async def _show_stats(client: X1Client, printer: Printer) -> None:
    me = await client.auth_me()
    if not isinstance(me, dict):
        return
    printer.info(
        f"points={me.get('points')} | ref_points={me.get('ref_points')} | "
        f"rank=#{me.get('rank')} | ref_rank=#{me.get('referral_rank')}"
    )


def _quest_skip_reason(quest: dict[str, Any]) -> Optional[str]:
    """Return a reason string if this quest should be skipped, else None."""
    qtype = str(quest.get("type") or "").lower()
    periodicity = quest.get("periodicity")

    if qtype in _QUEST_DISABLED:
        return f"disabled handler: {qtype}"

    # Cross-chain partner quests — bot doesn't perform them on-chain.
    if qtype in {"nomis", "symbiosis", "7ion"}:
        return f"unsupported partner type: {qtype}"

    if periodicity == "one_time" and quest.get("is_completed"):
        return "one_time already completed"
    if periodicity == "daily" and quest.get("is_completed_today"):
        return "daily already completed today"

    requirements = quest.get("requirements") or {}
    if requirements.get("linked_twitter"):
        return "requires linked twitter"
    if requirements.get("linked_discord"):
        return "requires linked discord"
    return None


async def _run_quest(
    quest: dict[str, Any],
    chain: ChainContext,
    client: X1Client,
    cfg: Config,
    *,
    dry_run: bool,
    printer: Printer,
) -> None:
    qid = str(quest.get("id") or quest.get("quest_id") or "?")
    qtype = str(quest.get("type") or "").lower()
    title = quest.get("title") or qid
    reward = quest.get("reward")

    skip = _quest_skip_reason(quest)
    if skip:
        printer.info(f"skip {title} ({qtype}): {skip}")
        return

    printer.info(f"quest {title} ({qtype}, reward={reward})")
    if dry_run:
        printer.warn(f"[dry-run] would dispatch {qtype}")
        return

    handler = _HANDLERS.get(qtype)
    if handler is None:
        printer.warn(f"no handler for type={qtype!r}, skipping")
        return

    try:
        ok = await handler(quest, chain, client, cfg, printer)
        if not ok:
            return
    except (ChainError, APIError) as e:
        printer.error(f"quest {qid} failed during dispatch: {e}")
        return

    try:
        await client.complete_quest(qid)
        printer.success(f"quest {qid} marked complete (+{reward} pts)")
    except APIError as e:
        printer.error(f"complete_quest({qid}) failed: {e}")


# --- Quest handlers ---

async def _h_faucet(_q, _chain, client, _cfg, printer):
    ok = await client.request_faucet(_chain.address)
    if ok:
        printer.success("faucet requested")
    return ok


async def _h_transfer(_q, chain, _client, cfg, printer):
    bal = chain.native_balance()
    amount_wei = bal * int(cfg.transfer_pct * 100) // 10_000
    gas_buffer = 10 ** 17  # 0.1 X1T
    if amount_wei <= 0 or bal < amount_wei + gas_buffer:
        printer.warn(
            f"insufficient native: {bal/1e18:.4f}, need {amount_wei/1e18:.4f} + 0.1 buffer"
        )
        return False
    recipient = _random_recipient()
    tx_hash = chain.transfer_native(to=recipient, amount_wei=amount_wei)
    printer.success(
        f"transfer {amount_wei/1e18:.6f} X1T ({cfg.transfer_pct}%) -> {mask_address(recipient)} | {tx_hash}"
    )
    return True


async def _h_swap(_q, chain, client, cfg, printer):
    pools = await client.pool_by_tokens(C.WXC_ADDRESS, C.USDT_ADDRESS)
    if not pools:
        printer.warn("no pool data — cannot compute amount_out_min for swap")
        return False
    bal = chain.native_balance()
    amount_in = bal * int(cfg.swap_pct * 100) // 10_000
    gas_buffer = 10 ** 16  # 0.01 X1T
    if amount_in <= 0 or bal < amount_in + gas_buffer:
        printer.warn(
            f"insufficient native: {bal/1e18:.4f}, need {amount_in/1e18:.4f} + 0.01 buffer"
        )
        return False

    amount_out_min = calc_amount_out_min(pools, "WX1T", amount_in, slippage_bps=cfg.slippage_bps)
    if amount_out_min <= 0:
        printer.warn(f"amount_out_min computed as {amount_out_min} — pool may be empty")
        return False
    pool = max(pools, key=lambda p: int(p.get("liquidity") or 0))
    fee_tier = int(pool.get("feeTier") or C.DEFAULT_FEE_TIER)

    tx_hash = chain.swap_exact_in_native(
        router=C.SWAP_ROUTER_ADDRESS,
        token_in=C.WXC_ADDRESS,
        token_out=C.USDT_ADDRESS,
        fee=fee_tier,
        amount_in=amount_in,
        amount_out_min=amount_out_min,
    )
    printer.success(
        f"swap {amount_in/1e18:.6f} X1T ({cfg.swap_pct}%) -> >= {amount_out_min/1e18:.6f} USDT | {tx_hash}"
    )
    return True


async def _h_liquidity(_q, chain, client, cfg, printer):
    pools = await client.pool_by_tokens(C.WXC_ADDRESS, C.USDT_ADDRESS)
    if not pools:
        printer.warn("no pool data — cannot add liquidity")
        return False
    pool = max(pools, key=lambda p: int(p.get("liquidity") or 0))
    fee_tier = int(pool.get("feeTier") or C.DEFAULT_FEE_TIER)

    bal = chain.native_balance()
    amount1_native = bal * int(cfg.add_liquidity_pct * 100) // 10_000
    if amount1_native <= 0 or bal < amount1_native + (10 ** 16):
        printer.warn(
            f"insufficient native: {bal/1e18:.4f}, need {amount1_native/1e18:.4f} + 0.01 buffer"
        )
        return False

    amount0_usdt = calc_paired_amount(pools, "WX1T", amount1_native)
    if amount0_usdt <= 0:
        printer.warn(f"computed USDT amount is {amount0_usdt} — pool may be empty")
        return False

    usdt_bal = chain.balance_of(C.USDT_ADDRESS)
    if usdt_bal < amount0_usdt:
        usable_usdt = usdt_bal * 99 // 100
        if usable_usdt <= 0:
            printer.warn(f"insufficient USDT: have {usdt_bal/1e18:.6f} — run swap quest first")
            return False
        scale = Decimal(usable_usdt) / Decimal(amount0_usdt)
        amount1_native = int(Decimal(amount1_native) * scale)
        amount0_usdt = usable_usdt
        printer.info(
            f"scaled add-liq down (USDT capped): {amount0_usdt/1e18:.6f} USDT + {amount1_native/1e18:.6f} X1T"
        )

    tx_hash = chain.add_liquidity_v3_native(
        manager=C.MINT_ROUTER_ADDRESS,
        token0=C.USDT_ADDRESS,
        token1=C.WXC_ADDRESS,
        fee=fee_tier,
        amount0=amount0_usdt,
        amount1_native=amount1_native,
        slippage_bps=cfg.slippage_bps,
    )
    printer.success(
        f"add-liq {amount0_usdt/1e18:.6f} USDT + {amount1_native/1e18:.6f} X1T ({cfg.add_liquidity_pct}%) | {tx_hash}"
    )
    return True


async def _h_deploy(_q, chain, client, _cfg, printer):
    params = generate_token_params()
    oz_dir = Path("contracts/oz")
    if not oz_dir.exists():
        printer.error("contracts/oz/ not vendored — run scripts/vendor_oz.sh first")
        return False
    creation_code, _abi = generate_creation_code(params, oz_dir, printer=printer)
    deploy_value = Web3.to_wei(100, "ether")  # X1's bot.py uses 100 X1T

    bal = chain.native_balance()
    if bal < deploy_value + (10 ** 17):
        printer.warn(f"deploy needs {deploy_value/1e18} X1T + gas, have {bal/1e18}")
        return False

    tx_hash, token_address = chain.deploy_token(
        deploy_router=C.DEPLOY_ROUTER_ADDRESS,
        payable_addr=C.PAYABLE_ADDRESS,
        value_wei=deploy_value,
        creation_code=creation_code,
    )
    printer.success(
        f"deployed {params.symbol} ({params.name}) at {token_address} | {tx_hash}"
    )

    # Constructor backend must know about the token before /quests will
    # accept tc completion. This is a separate auth flow + POST.
    try:
        await _constructor_signin(chain, client, printer)
        await client.save_contracts(
            owner=chain.address, token_address=token_address, name=params.name
        )
        printer.success(f"registered {params.name} in constructor backend")
    except APIError as e:
        printer.error(f"constructor save_contracts failed: {e}")
        return False

    await delay(3)  # match vonssy timing — backend needs a beat to index
    return True


async def _constructor_signin(chain: ChainContext, client: X1Client, printer: Printer) -> None:
    """SIWE-style sign-in to api-constructor.x1ecochain.com.

    Builds the message locally (no /signin GET); signs with the wallet key;
    posts to /auth/verify; stores the returned bearer token on the client.
    """
    nonce = await client.auth_nonce(chain.address)
    issued_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    message = (
        f"constructor.x1ecochain.com wants you to sign in with your Ethereum account:\n"
        f"{chain.address}\n\n"
        f"Sign in to Token Constructor.\n\n"
        f"URI: https://constructor.x1ecochain.com\n"
        f"Version: 1\n"
        f"Chain ID: {C.CHAIN_ID_DEFAULT}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )
    encoded = encode_defunct(text=message)
    signed = chain.account.sign_message(encoded)
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    resp = await client.auth_verify(signature=sig, message=message)
    token = resp.get("token") if isinstance(resp, dict) else None
    if not token:
        raise APIError(f"constructor auth_verify returned no token: {resp!r}")
    client.set_constructor_token(token)
    printer.debug("constructor session authorized")


_HANDLERS = {
    "faucet": _h_faucet,
    "transfer": _h_transfer,
    "swap": _h_swap,
    "liquidity": _h_liquidity,
    "tc": _h_deploy,
}


# --- Main loop ---

async def run_loop(cfg: Config, *, dry_run: bool, no_loop: bool, printer: Printer) -> None:
    keystores = list_keystores(cfg.keystore_dir)
    if not keystores:
        printer.error(
            f"no keystores in {cfg.keystore_dir} — create one with `python -m src.main --create-keystore`"
        )
        return

    printer.info(f"{len(keystores)} keystore(s) found in {cfg.keystore_dir}")

    while True:
        order = list(keystores)
        if cfg.shuffle_keystores:
            random.shuffle(order)
            printer.debug("shuffled keystore order for this pass")

        if cfg.initial_stagger_max > 0:
            stagger = random.uniform(0, cfg.initial_stagger_max)
            printer.info(f"initial stagger: sleeping {stagger:.1f}s before first wallet")
            await delay(stagger)

        for idx, ks in enumerate(order):
            printer.info(f"--- {ks.name} ---")
            pw = _prompt_with_retries(ks, printer)
            if pw is None:
                continue
            await run_account(ks, pw, cfg, dry_run=dry_run, printer=printer)
            if idx < len(order) - 1:
                base = cfg.account_delay
                jitter = cfg.account_delay_jitter
                wait = base + random.uniform(0, jitter) if jitter > 0 else base
                printer.debug(f"inter-wallet delay: {wait:.1f}s (base={base}, jitter≤{jitter})")
                await delay(wait)

        if no_loop:
            return
        printer.info(f"sleeping {cfg.loop_interval}s before next pass")
        await delay(cfg.loop_interval)


def _prompt_with_retries(ks: Path, printer: Printer) -> Optional[str]:
    """Prompt up to 3x; return password if a valid one was entered, else None."""
    for attempt in range(3):
        try:
            pw = prompt_password(f"Password for {ks.stem}: ")
        except (KeystoreError, EOFError) as e:
            printer.error(f"password input failed: {e}")
            return None
        try:
            with unlock(ks, pw) as _:
                return pw
        except KeystoreError as e:
            if "Wrong password" in str(e) and attempt < 2:
                printer.warn(f"wrong password, retry {attempt + 1}/3")
                continue
            printer.error(f"giving up on {ks.name}: {e}")
            return None
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Safe X1 testnet farming bot")
    p.add_argument("--verbose", "-v", action="store_true", help="show debug output")
    p.add_argument("--no-loop", action="store_true", help="run one pass and exit")
    p.add_argument("--dry-run", action="store_true", help="log dispatches; don't sign or broadcast")
    p.add_argument("--keystore-dir", type=Path, help="override KEYSTORE_DIR from .env")
    p.add_argument("--create-keystore", action="store_true", help="interactively create a keystore and exit")
    p.add_argument("--env-file", type=Path, help="path to .env (defaults to repo root .env)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    printer = Printer(verbose=args.verbose)

    try:
        cfg = load_config(env_file=args.env_file)
    except ConfigError as e:
        printer.error(f"config: {e}")
        return 2

    if args.keystore_dir:
        cfg = _override_keystore_dir(cfg, args.keystore_dir)

    if args.create_keystore:
        cli_create_keystore(cfg.keystore_dir, printer, proxy_map_file=cfg.proxy_map_file)
        return 0

    try:
        asyncio.run(run_loop(cfg, dry_run=args.dry_run, no_loop=args.no_loop, printer=printer))
    except KeyboardInterrupt:
        printer.warn("interrupted")
        return 130
    return 0


def _override_keystore_dir(cfg: Config, new_dir: Path) -> Config:
    from dataclasses import replace
    return replace(cfg, keystore_dir=new_dir)


if __name__ == "__main__":
    sys.exit(main())
