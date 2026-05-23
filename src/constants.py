"""Static constants: addresses, URLs, fee tiers, defaults.

Endpoints and contract addresses are sourced from
https://github.com/vonssy/X1-Ecochain-BOT/blob/main/bot.py (lines 30-54).
Verify on-chain before relying on them — addresses can change.
"""

# Token addresses (X1 testnet, "Maculatus")
WXC_ADDRESS = "0xe2ED17Ae5e68863E77899205a83A8f1E138c608f"  # WX1T
USDT_ADDRESS = "0xd127BA1f0EfA2c5c7d9e6E7339DBafe2A6b1EAeC"

# Router addresses (X1 testnet)
SWAP_ROUTER_ADDRESS = "0x1BEC6C32bAA0881EA3f3Ec5e95d10EF8a252589B"
MINT_ROUTER_ADDRESS = "0x4505eEA72B4D215284305d794CCAc618cd5eA531"
DEPLOY_ROUTER_ADDRESS = "0x8364089f85CFc7Bb455f1c8F2D924568cE433f9F"
PAYABLE_ADDRESS = "0x34264ec130f9aD5Fc9aa20aB95e42067b1304B5a"

# X1 testnet endpoints
RPC_URL_DEFAULT = "https://maculatus-rpc.x1eco.com/"
EXPLORER_TX_PREFIX = "https://maculatus-scan.x1eco.com/tx/"
API_TESTNET = "https://testnet-api.x1eco.com"
API_NFT = "https://nft-api.x1eco.com"
API_DEX = "https://ms.kod.af"
API_CONSTRUCTOR = "https://api-constructor.x1ecochain.com"

# X1 testnet chain ID (sourced from bot.py message text "Chain ID: 10778")
CHAIN_ID_DEFAULT = 10778

# Uniswap V3 fee tiers (parts per million)
FEE_TIER_LOW = 500       # 0.05%
FEE_TIER_MEDIUM = 3000   # 0.3%
FEE_TIER_HIGH = 10000    # 1%
DEFAULT_FEE_TIER = FEE_TIER_MEDIUM

# Uniswap V3 tick range. The X1 router accepts ±887270 (default V3 fork
# slightly outside the canonical ±887220). Using the value the upstream
# router expects avoids the rare "tick out of range" revert.
TICK_LOWER = -887270
TICK_UPPER = 887270

# Decimals
DEFAULT_TOKEN_DECIMALS = 18

# Slippage (basis points)
DEFAULT_SLIPPAGE_BPS = 200       # 2%
BPS_DENOMINATOR = 10000

# Gas
DEFAULT_PRIORITY_FEE_GWEI = 1.5
GAS_LIMIT_TRANSFER = 21000
GAS_LIMIT_ERC20_TRANSFER = 100000
GAS_LIMIT_APPROVE = 80000
GAS_LIMIT_SWAP = 300000
GAS_LIMIT_MINT = 800000
GAS_LIMIT_DEPLOY = 5000000

# Retry
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 5

# Keystore
KEYSTORE_DIR_DEFAULT = "keystores"
KEYSTORE_KDF_ITERATIONS = 262144  # web3 default

# OpenZeppelin
OZ_VERSION = "5.0.0"
OZ_NPM_TARBALL = f"https://registry.npmjs.org/@openzeppelin/contracts/-/contracts-{OZ_VERSION}.tgz"
OZ_LOCAL_DIR = "contracts/oz"

# Solidity
SOLC_VERSION = "0.8.20"
EVM_VERSION = "paris"

# Loop defaults
DEFAULT_LOOP_INTERVAL = 3600
DEFAULT_ACCOUNT_DELAY = 60
DEFAULT_ACCOUNT_DELAY_JITTER = 120
DEFAULT_INITIAL_STAGGER_MAX = 30

# Per-quest delay so chain state (balance, allowance) settles between txs.
# Random jitter avoids predictable timing patterns.
DEFAULT_QUEST_DELAY = 5.0
DEFAULT_QUEST_JITTER = 3.0

# Random token generation (for deploy)
TOKEN_NAME_PREFIXES = ["Eco", "Crypto", "Mega", "Hyper", "Quantum", "Solar", "Cosmic"]
TOKEN_NAME_SUFFIXES = ["Coin", "Token", "Cash", "Pay", "Swap", "Finance", "Network"]
TOKEN_PREMINT_MIN = 1_000_000
TOKEN_PREMINT_MAX = 100_000_000
