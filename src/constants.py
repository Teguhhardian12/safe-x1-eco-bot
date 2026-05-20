"""Static constants: addresses, URLs, fee tiers, defaults.

X1 testnet placeholders. Replace with actual addresses before running on testnet.
"""

# Token addresses (X1 testnet) - PLACEHOLDER, verify before use
WXC_ADDRESS = "0x0000000000000000000000000000000000000000"
USDT_ADDRESS = "0x0000000000000000000000000000000000000000"

# Router addresses (X1 testnet) - PLACEHOLDER, verify before use
SWAP_ROUTER_ADDRESS = "0x0000000000000000000000000000000000000000"
MINT_ROUTER_ADDRESS = "0x0000000000000000000000000000000000000000"
DEPLOY_ROUTER_ADDRESS = "0x0000000000000000000000000000000000000000"

# Uniswap V3 fee tiers (parts per million)
FEE_TIER_LOW = 500       # 0.05%
FEE_TIER_MEDIUM = 3000   # 0.3%
FEE_TIER_HIGH = 10000    # 1%
DEFAULT_FEE_TIER = FEE_TIER_MEDIUM

# Uniswap V3 tick range (full range)
TICK_LOWER = -887220
TICK_UPPER = 887220

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
DEFAULT_ACCOUNT_DELAY = 10

# Random token generation (for deploy)
TOKEN_NAME_PREFIXES = ["Eco", "Crypto", "Mega", "Hyper", "Quantum", "Solar", "Cosmic"]
TOKEN_NAME_SUFFIXES = ["Coin", "Token", "Cash", "Pay", "Swap", "Finance", "Network"]
TOKEN_PREMINT_MIN = 1_000_000
TOKEN_PREMINT_MAX = 100_000_000
