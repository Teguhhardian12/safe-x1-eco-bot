"""Configuration loader: parses .env, validates fields, returns frozen dataclass."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
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
    proxy_map: dict[str, str]
    proxy_map_file: Optional[Path]
    keystore_dir: Path
    transfer_pct: float
    swap_pct: float
    add_liquidity_pct: float


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


def _load_proxy_map(path: Optional[Path]) -> dict[str, str]:
    """Load address->proxy_url mapping from a JSON file.

    Format: { "0xAddress": "http://...", "0xOther": "socks5://...", "default": "..." }

    Address keys are normalised to lowercase. The optional "default" key is
    used when a wallet has no specific entry. Each URL is validated with
    the same scheme rules as PROXY_URL.
    """
    if path is None:
        return {}
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"PROXY_MAP_FILE is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"PROXY_MAP_FILE must be a JSON object, got {type(raw).__name__}")

    out: dict[str, str] = {}
    for key, val in raw.items():
        if not isinstance(val, str):
            raise ConfigError(f"PROXY_MAP_FILE[{key!r}] must be a string, got {type(val).__name__}")
        validated = _validate_proxy(val)
        if validated is None:
            continue  # empty string disables proxy for this wallet
        normalised = key.lower() if key.startswith("0x") else key
        out[normalised] = validated
    return out


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
    proxy_map_file_str = os.getenv("PROXY_MAP_FILE") or None
    if proxy_map_file_str:
        proxy_map_file = Path(proxy_map_file_str)
    else:
        default_map = Path(__file__).resolve().parent.parent / "proxies.json"
        proxy_map_file = default_map if default_map.exists() else None
    proxy_map = _load_proxy_map(proxy_map_file)
    keystore_dir = Path(os.getenv("KEYSTORE_DIR", C.KEYSTORE_DIR_DEFAULT))

    transfer_pct = _as_float("TRANSFER_PCT", os.getenv("TRANSFER_PCT", "2"))
    swap_pct = _as_float("SWAP_PCT", os.getenv("SWAP_PCT", "15"))
    add_liquidity_pct = _as_float("ADD_LIQUIDITY_PCT", os.getenv("ADD_LIQUIDITY_PCT", "15"))

    for name, val in (("TRANSFER_PCT", transfer_pct), ("SWAP_PCT", swap_pct), ("ADD_LIQUIDITY_PCT", add_liquidity_pct)):
        if not (0 < val < 100):
            raise ConfigError(f"{name} must be in (0, 100), got {val}")

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
        proxy_map=proxy_map,
        proxy_map_file=proxy_map_file,
        keystore_dir=keystore_dir,
        transfer_pct=transfer_pct,
        swap_pct=swap_pct,
        add_liquidity_pct=add_liquidity_pct,
    )
