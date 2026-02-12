"""
Crypto Swarm Intelligence System - Central Configuration (v8.8 Sweet Spot)
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

# ─── Rugcheck.xyz (Solana token security - FREE, no API key) ──────────────
RUGCHECK_BASE = "https://api.rugcheck.xyz"
RUGCHECK_DELAY = 2.0
RUGCHECK_MAX_SCORE = 5000       # reject tokens above this score
RUGCHECK_REJECT_DANGER = True   # reject if any "danger" level risk

REQUEST_TIMEOUT = 10  # seconds
MAX_RETRIES = 3
BACKOFF_BASE = 2  # exponential backoff base

# Smart rate limiting: use DexScreener where possible to reduce GeckoTerminal load
# GeckoTerminal budget: reserve calls for OHLCV (quant phase) on top candidates only
GECKO_OHLCV_BUDGET = 30          # max OHLCV calls per scan cycle
GECKO_CACHE_TTL = 300            # cache pool data for 5 minutes

# Per-host rate limits (req/min) — shared across all APIClient instances
HOST_RATE_LIMITS = {
    "api.geckoterminal.com": 30,    # strict
    "api.dexscreener.com": 300,     # aggressive
    "api.coingecko.com": 30,
    "api.rugcheck.xyz": 30,         # free API, be polite
    "_default": 60,
}

# ─── Networks to Scan ────────────────────────────────────────────────────────
# Ordered by priority: Solana and Base have faster/cheaper txns = better for memecoins

NETWORKS = {
    "solana": "solana",
    "base": "base",
    "ethereum": "eth",
}

# Priority networks get full scan; lower-priority ones get reduced pages
# v8.8: Base first — missed_opportunities.json showed 100% of +1000% gems were on Base
PRIORITY_NETWORKS = ["base", "solana"]

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
SCAN_MAX_CANDIDATES = 100        # reduced from 150 - fewer candidates = faster audit
SCAN_NEW_POOLS_PAGES = 2         # reduced from 5 - pages 3-5 are too old for memecoin alpha
SCAN_TRENDING_PAGES = 2          # reduced from 3 - top 2 pages have the most volume
SCAN_NEW_POOLS_PAGES_LOW_PRIO = 1  # ethereum gets just 1 page (slower chain, fewer memecoins)
SCAN_TRENDING_PAGES_LOW_PRIO = 1

# ─── Parallelization ────────────────────────────────────────────────────────
AUDIT_PARALLEL_WORKERS = 8       # parallel token audits
AUDIT_TRADE_CHECK_MIN_SCORE = 5.0  # only fetch trades for tokens scoring above this

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

# ─── Pump.fun Detection ──────────────────────────────────────────────────────
PUMP_FUN_MIN_LIQUIDITY = 15_000   # $15k (graduated from bonding curve)
PUMP_FUN_MAX_LIQUIDITY = 80_000   # $80k (recently graduated)
PUMP_FUN_MAX_AGE_DAYS = 2         # recently migrated

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
AUDIT_BLACKLIST_FILE = os.path.join(DATA_DIR, "audit_blacklist.json")
AUDIT_BLACKLIST_TTL = 3600  # 60 minutes — rejected tokens are skipped for this long
WALLET_WS_FALLBACK_INTERVAL = 600  # 10 min polling when WebSocket is primary

# ─── Paper Trading (v8.8 Sweet Spot — Educated Sniper) ────────────────
PAPER_TRADE_AMOUNT_SOL = 0.05          # SOL per trade — default (Tier A)
PAPER_TP_PCT = 50                      # Legacy TP (used as fallback)
PAPER_SL_PCT = -12                     # v8.8: -12% SL (was -18%). With ~3% slippage = ~-15% real loss.
                                        # Day 1 data: -25% SL still exited at -28 to -33%. Cut faster.
PAPER_TRAILING_ACTIVATION_PCT = 100    # Legacy trailing activation (unused in v7.0)
PAPER_EXIT_CHECK_INTERVAL = 45         # seconds between exit checks
PAPER_TRADES_FILE = os.path.join(DATA_DIR, "paper_trades.json")
PAPER_MAX_OPEN_TRADES = 5              # v8.8: was 20. With 0.5 SOL, max 5 concurrent positions.

# v8.8 Tiered Position Sizing — confidence-based amount
PAPER_TIER_A_SOL = 0.05                # Whale + forense>=8 + RSI<60 (highest confidence)
PAPER_TIER_B_SOL = 0.025               # forense>=8 + RSI<70 + h1_vol>$15k
PAPER_TIER_C_SOL = 0.015               # forense>=7.5 + h1_vol>$10k + chain=base

# v8.8 Moonbag settings (retuned for achievable targets)
PAPER_TP1_PCT = 40                     # v8.8: was 80%. Day 1 max upside was +35%. 40% is reachable.
PAPER_TP1_SELL_FRACTION = 0.60         # v8.8: was 0.50. Sell 60% at TP1 to secure more capital.
PAPER_MOONBAG_TRAILING_PCT = 15        # v8.8: was 20%. Tighter trailing on moonbag.
PAPER_EMERGENCY_RUGCHECK = True        # Close immediately on Rugcheck "Danger"

# v8.0 Realistic simulation
PAPER_FEE_SOL = 0.006                  # Simulated fees per trade (0.003 entry + 0.003 exit)
PAPER_SLIPPAGE_PCT = 5.0               # v8.8: was 3.0. Real data showed 3-8% slippage on DEX exits.

# v8.8 Anti-Fomo: RELAXED but SMARTER
AUDIT_MAX_RSI_1M = 70                  # v8.8: was 65. RSI 65 rejected BIG (+5.4%). 70 still catches
                                        # GIRAFFLUNA (RSI 75 → -98%). Sweet spot between safety & opportunity.
RSI_PERIOD_ANTIFOMO = 9                # v8.8: NEW. Was hardcoded 14. RSI(9) on 1-min candles is more
                                        # responsive to real peaks vs lagging momentum from 14+ min ago.
AUDIT_MIN_VOLUME_H1 = 10_000          # v8.8: was $50k — THE KILLER FILTER. All 17 tokens that pumped
                                        # +1000% had h1_vol between $26k-$48k. $10k filters dead tokens
                                        # (h1_vol <$5k) without blocking fresh gems.
AUDIT_MIN_VOL_LIQ_RATIO_H1 = 0.3      # v8.8: NEW. h1_volume / liquidity >= 0.3 = real traction.
                                        # SPIRIT had ratio 1.13 ($35k/$31k). Dead tokens have ratio 0.01.

# v8.8 Break-even trailing stop
PAPER_BREAKEVEN_PCT = 20               # v8.8: was 15%. Memecoins retrace 15-20% in healthy runs.
                                        # At +20% there's confirmed momentum.

# v8.8 Minimum alpha score to open any trade (prevents blind entries)
MIN_ALPHA_SCORE_FOR_TRADE = 1.0        # Day 1: all 4 losses had alpha_score 0.83-2.5 and signal_count=0.
                                        # Require at least 1.0 alpha to prevent pure-forense entries.

# v8.0 Anti-rug: minimum token age
AUDIT_MIN_TOKEN_AGE_SECONDS = 180      # 3 minutes — reject instant rugs

# v8.1 Anti-fragility: liquidity depth filters
AUDIT_MIN_LIQ_MCAP_RATIO = 0.10       # Reject if liq < 10% of mcap (price gap risk)
AUDIT_MAX_CREATOR_PCT = 15.0           # Reject if creator/insiders hold >15% supply
WHALE_MIN_LIQUIDITY = 30_000           # Minimum liq for whale fast-track trades ($)

# v8.8 Whale Confirmation (relaxed from v8.6)
DOUBLE_WHALE_MIN_WALLETS = 1           # v8.8: was 2. Requiring 2 whales is too rare in practice.
                                        # 1 whale + forense>=7.0 is sufficient signal.
SOL_TREND_MAX_DROP_PCT = -2.0          # Block ALL buys if SOL dropped more than 2% in last hour
SOL_USDC_PAIR_ADDRESS = "So11111111111111111111111111111111111111112"  # Wrapped SOL mint

# v8.6 Missed Opportunities Tracker
MISSED_OPPORTUNITIES_FILE = os.path.join(DATA_DIR, "missed_opportunities.json")
MISSED_OPP_CHECK_INTERVAL = 300        # Check rejected tokens every 5 minutes
MISSED_OPP_PUMP_THRESHOLD = 50         # Log if token pumped >50% after rejection
MISSED_OPP_TTL = 86400                 # Track for 24 hours
