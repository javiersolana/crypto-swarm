"""
Alpha Hunter System - Configuration for Advanced Data Sources
Fill in API keys below. All APIs have free tiers.
"""
import os

# ─── Helius API (Solana Enhanced Transactions) ────────────────────────────
# Free tier: ~100k credits/day (enough for wallet tracking)
# Sign up: https://www.helius.dev/
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""
HELIUS_API_URL = "https://api.helius.xyz"
HELIUS_RATE_LIMIT = 10  # req/sec on free tier
HELIUS_DELAY = 1.0 / HELIUS_RATE_LIMIT

# Helius Enhanced WebSocket
HELIUS_WS_URL = f"wss://atlas-mainnet.helius-rpc.com?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else ""

# WebSocket settings
WS_RECONNECT_BASE_DELAY = 5     # seconds, initial reconnect delay
WS_RECONNECT_MAX_DELAY = 300    # seconds, max reconnect delay (5 min)
WS_PING_INTERVAL = 30           # seconds, keepalive ping
WS_FALLBACK_POLL_INTERVAL = 600 # seconds, polling fallback when WS active (10 min)
POLLING_FAST_INTERVAL = 90      # seconds, fast polling when WS permanently failed

# ─── Birdeye API (Solana DEX Data + Token Security) ──────────────────────
# Free tier available after registration
# Sign up: https://birdeye.so/
BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "")
BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_DELAY = 0.5

# ─── Jupiter API (Solana DEX Aggregator - FREE, no key needed) ───────────
JUPITER_PRICE_API = "https://price.jup.ag/v6/price"
JUPITER_TOKEN_LIST = "https://token.jup.ag/all"
JUPITER_DELAY = 0.2

# ─── CryptoPanic API (News + Sentiment) ──────────────────────────────────
# Free tier: limited requests
# Sign up: https://cryptopanic.com/developers/api/
CRYPTOPANIC_API_KEY = os.environ.get("CRYPTOPANIC_API_KEY", "")
CRYPTOPANIC_BASE = "https://cryptopanic.com/api/free/v1"
CRYPTOPANIC_DELAY = 2.0

# ─── GitHub API (Repo Activity) ──────────────────────────────────────────
# Free: 60 req/hr (unauthenticated) or 5000 req/hr (with token)
# Create token: https://github.com/settings/tokens
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"
GITHUB_DELAY = 1.0

# ─── Solana Public RPC (Free fallback) ────────────────────────────────────
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_RPC_DELAY = 0.5

# ─── Etherscan/Basescan APIs (EVM Wallet Tracking) ───────────────────────
# Free tier: 5 req/sec
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
BASESCAN_API_KEY = os.environ.get("BASESCAN_API_KEY", "")
ETHERSCAN_BASE = "https://api.etherscan.io/api"
BASESCAN_BASE = "https://api.basescan.org/api"

# ─── Smart Wallet Tracking Settings ──────────────────────────────────────

# How often to check wallets (seconds)
WALLET_CHECK_INTERVAL = 120  # 2 minutes

# Minimum wallet win rate to consider "smart"
WALLET_MIN_WIN_RATE = 0.6  # 60% of trades profitable

# Minimum number of profitable trades to qualify
WALLET_MIN_PROFITABLE_TRADES = 5

# How far back to look for transactions (hours)
WALLET_LOOKBACK_HOURS = 2

# Maximum age of token for "new buy" alert (hours)
WALLET_NEW_TOKEN_MAX_AGE_HOURS = 24

# Minimum buy size to track (USD)
WALLET_MIN_BUY_USD = 500

# ─── Triple Confirmation Settings ────────────────────────────────────────

# Signal weights for triple confirmation scoring
ALPHA_WEIGHTS = {
    "smart_wallet_buying": 3.0,
    "multiple_smart_wallets": 2.0,  # bonus if 2+ wallets buying
    "github_active": 2.0,
    "news_positive": 1.5,
    "news_trending": 2.0,
    "volume_building": 3.0,        # volume up, price stable
    "fresh_token": 1.5,            # <6h old
    "holder_growth": 2.0,
    "social_mentions_growing": 1.5,
}

# Minimum alpha score to trigger high-priority alert
ALPHA_ALERT_THRESHOLD = 7.0

# ─── Known Smart Wallets (Solana) ────────────────────────────────────────
# These are well-known profitable Solana wallets from public tracking sites.
# Format: {"address": "description"}
# You can add more by running: python3 alpha/smart_wallet_tracker.py --discover
SMART_WALLETS_SOLANA = {
    # Add wallet addresses here as you discover them
    # Example format:
    # "WaLLetAdDrEsS123...": "High win-rate memecoin trader #1",
}

# ─── Known Smart Wallets (Base/EVM) ──────────────────────────────────────
SMART_WALLETS_EVM = {
    # Add EVM wallet addresses here
}

# ─── File Paths ──────────────────────────────────────────────────────────
ALPHA_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WALLETS_DB_FILE = os.path.join(ALPHA_DATA_DIR, "wallets", "smart_wallets.json")
WALLET_TRADES_FILE = os.path.join(ALPHA_DATA_DIR, "wallets", "wallet_trades.json")
ALPHA_ALERTS_FILE = os.path.join(ALPHA_DATA_DIR, "alpha_alerts.json")
SOCIAL_CACHE_FILE = os.path.join(ALPHA_DATA_DIR, "social_cache.json")
