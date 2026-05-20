"""Solidity compilation + creation-code generation for token deploys.

Key safety property: imports like `@openzeppelin/contracts/...` are resolved
from a local vendored directory (contracts/oz/) — never from a CDN at
compile time. Compromising the deploy pipeline therefore requires writing
to the local filesystem, not just hijacking a network endpoint.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from eth_abi import encode as abi_encode
import solcx

from . import constants as C
from .printer import Printer
from .utils import random_premint, random_token_name


_IMPORT_RE = re.compile(r'^\s*import\s+(?:[^"\']*\s+from\s+)?["\']([^"\']+)["\']\s*;', re.MULTILINE)


@dataclass(frozen=True)
class TokenParams:
    name: str
    symbol: str
    premint: int
    decimals: int


@dataclass(frozen=True)
class CompiledContract:
    abi: list
    bytecode: bytes


def generate_token_params(decimals: int = C.DEFAULT_TOKEN_DECIMALS) -> TokenParams:
    name, symbol = random_token_name(C.TOKEN_NAME_PREFIXES, C.TOKEN_NAME_SUFFIXES)
    premint = random_premint(C.TOKEN_PREMINT_MIN, C.TOKEN_PREMINT_MAX)
    return TokenParams(name=name, symbol=symbol, premint=premint, decimals=decimals)


def build_solidity_source(params: TokenParams) -> str:
    """Minimal ERC20 with premint to msg.sender, using vendored OZ."""
    return f"""// SPDX-License-Identifier: MIT
pragma solidity ^{C.SOLC_VERSION};

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract {params.symbol}Token is ERC20 {{
    constructor() ERC20("{params.name}", "{params.symbol}") {{
        _mint(msg.sender, {params.premint} * 10 ** decimals());
    }}
}}
"""


def _resolve_oz_path(import_path: str, oz_dir: Path) -> Path:
    """`@openzeppelin/contracts/foo/Bar.sol` -> `<oz_dir>/foo/Bar.sol`."""
    prefix = "@openzeppelin/contracts/"
    if not import_path.startswith(prefix):
        raise ValueError(f"Unsupported import path (only @openzeppelin/contracts/ allowed): {import_path}")
    rel = import_path[len(prefix):]
    candidate = (oz_dir / rel).resolve()
    if not str(candidate).startswith(str(oz_dir.resolve())):
        raise ValueError(f"Import escapes vendored OZ dir: {import_path}")
    if not candidate.exists():
        raise FileNotFoundError(f"OZ source not vendored: {import_path} (expected {candidate})")
    return candidate


def _normalize_import(import_path: str, source_key: str, oz_dir: Path) -> tuple[str, Path]:
    """Resolve an import to (canonical_key, filesystem_path).

    canonical_key is what we use as the source name for solc. We use the
    `@openzeppelin/contracts/...` form for OZ files and the relative-from-main
    path otherwise — solc only cares that the keys are consistent across the
    sources dict.
    """
    if import_path.startswith("@openzeppelin/contracts/"):
        return import_path, _resolve_oz_path(import_path, oz_dir)

    if import_path.startswith("./") or import_path.startswith("../"):
        if source_key == "main.sol":
            raise ValueError(f"main.sol cannot use relative imports: {import_path}")
        # source_key is something like "@openzeppelin/contracts/token/ERC20/ERC20.sol"
        # Resolve the relative import against the OZ-relative dirname of source_key.
        if not source_key.startswith("@openzeppelin/contracts/"):
            raise ValueError(f"Relative import from unknown source {source_key}: {import_path}")
        oz_rel = source_key[len("@openzeppelin/contracts/"):]
        parent_oz_rel = "/".join(oz_rel.split("/")[:-1])
        # Join and normalize
        joined = parent_oz_rel + "/" + import_path if parent_oz_rel else import_path
        parts: list[str] = []
        for seg in joined.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if not parts:
                    raise ValueError(f"Relative import escapes OZ root: {import_path} from {source_key}")
                parts.pop()
            else:
                parts.append(seg)
        canonical = "@openzeppelin/contracts/" + "/".join(parts)
        return canonical, _resolve_oz_path(canonical, oz_dir)

    raise ValueError(f"Unsupported import scheme: {import_path}")


def collect_sources(entry_source: str, oz_dir: Path) -> dict[str, str]:
    """Walk import graph, collect all needed .sol files keyed by their canonical import path.

    Relative imports inside OZ sources are normalised back to their
    `@openzeppelin/contracts/...` canonical form so solc sees a single key per file.
    """
    sources: dict[str, str] = {"main.sol": entry_source}
    queue: list[tuple[str, str]] = [("main.sol", entry_source)]
    seen: set[str] = {"main.sol"}

    while queue:
        current_key, content = queue.pop()
        for match in _IMPORT_RE.finditer(content):
            imp = match.group(1)
            canonical, fs_path = _normalize_import(imp, current_key, oz_dir)
            if canonical in seen:
                continue
            seen.add(canonical)
            text = fs_path.read_text()
            sources[canonical] = text
            queue.append((canonical, text))
    return sources


def compile_contract(
    sources: dict[str, str],
    contract_name: str,
    *,
    solc_version: str = C.SOLC_VERSION,
    evm_version: str = C.EVM_VERSION,
    printer: Printer | None = None,
) -> CompiledContract:
    if printer:
        printer.debug(f"installing solc {solc_version}")
    if solc_version not in [str(v) for v in solcx.get_installed_solc_versions()]:
        solcx.install_solc(solc_version)
    solcx.set_solc_version(solc_version)

    standard_input = {
        "language": "Solidity",
        "sources": {name: {"content": content} for name, content in sources.items()},
        "settings": {
            "evmVersion": evm_version,
            "optimizer": {"enabled": False},
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
        },
    }
    out = solcx.compile_standard(standard_input, allow_paths=".")
    contracts = out["contracts"]["main.sol"]
    if contract_name not in contracts:
        available = list(contracts.keys())
        raise ValueError(f"Contract {contract_name!r} not found in compile output. Got: {available}")
    artefact = contracts[contract_name]
    return CompiledContract(
        abi=artefact["abi"],
        bytecode=bytes.fromhex(artefact["evm"]["bytecode"]["object"]),
    )


def encode_constructor_args(types: Iterable[str], values: Iterable) -> bytes:
    types_list = list(types)
    values_list = list(values)
    if not types_list:
        return b""
    return abi_encode(types_list, values_list)


def generate_creation_code(
    params: TokenParams,
    oz_dir: Path,
    *,
    printer: Printer | None = None,
) -> tuple[bytes, list]:
    """Build full creation code (init bytecode + constructor args).

    Returns (creation_code_bytes, abi). The current template's constructor
    takes no arguments — _mint reads from constants baked into the source —
    so encode_constructor_args returns b"". If you change the template to
    parameterise name/symbol/premint at deploy time, wire those values here.
    """
    source = build_solidity_source(params)
    sources = collect_sources(source, oz_dir)
    if printer:
        printer.debug(f"collected {len(sources)} source file(s)")
    compiled = compile_contract(sources, f"{params.symbol}Token", printer=printer)
    args = encode_constructor_args([], [])
    return compiled.bytecode + args, compiled.abi
