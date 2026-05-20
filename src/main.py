"""CLI entry point and main loop.

Workflow per account:
  1. prompt password, decrypt keystore
  2. SIWE auth: get message -> sign -> POST signature -> bearer token
  3. fetch quests
  4. dispatch each quest to its handler (faucet/transfer/swap/add-liq/deploy)
  5. POST quest completion (with tx_hash if available)
  6. wipe key from memory
  7. inter-account delay
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from eth_account.messages import encode_defunct

from . import constants as C
from .chain import ChainContext, ChainError
from .client import APIError, X1Client
from .config import Config, ConfigError, load_config
from .keystore import (
    KeystoreError,
    cli_create_keystore,
    list_keystores,
    prompt_password,
    unlock,
)
from .printer import Printer
from .utils import delay, mask_address


async def run_account(
    keystore_path: Path,
    password: str,
    cfg: Config,
    *,
    dry_run: bool,
    printer: Printer,
) -> None:
    """Run one full pass for the account behind keystore_path."""
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
                token = await _siwe_signin(chain, client, address, printer)
                client.set_token(token)
                me = await client.auth_me()
                printer.debug(f"auth_me: {me}")

                quests = await client.quests_list()
                printer.info(f"{len(quests)} quest(s) available")

                for quest in quests:
                    await _run_quest(quest, chain, client, cfg, dry_run=dry_run, printer=printer)
    except KeystoreError as e:
        printer.error(f"keystore error for {keystore_path.name}: {e}")
    except ChainError as e:
        printer.error(f"chain error for {keystore_path.name}: {e}")
    except APIError as e:
        printer.error(f"api error for {keystore_path.name}: {e}")


async def _siwe_signin(
    chain: ChainContext, client: X1Client, address: str, printer: Printer
) -> str:
    message = await client.auth_message(address)
    encoded = encode_defunct(text=message)
    signed = chain.account.sign_message(encoded)
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature
    token = await client.auth_signin(address, signature)
    printer.success(f"signed in as {mask_address(address)}")
    return token


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
    qtype = str(quest.get("type") or quest.get("action") or "").lower()
    printer.info(f"quest {qid} ({qtype})")

    if dry_run:
        printer.warn(f"[dry-run] skipping {qtype}")
        return

    tx_hash: str | None = None
    try:
        if qtype == "faucet":
            r = await client.request_faucet(chain.address)
            printer.success(f"faucet: {r}")
        elif qtype == "transfer":
            tx_hash = chain.transfer_native(
                to=chain.address,  # self-transfer if no recipient specified
                amount_wei=int(cfg.transfer_amount * 10**18),
            )
            printer.success(f"transfer tx: {tx_hash}")
        elif qtype == "swap":
            printer.warn("swap quest needs router/token addresses configured in constants — skipping")
            return
        elif qtype == "add_liquidity":
            printer.warn("add_liquidity quest needs router/token addresses configured in constants — skipping")
            return
        elif qtype == "deploy":
            printer.warn("deploy quest needs deploy router configured in constants — skipping")
            return
        else:
            printer.warn(f"unknown quest type: {qtype}")
            return

        await client.complete_quest(qid, tx_hash=tx_hash)
        printer.success(f"quest {qid} marked complete")
    except (ChainError, APIError) as e:
        printer.error(f"quest {qid} failed: {e}")


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
            for attempt in range(3):
                try:
                    pw = prompt_password(f"Password for {ks.stem}: ")
                except (KeystoreError, EOFError) as e:
                    printer.error(f"password input failed: {e}")
                    break
                try:
                    with unlock(ks, pw) as _:
                        pass  # validate password before running
                    break
                except KeystoreError as e:
                    if "Wrong password" in str(e) and attempt < 2:
                        printer.warn(f"wrong password, retry {attempt + 1}/3")
                        continue
                    printer.error(f"giving up on {ks.name}: {e}")
                    pw = None  # signal skip
                    break
            else:
                pw = None

            if pw:
                await run_account(ks, pw, cfg, dry_run=dry_run, printer=printer)
            await delay(cfg.account_delay)

        if no_loop:
            return
        printer.info(f"sleeping {cfg.loop_interval}s before next pass")
        await delay(cfg.loop_interval)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Safe X1 testnet farming bot")
    p.add_argument("--verbose", "-v", action="store_true", help="show debug output")
    p.add_argument("--no-loop", action="store_true", help="run one pass and exit")
    p.add_argument("--dry-run", action="store_true", help="build txs and log, don't send")
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
    """Return a new Config with keystore_dir replaced (Config is frozen)."""
    from dataclasses import replace
    return replace(cfg, keystore_dir=new_dir)


if __name__ == "__main__":
    sys.exit(main())
