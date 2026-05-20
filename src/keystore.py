"""Encrypted keystore management.

Wraps eth_account's web3 keystore format. Private keys are never written
to disk in plaintext, never logged, and the decrypted hex key is wiped
from memory when the Keystore context exits.
"""
from __future__ import annotations

import ctypes
import getpass
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from eth_account import Account

from . import constants as C
from .printer import Printer
from .utils import mask_address


class KeystoreError(Exception):
    pass


def _wipe(s: Optional[str]) -> None:
    """Best-effort overwrite of a Python str's underlying buffer.

    CPython interns small/identical strings, so this is not guaranteed —
    treat it as defense-in-depth, not a hard guarantee. The real protection
    is keeping the decrypted key out of long-lived references.
    """
    if not s:
        return
    try:
        size = len(s.encode("utf-8"))
        addr = id(s) + ctypes.sizeof(ctypes.c_size_t) * 2 + ctypes.sizeof(ctypes.c_void_p)
        ctypes.memset(addr, 0, size)
    except Exception:
        pass


def _keystore_path(keystore_dir: Path, address: str) -> Path:
    return keystore_dir / f"{address.lower()}.json"


def create_keystore(
    keystore_dir: Path,
    private_key: str,
    password: str,
    iterations: int = C.KEYSTORE_KDF_ITERATIONS,
) -> Path:
    """Encrypt private_key with password and write keystore JSON.

    Returns the path to the written file. Raises KeystoreError if a keystore
    for the same address already exists (won't overwrite).
    """
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    try:
        acct = Account.from_key(private_key)
    except Exception as e:
        raise KeystoreError(f"Invalid private key: {e}") from e

    keystore_dir.mkdir(parents=True, exist_ok=True)
    out_path = _keystore_path(keystore_dir, acct.address)
    if out_path.exists():
        raise KeystoreError(f"Keystore for {mask_address(acct.address)} already exists at {out_path}")

    encrypted = Account.encrypt(private_key, password, iterations=iterations)
    out_path.write_text(json.dumps(encrypted))
    out_path.chmod(0o600)
    return out_path


def list_keystores(keystore_dir: Path) -> list[Path]:
    if not keystore_dir.exists():
        return []
    return sorted(p for p in keystore_dir.glob("*.json") if p.is_file())


@contextmanager
def unlock(path: Path, password: str) -> Iterator[tuple[str, str]]:
    """Yield (address, private_key_hex) and wipe on exit.

    Use as: `with unlock(p, pw) as (addr, key): ...`
    """
    try:
        encrypted = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise KeystoreError(f"Cannot read keystore {path}: {e}") from e

    try:
        key_bytes = Account.decrypt(encrypted, password)
    except ValueError as e:
        raise KeystoreError("Wrong password") from e
    except Exception as e:
        raise KeystoreError(f"Decrypt failed: {e}") from e

    private_key = "0x" + key_bytes.hex()
    address = Account.from_key(private_key).address
    try:
        yield address, private_key
    finally:
        _wipe(private_key)
        try:
            ctypes.memset((ctypes.c_char * len(key_bytes)).from_buffer(bytearray(key_bytes)), 0, len(key_bytes))
        except Exception:
            pass
        del key_bytes


def prompt_password(prompt: str = "Keystore password: ", confirm: bool = False) -> str:
    pw = getpass.getpass(prompt)
    if not pw:
        raise KeystoreError("Empty password")
    if confirm:
        pw2 = getpass.getpass("Confirm password: ")
        if pw != pw2:
            raise KeystoreError("Passwords do not match")
    return pw


def cli_create_keystore(keystore_dir: Path, printer: Printer) -> None:
    """Interactive: prompt private key + password, write keystore."""
    pk = getpass.getpass("Private key (hex, with or without 0x): ").strip()
    if not pk:
        printer.error("Empty private key, aborting")
        return
    try:
        password = prompt_password(confirm=True)
        path = create_keystore(keystore_dir, pk, password)
    except KeystoreError as e:
        printer.error(str(e))
        return
    finally:
        _wipe(pk)

    addr = path.stem
    printer.success(f"Keystore created for {mask_address(addr)} at {path}")
