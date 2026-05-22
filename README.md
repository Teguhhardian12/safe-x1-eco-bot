# safe-x1-eco-bot

Safe rebuild of [vonssy/X1-Ecochain-BOT](https://github.com/vonssy/X1-Ecochain-BOT) for X1 testnet (Maculatus, chain ID 10778). Same on-chain feature set, hardened against the wallet-draining patterns in the original.

**Status:** Phase 13 — live tested 2026-05-21. All 5 dispatchable quests pass on a single wallet (10 pts/cycle: faucet 1 + swap 2 + transfer 1 + liquidity 3 + tc 3). Phase 13 adds auto-save of proxy details during `--create-keystore`.

## Project layout

```
safe-x1-eco-bot/
├── src/
│   ├── main.py        # CLI + main loop, constructor SIWE flow
│   ├── config.py      # .env -> frozen dataclass + validation
│   ├── keystore.py    # encrypted keystore: create/unlock/wipe
│   ├── client.py      # async HTTP client for X1 API + subgraph + constructor
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

### Prerequisites

- Python ≥ 3.10
- `npm` or `tar`+`curl` (for vendoring OpenZeppelin)
- Internet on first run (solc 0.8.20 auto-downloaded to `~/.solcx/`, then cached)

### Install

```bash
git clone https://github.com/Teguhhardian12/safe-x1-eco-bot.git
cd safe-x1-eco-bot

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Optional: SOCKS proxy support (only if you'll set PROXY_URL=socks5://...)
pip install aiohttp-socks

# Vendor OpenZeppelin Contracts (writes to contracts/oz/)
./scripts/vendor_oz.sh

cp .env.example .env  # endpoints + chain ID prefilled for X1 testnet
```

### Add a wallet

```bash
python -m src.main --create-keystore
# 1. prompts for private key + password, writes keystores/<addr>.json (0600 perms)
# 2. optionally prompts for proxy (scheme/host/port/user/pw) and writes
#    the URL into proxies.json at the repo root (0600 perms)
```

Repeat for every wallet you want to farm. Each one becomes a separate file in `keystores/`. The bot processes all of them in a single run, and auto-loads `proxies.json` from the repo root if present.

### Run

```bash
# Dry run: connect, list quests, log dispatches, but don't sign or broadcast
python -m src.main --no-loop --dry-run --verbose

# Real run: sends testnet txs and claims quest points
python -m src.main --no-loop --verbose
```

You'll be prompted for each keystore's password once at the start of its turn (3 retries on wrong password). Quests run in this priority: faucet → tc → swap → transfer → liquidity.

## Multi-wallet

One run iterates every `keystores/*.json` file sequentially:

1. Prompt password for wallet 1 → sign in → run 5 quests → +10 pts
2. Sleep `ACCOUNT_DELAY_SECONDS` (default 10s, set in `.env`)
3. Prompt password for wallet 2 → ... and so on

Each wallet uses an isolated `X1Client` instance — auth tokens, constructor sessions, and nonces don't bleed across wallets. There's no global lock, just a per-account stagger delay.

## Configuration (.env)

| Key | Purpose | Default |
|---|---|---|
| `X1_RPC_URL` | EVM RPC for tx signing | `https://maculatus-rpc.x1eco.com/` |
| `X1_CHAIN_ID` | network chain ID | `10778` |
| `X1_API_BASE` | testnet quest API | `https://testnet-api.x1eco.com` |
| `X1_SUBGRAPH_URL` | DEX subgraph for pool data | `https://ms.kod.af` |
| `KEYSTORE_DIR` | where keystores live | `keystores` |
| `LOOP_INTERVAL_SECONDS` | pause between full passes (loop mode) | `3600` |
| `ACCOUNT_DELAY_SECONDS` | pause between wallets | `10` |
| `SLIPPAGE_BPS` | swap/add-liq slippage in basis points | `200` (2%) |
| `GAS_PRIORITY_FEE_GWEI` | EIP-1559 priority fee | `1.5` |
| `TRANSFER_PCT` | % of native balance per transfer quest | `2` |
| `SWAP_PCT` | % of native balance per swap | `15` |
| `ADD_LIQUIDITY_PCT` | % of native balance per add-liq | `15` |
| `PROXY_URL` | optional outbound proxy (see below) | empty |
| `PROXY_MAP_FILE` | path to per-wallet proxy mapping; auto-detects `proxies.json` at repo root if unset | empty |

### Proxy

Set `PROXY_URL` in `.env` to route every HTTP call (X1 API, faucet, subgraph, constructor) through a proxy. The on-chain RPC connection is not proxied — `web3.py` uses its own HTTP transport.

Supported schemes:

```ini
# HTTP/HTTPS proxy
PROXY_URL=http://user:pass@host:port

# SOCKS5 (requires `pip install aiohttp-socks`)
PROXY_URL=socks5://user:pass@host:port
```

`PROXY_URL` is **global per-process** — every wallet in the run uses it.

#### Per-wallet proxy

For one IP per wallet (residential proxies, anti-detection), the bot reads a JSON map at `proxies.json` (repo root, auto-detected) or whatever `PROXY_MAP_FILE` in `.env` points to:

```json
{
  "0xa256ca...35ff": "http://user:pass@us-proxy:8080",
  "0xb789cd...12ee": "socks5://user:pass@sg-proxy:1080",
  "default":         "http://user:pass@fallback:8080"
}
```

The fastest way to populate it is `--create-keystore` — answer `y` at the proxy prompt and the entry is upserted for you (credentials are URL-encoded). Manual edits work too.

Resolution order per wallet: specific address entry → `"default"` key → `PROXY_URL` → no proxy. Address keys are matched case-insensitively. Empty string disables the proxy for that wallet. `proxies.json` is gitignored — it stores credentials in plaintext.

One run, one prompt sequence — every wallet still iterates in the same loop, but each one opens its own `X1Client` with its own proxy. Run `--verbose` to see which proxy got picked per wallet (`proxy: per-wallet entry...` / `proxy: map default...`).

## CLI reference

| Flag | Purpose |
|---|---|
| `--verbose`, `-v` | show debug output (compile steps, nonce, allowance, constructor session) |
| `--no-loop` | run one pass through all keystores then exit |
| `--dry-run` | log what would be dispatched, but don't sign or broadcast |
| `--keystore-dir PATH` | override `KEYSTORE_DIR` from .env |
| `--create-keystore` | interactive wallet import + encrypt + optional proxy save, then exit |
| `--env-file PATH` | path to a custom .env |

## Safety contracts

The original bot has a few patterns that can drain a wallet under specific conditions. This rebuild replaces each:

| Original | Safe rebuild |
|---|---|
| Plaintext private key in env | Encrypted keystore (web3 format), 0600 perms, ctypes.memset wipe on unlock exit |
| `approve(spender, 2^256-1)` left set | `approve(EXACT)` + `approve(0)` revoke after the spend |
| `amountOutMin = 0`, `amountMin = 0` | Computed from pool `slot0` sqrtPrice + configured slippage bps |
| `maxFeePerGas = baseFee + 1` | `maxFeePerGas = 2 * baseFee + priorityFee` (survives base-fee spikes) |
| `import @openzeppelin/...` resolved to jsDelivr CDN at compile time | Vendored OZ v5.0.0 in `contracts/oz/`, path-traversal blocked |

## Disclaimer

For X1 **testnet** only. Don't run with mainnet keys or significant funds. Audit the code yourself before trusting it.

Endpoints and contract addresses are sourced from [vonssy/X1-Ecochain-BOT](https://github.com/vonssy/X1-Ecochain-BOT/blob/main/bot.py). Verify on-chain before relying on them.
