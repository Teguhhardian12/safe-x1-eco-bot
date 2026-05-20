# safe-x1-eco-bot

Safe rebuild of [vonssy/X1-Ecochain-BOT](https://github.com/vonssy/X1-Ecochain-BOT) for X1 testnet (Maculatus, chain ID 10778). Same feature set, hardened against the wallet-draining patterns in the original.

**Status:** Implementation complete (Phase 1-7). Real testnet smoke test pending — see Verification below.

## Project layout

```
safe-x1-eco-bot/
├── src/
│   ├── main.py        # CLI + main loop
│   ├── config.py      # .env -> frozen dataclass + validation
│   ├── keystore.py    # encrypted keystore: create/unlock/wipe
│   ├── client.py      # async HTTP client for X1 API + subgraph
│   ├── chain.py       # transfer/swap/add-liq/deploy with safety contract
│   ├── deploy.py      # solc compile + vendored OZ resolution
│   ├── abi.py         # ERC20 + V3 router/manager/pool ABIs
│   ├── constants.py   # X1 testnet endpoints, contract addresses
│   ├── printer.py     # colored timestamped logger
│   └── utils.py       # retry decorator, async delay, helpers
├── scripts/
│   └── vendor_oz.sh   # pin & install OpenZeppelin Contracts to contracts/oz/
├── contracts/oz/      # populated by vendor_oz.sh, gitignored
└── keystores/         # populated by --create-keystore, gitignored
```

## Setup

```bash
git clone https://github.com/Teguhhardian12/safe-x1-eco-bot.git
cd safe-x1-eco-bot

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Vendor OpenZeppelin Contracts (writes to contracts/oz/)
./scripts/vendor_oz.sh

cp .env.example .env  # endpoints + chain ID prefilled for X1 testnet

# Create an encrypted keystore for a test wallet
python -m src.main --create-keystore
# -> prompts for private key + password, writes keystores/<addr>.json (0600)

# Dry run: no txs sent, just builds and logs
python -m src.main --no-loop --dry-run --verbose

# Real run (will send testnet txs):
python -m src.main --no-loop --verbose
```

## CLI reference

| Flag | Purpose |
|---|---|
| `--verbose`, `-v` | show debug output (compile steps, nonce, allowance) |
| `--no-loop` | run one pass through all keystores then exit |
| `--dry-run` | log what would be sent, but don't sign or broadcast |
| `--keystore-dir PATH` | override `KEYSTORE_DIR` from .env |
| `--create-keystore` | interactive wallet import + encrypt, then exit |
| `--env-file PATH` | path to a custom .env |

## Verified so far

- `src/`: 4 config validation cases, 7 keystore cases, 5 client cases (incl. retry-on-500), 6 deploy security cases (path traversal, scheme rejection), GasPolicy + slippage math, `--help` and parse_args, frozen-config override
- Solidity compile end-to-end: ERC20 + vendored OZ v5.0.0 -> 6313-byte bytecode, 18 ABI entries
- X1 testnet RPC live: chain ID `0x2a1a` = 10778 ✓, block height progressing
- Contract bytecode confirmed on-chain for WX1T, USDT, swap router, mint router, deploy router
- ERC20 ABI calls return correct metadata: WX1T (18 dec), USDT (18 dec — note: not 6 like Ethereum mainnet USDT)

## Pending

- Live testnet run with a funded throwaway wallet (faucet -> transfer -> swap -> add-liq -> deploy)
- Quest type strings haven't been confirmed against the real `/quests` payload — the dispatcher in `_run_quest` infers types from `quest["type"]`/`quest["action"]`; first live run will surface the canonical names
- Subgraph schema for `pool_by_tokens` is best-effort; if `https://ms.kod.af` exposes a different schema, `client.pool_by_tokens` will need adjusting

## Disclaimer

For X1 **testnet** only. Don't run with mainnet keys or significant funds. Audit the code yourself before trusting it.

Endpoints and contract addresses are sourced from the original [vonssy/X1-Ecochain-BOT](https://github.com/vonssy/X1-Ecochain-BOT/blob/main/bot.py#L30-L54). Verify on-chain before relying on them.
