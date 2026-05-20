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
import secrets
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from . import constants as C
from .chain import ChainContext, ChainError
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

            async with X1Client(
                api_base=cfg.api_base,
                subgraph_url=cfg.subgraph_url,
                proxy_url=cfg.proxy_url,
                printer=printer,
            ) as client:
                token = await _signin(chain, client, address, printer)
                client.set_token(token)
                await _show_stats(client, printer)
                quests = await client.quests_list()
                printer.info(f"{len(quests)} quest(s) returned")
                for quest in quests:
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
    recipient = _random_recipient()
    amount_wei = int(Decimal(str(cfg.transfer_amount)) * (10 ** 18))
    bal = chain.native_balance()
    if bal < amount_wei + (10 ** 17):  # leave 0.1 X1T for gas
        printer.warn(f"insufficient native balance: {bal/1e18:.4f}, need >= {amount_wei/1e18 + 0.1:.4f}")
        return False
    tx_hash = chain.transfer_native(to=recipient, amount_wei=amount_wei)
    printer.success(f"transfer {cfg.transfer_amount} X1T -> {mask_address(recipient)} | {tx_hash}")
    return True


async def _h_swap(_q, chain, client, cfg, printer):
    pools = await client.pool_by_tokens(C.WXC_ADDRESS, C.USDT_ADDRESS)
    if not pools:
        printer.warn("no pool data — cannot compute amount_out_min for swap")
        return False
    pool = pools[0]
    fee_tier = int(pool.get("feeTier") or C.DEFAULT_FEE_TIER)
    amount_in = int(Decimal(str(cfg.swap_amount)) * (10 ** 18))

    # Conservative minOut from sqrtPrice — chain.swap_exact_in refuses 0.
    # For now: compute a coarse minOut at slippage tolerance and let the
    # router enforce it. Future improvement: compute precisely from sqrtPrice.
    slip_factor = (C.BPS_DENOMINATOR - cfg.slippage_bps)
    amount_out_min = max(1, amount_in * slip_factor // C.BPS_DENOMINATOR)

    tx_hash = chain.swap_exact_in(
        router=C.SWAP_ROUTER_ADDRESS,
        token_in=C.WXC_ADDRESS,
        token_out=C.USDT_ADDRESS,
        fee=fee_tier,
        amount_in=amount_in,
        amount_out_min=amount_out_min,
    )
    printer.success(f"swap {cfg.swap_amount} WX1T -> USDT | {tx_hash}")
    return True


async def _h_liquidity(_q, chain, client, cfg, printer):
    pools = await client.pool_by_tokens(C.WXC_ADDRESS, C.USDT_ADDRESS)
    if not pools:
        printer.warn("no pool data — cannot add liquidity")
        return False
    pool = pools[0]
    token0 = (pool.get("token0") or {}).get("id") or C.WXC_ADDRESS
    token1 = (pool.get("token1") or {}).get("id") or C.USDT_ADDRESS
    fee_tier = int(pool.get("feeTier") or C.DEFAULT_FEE_TIER)
    amount = int(Decimal(str(cfg.add_liquidity_amount)) * (10 ** 18))

    tx_hash = chain.add_liquidity_v3(
        manager=C.MINT_ROUTER_ADDRESS,
        pool=Web3.to_checksum_address(pool["id"]),
        token0=Web3.to_checksum_address(token0),
        token1=Web3.to_checksum_address(token1),
        fee=fee_tier,
        amount0=amount,
        amount1=amount,
        slippage_bps=cfg.slippage_bps,
    )
    printer.success(f"add-liq {cfg.add_liquidity_amount} on pool {mask_address(pool['id'])} | {tx_hash}")
    return True


async def _h_deploy(_q, chain, client, _cfg, printer):
    params = generate_token_params()
    oz_dir = Path("contracts/oz")
    if not oz_dir.exists():
        printer.error("contracts/oz/ not vendored — run scripts/vendor_oz.sh first")
        return False
    creation_code, _abi = generate_creation_code(params, oz_dir, printer=printer)
    deploy_value = Web3.to_wei(100, "ether")  # X1's bot.py uses 100 X1T

    tx_hash, token_address = chain.deploy_token(
        deploy_router=C.DEPLOY_ROUTER_ADDRESS,
        payable_addr=C.PAYABLE_ADDRESS,
        value_wei=deploy_value,
        creation_code=creation_code,
    )
    printer.success(
        f"deployed {params.symbol} ({params.name}) at {token_address} | {tx_hash}"
    )
    # save_contracts requires constructor-flow auth; left as future work.
    return True


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
        for ks in keystores:
            printer.info(f"--- {ks.name} ---")
            pw = _prompt_with_retries(ks, printer)
            if pw is None:
                continue
            await run_account(ks, pw, cfg, dry_run=dry_run, printer=printer)
            await delay(cfg.account_delay)

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
        cli_create_keystore(cfg.keystore_dir, printer)
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
