# safe-x1-eco-bot

Safe rebuild of [vonssy/X1-Ecochain-BOT](https://github.com/vonssy/X1-Ecochain-BOT) with security hardening for use with main wallets.

**Status: Work in progress.** Phase 1 scaffold only — keystore, API client, deploy engine, chain actions, and main loop are pending.

## Why a rebuild

The original bot stores private keys in plaintext (`accounts.txt`), uses unlimited token approvals (`2^256 - 1`), sets slippage minimums to 0, fetches Solidity imports from a CDN at runtime, and ships as one 2195-line file. This rebuild preserves the same feature set (auth, faucet, transfer, swap, add liquidity, deploy token, quests) but with the safety upgrades below.

## Safety upgrades vs original

| Aspect | Original | This repo |
|---|---|---|
| Private key storage | Plaintext `accounts.txt` | Encrypted keystore JSON (web3 format) |
| Approval amount | `2^256 - 1` (unlimited) | Exact amount + revoke to 0 after |
| Add-liquidity slippage | `amount0Min = amount1Min = 0` | Computed from pool price |
| Gas `maxFeePerGas` | `baseFee + 1` | `2 * baseFee + priorityFee` |
| Solidity imports | CDN jsDelivr at runtime | Local `contracts/oz/` (vendored) |
| Code structure | 1 file, 2195 lines | 10 modules |
| Config validation | None | All fields validated |
| Nonce handling | Fetch once, crash on error | Refetch on `nonce too low` |

## Setup (preview, not all phases implemented yet)

```bash
git clone https://github.com/Teguhhardian12/safe-x1-eco-bot.git
cd safe-x1-eco-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with X1 testnet endpoints
```

## Disclaimer

For X1 **testnet** only. Don't run with mainnet keys. Audit the code before trusting it with real funds.
