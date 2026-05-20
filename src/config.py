"""Configuration loader: parses .env, validates fields, returns frozen dataclass."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from . import constants as C


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    rpc_url: str
    chain_id: int
    api_base: str
    subgraph_url: str
    loop_interval: int
    account_delay: int
    slippage_bps: int
    priority_fee_gwei: float
    proxy_url: Optional[str]
    keystore_dir: Path
    transfer_amount: float
    swap_amount: float
    add_liquidity_amount: float


def _required(key: str, value: Optional[str]) -> str:
    if not value:
        raise ConfigError(f"Missing required env var: {key}")
    return value


def _as_int(key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as e:
        raise ConfigError(f"{key} must be an integer, got {value!r}") from e


def _as_float(key: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as e:
        raise ConfigError(f"{key} must be a float, got {value!r}") from e


def _validate_url(key: str, value: str) -> str:
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ConfigError(f"{key} must start with http:// or https://, got {value!r}")
    return value


def _validate_proxy(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    valid = ("http://", "https://", "socks5://", "socks4://")
    if not value.startswith(valid):
        raise ConfigError(f"PROXY_URL must use one of {valid}, got {value!r}")
    return value


def load_config(env_file: Optional[Path] = None) -> Config:
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    rpc_url = _validate_url("X1_RPC_URL", _required("X1_RPC_URL", os.getenv("X1_RPC_URL")))
    chain_id = _as_int("X1_CHAIN_ID", _required("X1_CHAIN_ID", os.getenv("X1_CHAIN_ID")))
    api_base = _validate_url("X1_API_BASE", _required("X1_API_BASE", os.getenv("X1_API_BASE")))
    subgraph_url = _validate_url("X1_SUBGRAPH_URL", _required("X1_SUBGRAPH_URL", os.getenv("X1_SUBGRAPH_URL")))

    loop_interval = _as_int("LOOP_INTERVAL_SECONDS", os.getenv("LOOP_INTERVAL_SECONDS", str(C.DEFAULT_LOOP_INTERVAL)))
    account_delay = _as_int("ACCOUNT_DELAY_SECONDS", os.getenv("ACCOUNT_DELAY_SECONDS", str(C.DEFAULT_ACCOUNT_DELAY)))
    slippage_bps = _as_int("SLIPPAGE_BPS", os.getenv("SLIPPAGE_BPS", str(C.DEFAULT_SLIPPAGE_BPS)))
    priority_fee_gwei = _as_float("GAS_PRIORITY_FEE_GWEI", os.getenv("GAS_PRIORITY_FEE_GWEI", str(C.DEFAULT_PRIORITY_FEE_GWEI)))

    if not (0 <= slippage_bps <= C.BPS_DENOMINATOR):
        raise ConfigError(f"SLIPPAGE_BPS must be 0-{C.BPS_DENOMINATOR}, got {slippage_bps}")
    if loop_interval < 0:
        raise ConfigError(f"LOOP_INTERVAL_SECONDS must be non-negative, got {loop_interval}")
    if account_delay < 0:
        raise ConfigError(f"ACCOUNT_DELAY_SECONDS must be non-negative, got {account_delay}")
    if priority_fee_gwei < 0:
        raise ConfigError(f"GAS_PRIORITY_FEE_GWEI must be non-negative, got {priority_fee_gwei}")

    proxy_url = _validate_proxy(os.getenv("PROXY_URL") or None)
    keystore_dir = Path(os.getenv("KEYSTORE_DIR", C.KEYSTORE_DIR_DEFAULT))

    transfer_amount = _as_float("TRANSFER_AMOUNT", os.getenv("TRANSFER_AMOUNT", "0.001"))
    swap_amount = _as_float("SWAP_AMOUNT", os.getenv("SWAP_AMOUNT", "0.001"))
    add_liquidity_amount = _as_float("ADD_LIQUIDITY_AMOUNT", os.getenv("ADD_LIQUIDITY_AMOUNT", "0.001"))

    for name, val in (("TRANSFER_AMOUNT", transfer_amount), ("SWAP_AMOUNT", swap_amount), ("ADD_LIQUIDITY_AMOUNT", add_liquidity_amount)):
        if val <= 0:
            raise ConfigError(f"{name} must be > 0, got {val}")

    return Config(
        rpc_url=rpc_url,
        chain_id=chain_id,
        api_base=api_base,
        subgraph_url=subgraph_url,
        loop_interval=loop_interval,
        account_delay=account_delay,
        slippage_bps=slippage_bps,
        priority_fee_gwei=priority_fee_gwei,
        proxy_url=proxy_url,
        keystore_dir=keystore_dir,
        transfer_amount=transfer_amount,
        swap_amount=swap_amount,
        add_liquidity_amount=add_liquidity_amount,
    )
