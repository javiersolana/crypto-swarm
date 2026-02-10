"""
Crypto Swarm Intelligence System - Central Configuration
"""
import os

# ─── API Endpoints & Rate Limits ─────────────────────────────────────────────

DEXSCREENER_BASE = "https://api.dexscreener.com"
DEXSCREENER_RATE_LIMIT = 300  # req/min
DEXSCREENER_DELAY = 60 / DEXSCREENER_RATE_LIMIT  # ~0.2s

GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
GECKOTERMINAL_RATE_LIMIT = 10  # req/min
GECKOTERMINAL_DELAY = 60 / GECKOTERMINAL_RATE_LIMIT  # 6s

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_RATE_LIMIT = 30  # req/min
COINGECKO_DELAY = 60 / COINGECKO_RATE_LIMIT  # 2s

REQUEST_TIMEOUT = 10  # seconds
MAX_RETRIES = 3
BACKOFF_BASE = 2  # exponential backoff base

# Smart rate limiting: use DexScreener where possible to reduce GeckoTerminal load
# GeckoTerminal budget: reserve calls for OHLCV (quant phase) on top candidates only
GECKO_OHLCV_BUDGET = 30          # max OHLCV calls per scan cycle
GECKO_CACHE_TTL = 300            # cache pool data for 5 minutes

# ─── Networks to Scan ────────────────────────────────────────────────────────

NETWORKS = {
    "solana": "solana",
    "base": "base",
    "ethereum": "eth",
}

# DexScreener uses different chain IDs
DEXSCREENER_CHAINS = {
    "solana": "solana",
    "base": "base",
    "ethereum": "ethereum",
}

# ─── Scanner (THE SCOUT) Parameters ─────────────────────────────────────────

SCAN_MAX_AGE_DAYS = 7
SCAN_MIN_LIQUIDITY = 30_000      # $30k minimum
SCAN_MAX_LIQUIDITY = 10_000_000  # $10M - include trending tokens
SCAN_MAX_MCAP = 50_000_000       # $50M - include early growth tokens
SCAN_VOL_LIQ_RATIO_MIN = 0.3
SCAN_VOL_LIQ_RATIO_MAX = 50.0    # Trending DEX tokens often have very high vol/liq
SCAN_MAX_CANDIDATES = 150        # 5x more coverage
SCAN_NEW_POOLS_PAGES = 5         # pages to fetch per network (20 per page = 100 tokens)
SCAN_TRENDING_PAGES = 3          # trending pages per network

# ─── Auditor (THE FORENSE) Thresholds ───────────────────────────────────────

AUDIT_MIN_LIQUIDITY = 30_000
AUDIT_MAX_TOP10_HOLDERS_PCT = 20  # percent
AUDIT_MIN_BUY_SELL_RATIO = 0.3   # below = honeypot
AUDIT_MIN_POOL_AGE_HOURS = 6     # too new = suspicious
AUDIT_MIN_TX_COUNT = 100          # minimum transactions
AUDIT_PASS_SCORE = 7              # minimum forense_score to proceed

# ─── Early Entry Detection (v2.0) ──────────────────────────────────────────
# Tokens >6h old that already pumped >100% = too late
EARLY_ENTRY_OLD_PUMP_AGE_HOURS = 6
EARLY_ENTRY_OLD_PUMP_MAX_CHANGE = 100  # %
# Tokens <1h = extra risky but potentially early
EARLY_ENTRY_VERY_NEW_HOURS = 1
# Coordinated pump detection: 1h volume / liquidity ratio
PUMP_DETECT_VOL_LIQ_1H_MAX = 3.0  # ratio >3 in 1h = likely coordinated

# ─── Sentiment (THE NARRATOR) Parameters ────────────────────────────────────

SENTIMENT_MAX_PUMP_PRICE_CHANGE = 500  # % - if price already up 500% = too late
SENTIMENT_MIN_MENTIONS = 2             # minimum web mentions to score

# ─── Technical (THE QUANT) Parameters ───────────────────────────────────────

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
ATH_PROXIMITY_PCT = 10        # within 10% of ATH = don't buy
ACCUMULATION_VOL_INCREASE = 1.5  # volume must be 1.5x average

# ─── Volume Divergence Detection (v2.0) ─────────────────────────────────
# Bearish divergence: price rising while volume declining = unsustainable
VOL_DIVERGENCE_PRICE_UP_PCT = 10     # price up >10%
VOL_DIVERGENCE_VOL_DOWN_RATIO = 0.7  # volume <70% of prior period

# ─── Scoring Weights ────────────────────────────────────────────────────────

WEIGHTS = {
    "scout":    0.15,
    "forense":  0.30,
    "narrator": 0.15,
    "quant":    0.20,
    "executor": 0.20,
}

# ─── Portfolio (THE EXECUTOR) Rules ──────────────────────────────────────────

DEFAULT_CAPITAL = 300          # EUR total monthly
RESERVE_AMOUNT = 50            # EUR reserve
INVESTABLE_CAPITAL = DEFAULT_CAPITAL - RESERVE_AMOUNT  # 250 EUR
MAX_POSITION_PCT = 40          # max % per position
MIN_POSITIONS = 3
MAX_POSITIONS = 5
STOP_LOSS_PCT = -30            # sell all at -30%
TAKE_PROFIT_PCT = 100          # sell 50% at +100%
TAKE_PROFIT_SELL_PCT = 50      # sell this % at take-profit

# ─── File Paths ──────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
SCAN_HISTORY_FILE = os.path.join(DATA_DIR, "scan_history.json")
WEEKLY_REPORTS_DIR = os.path.join(DATA_DIR, "weekly_reports")
