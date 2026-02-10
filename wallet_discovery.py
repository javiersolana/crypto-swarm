#!/usr/bin/env python3
"""
Wallet Discovery - Auto-discover profitable Solana wallets daily.

Data sources (all FREE):
  1. gmgn.ai public API - top trader leaderboards
  2. DexScreener boosted tokens - find early buyers of winners
  3. Birdeye top traders (if API key set)

Filters: PNL >$10k, win_rate >55%, trades >20
Output: data/wallets/discovery_report.json + updates smart_wallets.json

Usage:
  python3 wallet_discovery.py --refresh          # full discovery + update
  python3 wallet_discovery.py --test             # dry run, show what would be added
  python3 wallet_discovery.py --report           # show current discovery report
  python3 wallet_discovery.py --cleanup          # remove inactive wallets first
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import config_alpha
from utils import setup_logging, get_logger, load_json, save_json, now_utc, safe_float
from api_client import DexScreenerClient

log = get_logger("wallet_discovery")

# ─── Discovery Settings ──────────────────────────────────────────────────

DISCOVERY_REPORT_FILE = os.path.join(config.DATA_DIR, "wallets", "discovery_report.json")
MAX_TRACKED_WALLETS = 50  # don't track more than this
MIN_PNL_USD = 10_000      # minimum 30d PNL
MIN_WIN_RATE = 0.55        # 55% win rate
MIN_TRADES = 20            # at least 20 trades
MAX_WALLET_AGE_DAYS = 30   # wallet must have recent activity


def _set_min_pnl(value: float):
    global MIN_PNL_USD
    MIN_PNL_USD = value


def _set_min_wr(value: float):
    global MIN_WIN_RATE
    MIN_WIN_RATE = value


# ─── Solana Address Validation ───────────────────────────────────────────

_BASE58_ALPHABET = set('123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz')


def is_valid_solana_address(addr: str) -> bool:
    """Check if string is a valid Solana base58 address (32-44 chars)."""
    if not addr or not isinstance(addr, str):
        return False
    if len(addr) < 32 or len(addr) > 44:
        return False
    return all(c in _BASE58_ALPHABET for c in addr)


# ─── gmgn.ai Public API ──────────────────────────────────────────────────

class GmgnClient:
    """
    Fetch top trader data from gmgn.ai public API endpoints.
    These are the same endpoints the gmgn.ai frontend uses.
    """

    BASE_URL = "https://gmgn.ai/defi/quotation/v1/rank/sol/swaps"
    WALLET_URL = "https://gmgn.ai/defi/quotation/v1/rank/sol/walletActivities"

    HEADERS = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Referer": "https://gmgn.ai/",
    }

    def __init__(self):
        self._last_request = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < 2.0:  # conservative: 1 req per 2 seconds
            time.sleep(2.0 - elapsed)
        self._last_request = time.time()

    def _fetch_json(self, url: str) -> dict | None:
        self._rate_limit()
        req = urllib.request.Request(url, headers=self.HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            log.warning(f"gmgn.ai request failed: {e}")
            return None
        except Exception as e:
            log.warning(f"gmgn.ai unexpected error: {e}")
            return None

    def get_top_traders(self, timeframe: str = "7d", order_by: str = "pnl",
                        direction: str = "desc", limit: int = 100) -> list[dict]:
        """
        Fetch top traders from gmgn.ai leaderboard.

        timeframe: '1d', '7d', '30d'
        order_by: 'pnl', 'winrate', 'roi'
        """
        url = (
            f"{self.BASE_URL}/{timeframe}"
            f"?orderby={order_by}&direction={direction}"
        )
        data = self._fetch_json(url)
        if not data:
            return []

        # gmgn.ai response structure: {"code": 0, "data": {"rank": [...]}}
        rank_data = data.get("data", {})
        if isinstance(rank_data, dict):
            traders = rank_data.get("rank", [])
        elif isinstance(rank_data, list):
            traders = rank_data
        else:
            traders = []

        log.info(f"gmgn.ai: fetched {len(traders)} traders ({timeframe}, order={order_by})")
        return traders[:limit]

    def get_top_traders_multi(self) -> list[dict]:
        """Fetch from multiple timeframes and merge for better coverage."""
        all_traders = {}

        for timeframe in ["7d", "30d"]:
            for order_by in ["pnl", "winrate"]:
                traders = self.get_top_traders(timeframe=timeframe, order_by=order_by, limit=50)
                for trader in traders:
                    addr = trader.get("wallet_address", "") or trader.get("address", "")
                    if addr and addr not in all_traders:
                        all_traders[addr] = trader

        log.info(f"gmgn.ai: {len(all_traders)} unique traders after merge")
        return list(all_traders.values())


# ─── DexScreener-based Discovery ─────────────────────────────────────────

class DexScreenerDiscovery:
    """Find profitable wallets by tracing early buyers of winning tokens."""

    def __init__(self):
        self.dex = DexScreenerClient()

    def find_winning_tokens(self, min_pump_pct: float = 500, min_liq: float = 50_000) -> list[dict]:
        """Find tokens that pumped significantly - candidates for early buyer analysis."""
        winners = []

        # Strategy 1: Boosted tokens (promoted = high visibility)
        boosted = self.dex.get_boosted_tokens()
        for token in boosted[:30]:
            chain = token.get("chainId", "")
            addr = token.get("tokenAddress", "")
            if chain != "solana" or not addr:
                continue
            pairs = self.dex.get_token_pairs(chain, addr)
            for pair in pairs[:1]:
                change_24h = safe_float(pair.get("priceChange", {}).get("h24"))
                liq = safe_float(pair.get("liquidity", {}).get("usd"))
                if change_24h > min_pump_pct and liq > min_liq:
                    winners.append({
                        "name": pair.get("baseToken", {}).get("name", "?"),
                        "symbol": pair.get("baseToken", {}).get("symbol", "?"),
                        "address": addr,
                        "chain": chain,
                        "price_change_24h": change_24h,
                        "liquidity_usd": liq,
                        "pair_address": pair.get("pairAddress", ""),
                    })

        # Strategy 2: Top token profiles
        profiles = self.dex.get_token_profiles()
        for token in profiles[:20]:
            chain = token.get("chainId", "")
            addr = token.get("tokenAddress", "")
            if chain != "solana" or not addr:
                continue
            # Skip if we already have this token
            if any(w["address"] == addr for w in winners):
                continue
            pairs = self.dex.get_token_pairs(chain, addr)
            for pair in pairs[:1]:
                change_24h = safe_float(pair.get("priceChange", {}).get("h24"))
                liq = safe_float(pair.get("liquidity", {}).get("usd"))
                if change_24h > min_pump_pct and liq > min_liq:
                    winners.append({
                        "name": pair.get("baseToken", {}).get("name", "?"),
                        "symbol": pair.get("baseToken", {}).get("symbol", "?"),
                        "address": addr,
                        "chain": chain,
                        "price_change_24h": change_24h,
                        "liquidity_usd": liq,
                        "pair_address": pair.get("pairAddress", ""),
                    })

        log.info(f"DexScreener: found {len(winners)} winning tokens")
        return winners


# ─── Solana RPC Discovery ────────────────────────────────────────────────

class SolanaRPCDiscovery:
    """
    Discover smart wallets by finding top holders of trending Solana tokens.
    Uses DexScreener (trending tokens) + free Solana RPC (top holders).
    No API key required.
    """

    # Program/system addresses to exclude (not real wallets)
    EXCLUDED_ADDRESSES = {
        "11111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        "So11111111111111111111111111111111111111112",
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
        "TSWAPaqyCSx2KABk68Shruf4rp7CxcNi8hAsbdwmHbN",
        "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",
        "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
        "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD",
        "SSwpkEEcbUqx4vtoEByFjSkhKdCT862DNVb52nZg1UZ",
        "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS",
        "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY",
    }

    def __init__(self):
        # Prefer Helius RPC (10 req/s) over free public RPC (~2 req/s)
        helius_key = getattr(config_alpha, 'HELIUS_API_KEY', '') or ''
        if helius_key:
            self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
            self._delay = 0.15  # ~6 req/s (conservative for Helius free tier)
            log.info("RPC Discovery: using Helius RPC")
        else:
            self.rpc_url = getattr(config_alpha, 'SOLANA_RPC_URL', None) or \
                           "https://api.mainnet-beta.solana.com"
            self._delay = 2.0  # very conservative for free public RPC
            log.info("RPC Discovery: using public Solana RPC (slower)")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._last_request = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request = time.time()

    def _rpc(self, method: str, params: list) -> dict | None:
        """Make a JSON-RPC call to Solana with retry on 429."""
        for attempt in range(3):
            self._rate_limit()
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                resp = self.session.post(self.rpc_url, json=payload, timeout=15)
                if resp.status_code == 429:
                    wait = (attempt + 1) * 2
                    log.debug(f"RPC 429, backing off {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    log.warning(f"RPC error ({method}): {data['error']}")
                    return None
                return data.get("result")
            except Exception as e:
                log.warning(f"RPC call failed ({method}): {e}")
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                return None
        return None

    def get_top_holder_wallets(self, mint_address: str) -> list[str]:
        """Get owner wallet addresses of the largest token account holders."""
        # Step 1: Get 20 largest token accounts for this mint
        result = self._rpc("getTokenLargestAccounts", [mint_address])
        if not result:
            return []

        accounts = result.get("value", [])
        token_addrs = [a["address"] for a in accounts[:20] if a.get("address")]
        if not token_addrs:
            return []

        # Step 2: Batch resolve token accounts → owner wallet addresses
        result = self._rpc("getMultipleAccounts", [
            token_addrs, {"encoding": "jsonParsed"}
        ])
        if not result:
            return []

        wallets = []
        for acc_info in result.get("value", []):
            if not acc_info:
                continue
            try:
                owner = acc_info["data"]["parsed"]["info"]["owner"]
                if (owner
                        and is_valid_solana_address(owner)
                        and owner not in self.EXCLUDED_ADDRESSES):
                    wallets.append(owner)
            except (KeyError, TypeError):
                continue

        return wallets

    def discover(self) -> list[dict]:
        """
        Main discovery pipeline:
        1. Get trending Solana tokens from DexScreener
        2. For each token, find top holder wallets via Solana RPC
        3. Cross-reference: wallets holding multiple trending tokens = smart money
        """
        dex = DexScreenerClient()

        # ── Collect trending Solana token mints ──
        trending_mints = []
        seen = set()

        try:
            boosted = dex.get_boosted_tokens()
            for t in boosted:
                addr = t.get("tokenAddress", "")
                if t.get("chainId") == "solana" and addr and addr not in seen:
                    trending_mints.append(addr)
                    seen.add(addr)
        except Exception as e:
            log.warning(f"DexScreener boosted fetch failed: {e}")

        try:
            profiles = dex.get_token_profiles()
            for t in profiles:
                addr = t.get("tokenAddress", "")
                if t.get("chainId") == "solana" and addr and addr not in seen:
                    trending_mints.append(addr)
                    seen.add(addr)
        except Exception as e:
            log.warning(f"DexScreener profiles fetch failed: {e}")

        # Cap to avoid RPC rate limits on free tier
        trending_mints = trending_mints[:20]
        log.info(f"RPC Discovery: analyzing top holders of {len(trending_mints)} trending tokens")

        if not trending_mints:
            log.warning("RPC Discovery: no trending tokens found from DexScreener")
            return []

        # ── For each token, find top holder wallets ──
        wallet_tokens = {}  # wallet_addr -> count of trending tokens held

        for i, mint in enumerate(trending_mints):
            holders = self.get_top_holder_wallets(mint)
            for wallet in holders:
                wallet_tokens[wallet] = wallet_tokens.get(wallet, 0) + 1

            if holders:
                log.debug(f"  Token {i + 1}/{len(trending_mints)}: {len(holders)} holders")

        log.info(f"RPC Discovery: {len(wallet_tokens)} unique wallets found across "
                 f"{len(trending_mints)} tokens")

        # ── Build results ──
        discovered = []
        for addr, count in wallet_tokens.items():
            discovered.append({
                "address": addr,
                "chain": "solana",
                "trending_tokens_held": count,
                "source": "rpc_top_holders",
                "discovered_at": now_utc().isoformat(),
            })

        # Sort by cross-token count descending
        discovered.sort(key=lambda w: w["trending_tokens_held"], reverse=True)

        multi = sum(1 for w in discovered if w["trending_tokens_held"] >= 2)
        log.info(f"RPC Discovery: {multi} wallets hold 2+ trending tokens")

        return discovered


# ─── Wallet Filtering & Deduplication ──────────────────────────────────────

def parse_gmgn_trader(trader: dict) -> dict | None:
    """Parse a gmgn.ai trader entry into our standard format."""
    addr = trader.get("wallet_address", "") or trader.get("address", "")
    if not addr:
        return None

    # gmgn.ai fields vary by endpoint; handle multiple formats
    pnl = safe_float(trader.get("pnl")) or safe_float(trader.get("realized_profit"))
    win_rate = safe_float(trader.get("winrate")) or safe_float(trader.get("win_rate"))
    trades = (
        int(safe_float(trader.get("total_trades", 0)))
        or int(safe_float(trader.get("buy", 0))) + int(safe_float(trader.get("sell", 0)))
    )
    roi = safe_float(trader.get("roi")) or safe_float(trader.get("pnl_percentage"))

    # Normalize win_rate (might be 0-1 or 0-100)
    if win_rate > 1:
        win_rate = win_rate / 100.0

    return {
        "address": addr,
        "chain": "solana",
        "pnl_usd": pnl,
        "win_rate": win_rate,
        "trades": trades,
        "roi_pct": roi * 100 if roi <= 1 else roi,  # normalize to percentage
        "source": "gmgn.ai",
        "discovered_at": now_utc().isoformat(),
    }


def filter_wallets(raw_wallets: list[dict]) -> list[dict]:
    """Apply quality filters to discovered wallets."""
    filtered = []
    for w in raw_wallets:
        if not w or not w.get("address"):
            continue

        # Validate address format
        if not is_valid_solana_address(w["address"]):
            continue

        source = w.get("source", "")

        if source == "rpc_top_holders":
            # RPC-discovered wallets: no PNL data available.
            # Quality signal = number of trending tokens held.
            # Any wallet holding a top position in a trending token is worth tracking.
            filtered.append(w)
        else:
            # Standard PNL-based filtering (gmgn.ai, birdeye, etc.)
            pnl = w.get("pnl_usd", 0)
            win_rate = w.get("win_rate", 0)
            trades = w.get("trades", 0)

            if pnl < MIN_PNL_USD:
                continue
            if win_rate < MIN_WIN_RATE:
                continue
            if trades < MIN_TRADES:
                continue

            filtered.append(w)

    # Sort: wallets with PNL data first (by PNL), then RPC wallets (by token count)
    def sort_key(w):
        if w.get("source") == "rpc_top_holders":
            return (0, w.get("trending_tokens_held", 0))
        return (1, w.get("pnl_usd", 0))

    filtered.sort(key=sort_key, reverse=True)

    # Cap at max tracked
    filtered = filtered[:MAX_TRACKED_WALLETS]

    rpc_count = sum(1 for w in filtered if w.get("source") == "rpc_top_holders")
    pnl_count = len(filtered) - rpc_count
    log.info(
        f"Filtered: {len(filtered)} wallets "
        f"({pnl_count} PNL-verified, {rpc_count} RPC-discovered)"
    )
    return filtered


def deduplicate_wallets(wallets: list[dict], existing: dict) -> list[dict]:
    """Remove wallets that are already being tracked."""
    existing_addrs = set(existing.keys())
    new = [w for w in wallets if w["address"] not in existing_addrs]
    if len(wallets) != len(new):
        log.info(f"Deduplication: {len(wallets) - len(new)} already tracked, {len(new)} new")
    return new


# ─── Update Tracked Wallets ──────────────────────────────────────────────

def update_tracked_wallets(new_wallets: list[dict], dry_run: bool = False) -> tuple[int, int]:
    """
    Update the smart_wallets.json with newly discovered wallets.
    Returns (added, removed) counts.
    """
    from alpha.smart_wallet_tracker import WalletDB
    db = WalletDB()
    existing = db.load_wallets()

    # Deduplicate
    new_wallets = deduplicate_wallets(new_wallets, existing)

    if not new_wallets:
        log.info("No new wallets to add.")
        return 0, 0

    # Check capacity
    current_count = len(existing)
    space = MAX_TRACKED_WALLETS - current_count
    to_add = new_wallets[:max(space, 10)]  # always allow at least 10 new

    if dry_run:
        log.info(f"[DRY RUN] Would add {len(to_add)} wallets:")
        for w in to_add:
            source = w.get("source", "unknown")
            if source == "rpc_top_holders":
                log.info(f"  {w['address'][:20]}... trending_tokens={w.get('trending_tokens_held', 0)} "
                         f"source={source}")
            else:
                log.info(f"  {w['address'][:20]}... PNL=${w.get('pnl_usd', 0):,.0f} "
                         f"WR={w.get('win_rate', 0)*100:.0f}%")
        return len(to_add), 0

    added = 0
    for w in to_add:
        source = w.get("source", "unknown")
        tokens_held = w.get("trending_tokens_held", 0)

        if source == "rpc_top_holders":
            label = f"rpc_{tokens_held}tok_{added+1}"
            notes = (f"Auto-discovered via RPC: top holder of "
                     f"{tokens_held} trending token(s), "
                     f"Source={source}")
        else:
            label = f"gmgn_{w.get('win_rate', 0)*100:.0f}wr_{added+1}"
            notes = (f"Auto-discovered: PNL=${w.get('pnl_usd', 0):,.0f}, "
                     f"WR={w.get('win_rate', 0)*100:.0f}%, "
                     f"Trades={w.get('trades', 0)}, "
                     f"Source={source}")

        success = db.add_wallet(
            address=w["address"],
            chain=w.get("chain", "solana"),
            label=label,
            notes=notes,
            pnl=w.get("pnl_usd", 0),
            win_rate=w.get("win_rate", 0),
        )
        if success:
            added += 1

    log.info(f"Added {added} new wallets to tracking")
    return added, 0


# ─── Discovery Report ────────────────────────────────────────────────────

def save_discovery_report(wallets: list[dict], source: str = "mixed"):
    """Save the discovery results for review."""
    os.makedirs(os.path.dirname(DISCOVERY_REPORT_FILE), exist_ok=True)
    report = {
        "timestamp": now_utc().isoformat(),
        "source": source,
        "total_discovered": len(wallets),
        "filters": {
            "min_pnl_usd": MIN_PNL_USD,
            "min_win_rate": MIN_WIN_RATE,
            "min_trades": MIN_TRADES,
        },
        "wallets": wallets,
    }
    save_json(DISCOVERY_REPORT_FILE, report)
    log.info(f"Discovery report saved: {DISCOVERY_REPORT_FILE}")


def print_discovery_report():
    """Print the latest discovery report."""
    report = load_json(DISCOVERY_REPORT_FILE)
    if not report:
        print("No discovery report found. Run --refresh first.")
        return

    ts = report.get("timestamp", "unknown")
    wallets = report.get("wallets", [])
    print(f"\n{'='*80}")
    print(f"WALLET DISCOVERY REPORT")
    print(f"Generated: {ts}")
    print(f"Total wallets: {len(wallets)}")
    print(f"{'='*80}\n")

    print(f"{'#':>3}  {'Address':22}  {'PNL ($)':>12}  {'Win Rate':>9}  {'Tokens':>7}  {'Source'}")
    print(f"{'-'*3}  {'-'*22}  {'-'*12}  {'-'*9}  {'-'*7}  {'-'*15}")

    for i, w in enumerate(wallets[:50], 1):
        addr = w.get("address", "?")[:20] + "..."
        source = w.get("source", "?")

        if source == "rpc_top_holders":
            tokens_held = w.get("trending_tokens_held", 0)
            print(f"{i:>3}  {addr:22}  {'n/a':>12}  {'n/a':>9}  {tokens_held:>7}  {source}")
        else:
            pnl = w.get("pnl_usd", 0)
            wr = w.get("win_rate", 0) * 100
            trades = w.get("trades", 0)
            print(f"{i:>3}  {addr:22}  ${pnl:>11,.0f}  {wr:>8.1f}%  {trades:>7}  {source}")

    print(f"\nReport file: {DISCOVERY_REPORT_FILE}")


# ─── Main Discovery Pipeline ─────────────────────────────────────────────

def run_discovery(dry_run: bool = False) -> tuple[int, int]:
    """
    Full discovery pipeline:
    1. Primary: DexScreener trending tokens + Solana RPC top holders
    2. Fallback: gmgn.ai top traders (if their API is accessible)
    3. Filter by quality
    4. Update tracked wallets
    """
    start_time = time.time()
    log.info("Starting wallet discovery pipeline...")

    all_raw = []

    # ── Source 1 (Primary): Solana RPC - top holders of trending tokens ──
    try:
        rpc_disc = SolanaRPCDiscovery()
        rpc_wallets = rpc_disc.discover()
        all_raw.extend(rpc_wallets)
        log.info(f"Solana RPC: {len(rpc_wallets)} wallets discovered")
    except Exception as e:
        log.warning(f"Solana RPC discovery failed: {e}")

    # ── Source 2 (Fallback): gmgn.ai top traders ──
    try:
        gmgn = GmgnClient()
        traders = gmgn.get_top_traders_multi()
        parsed = [parse_gmgn_trader(t) for t in traders]
        parsed = [p for p in parsed if p is not None]
        all_raw.extend(parsed)
        if parsed:
            log.info(f"gmgn.ai: {len(parsed)} traders parsed")
        else:
            log.info("gmgn.ai: 0 traders (API may require browser access)")
    except Exception as e:
        log.warning(f"gmgn.ai discovery failed: {e}")

    if not all_raw:
        log.warning("No wallets discovered from any source.")
        save_discovery_report([], source="none")
        return 0, 0

    # Filter
    filtered = filter_wallets(all_raw)

    # Save report
    sources = set(w.get("source", "unknown") for w in filtered)
    save_discovery_report(filtered, source="+".join(sorted(sources)))

    # Update tracked wallets
    added, removed = update_tracked_wallets(filtered, dry_run=dry_run)

    elapsed = time.time() - start_time
    log.info(
        f"Discovery complete in {elapsed:.0f}s: "
        f"{len(all_raw)} raw -> {len(filtered)} filtered -> {added} added"
    )

    return added, removed


# ─── CLI ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Wallet Discovery - Auto-discover profitable Solana wallets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 wallet_discovery.py --refresh          # discover + add new wallets
  python3 wallet_discovery.py --test             # dry run (show only)
  python3 wallet_discovery.py --report           # view last report
  python3 wallet_discovery.py --cleanup          # remove inactive wallets

Cron setup (daily at 6am UTC):
  0 6 * * * cd ~/crypto-swarm && python3 wallet_discovery.py --refresh >> logs/discovery.log 2>&1
        """,
    )
    parser.add_argument("--refresh", action="store_true", help="Run full discovery + update")
    parser.add_argument("--test", action="store_true", help="Dry run (show what would happen)")
    parser.add_argument("--report", action="store_true", help="Show latest discovery report")
    parser.add_argument("--cleanup", action="store_true", help="Remove inactive wallets first")
    parser.add_argument("--min-pnl", type=float, default=None,
                        help=f"Min PNL filter (default: ${MIN_PNL_USD:,})")
    parser.add_argument("--min-wr", type=float, default=None,
                        help=f"Min win rate %% (default: {MIN_WIN_RATE*100:.0f})")

    args = parser.parse_args()
    setup_logging()

    if args.min_pnl is not None:
        _set_min_pnl(args.min_pnl)
    if args.min_wr is not None:
        _set_min_wr(args.min_wr / 100.0)

    if args.report:
        print_discovery_report()
        return

    if args.cleanup:
        from alpha.smart_wallet_tracker import WalletDB
        db = WalletDB()
        removed = db.remove_inactive_wallets()
        print(f"Cleaned up {removed} inactive wallets.")
        if not args.refresh:
            return

    if args.test:
        log.info("DRY RUN mode - no changes will be made")
        added, removed = run_discovery(dry_run=True)
        print(f"\n[DRY RUN] Would add {added} wallets, remove {removed}")
        print_discovery_report()
        return

    if args.refresh:
        added, removed = run_discovery(dry_run=False)
        print(f"\nDiscovery complete: {added} added, {removed} removed")
        print_discovery_report()
        return

    # Default: show help
    parser.print_help()


if __name__ == "__main__":
    main()
