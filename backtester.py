#!/usr/bin/env python3
"""
Backtester - Validate strategy by analyzing historical alert performance.

Loads past alerts from data/alerts.json, fetches current prices from DexScreener,
and calculates what would have happened if you bought at each alert price.

Usage:
  python3 backtester.py                      # all alerts
  python3 backtester.py --last-7-days        # only recent alerts
  python3 backtester.py --last-30-days       # last month
  python3 backtester.py --min-score 7.5      # only high-score alerts
  python3 backtester.py --export results.json # export detailed results
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import config_alpha
from utils import setup_logging, get_logger, load_json, save_json, now_utc, safe_float
from api_client import DexScreenerClient

log = get_logger("backtester")

ALERTS_FILE = os.path.join(config.DATA_DIR, "alerts.json")
ALPHA_ALERTS_FILE = config_alpha.ALPHA_ALERTS_FILE
BACKTEST_REPORT_FILE = os.path.join(config.DATA_DIR, "backtest_report.json")


# ─── Load Historical Alerts ──────────────────────────────────────────────

def load_historical_alerts(days: int = None, min_score: float = 0) -> list[dict]:
    """
    Load alerts from data/alerts.json and data/alpha_alerts.json.
    Optionally filter by age and minimum score.
    """
    alerts = []

    # Load standard alerts
    standard = load_json(ALERTS_FILE)
    if isinstance(standard, list):
        alerts.extend(standard)

    # Load alpha alerts
    alpha = load_json(ALPHA_ALERTS_FILE)
    if isinstance(alpha, list):
        alerts.extend(alpha)

    if not alerts:
        log.warning("No historical alerts found. Run the monitor first to generate alerts.")
        return []

    log.info(f"Loaded {len(alerts)} raw alerts")

    # Deduplicate by address
    seen_addrs = set()
    unique = []
    for alert in alerts:
        addr = (alert.get("address") or alert.get("token_address") or "").lower()
        if addr and addr not in seen_addrs:
            seen_addrs.add(addr)
            unique.append(alert)

    alerts = unique

    # Filter by age
    if days:
        cutoff = now_utc() - timedelta(days=days)
        filtered = []
        for alert in alerts:
            ts = alert.get("timestamp", "")
            if not ts:
                filtered.append(alert)  # keep alerts without timestamp
                continue
            try:
                alert_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if alert_time >= cutoff:
                    filtered.append(alert)
            except (ValueError, TypeError):
                filtered.append(alert)
        alerts = filtered
        log.info(f"After {days}-day filter: {len(alerts)} alerts")

    # Filter by minimum score
    if min_score > 0:
        alerts = [
            a for a in alerts
            if safe_float(a.get("composite_score") or a.get("alpha_score", 0)) >= min_score
        ]
        log.info(f"After min_score={min_score} filter: {len(alerts)} alerts")

    return alerts


# ─── Fetch Current Prices ─────────────────────────────────────────────────

def fetch_current_prices(alerts: list[dict]) -> dict:
    """
    Fetch current prices for all alerted tokens via DexScreener batch API.
    Returns: {token_address_lower: {price_usd, liquidity_usd, volume_24h, ...}}
    """
    dex = DexScreenerClient()
    price_map = {}

    # Group by chain
    by_chain = {}
    for alert in alerts:
        addr = (alert.get("address") or alert.get("token_address") or "").strip()
        chain = alert.get("network", alert.get("chain", "solana")).lower()
        # Normalize chain names
        if chain in ("eth", "ethereum"):
            chain = "ethereum"
        elif chain == "sol":
            chain = "solana"
        if addr:
            by_chain.setdefault(chain, set()).add(addr)

    for chain, addresses in by_chain.items():
        addr_list = list(addresses)
        dex_chain = config.DEXSCREENER_CHAINS.get(chain, chain)

        # Batch in groups of 30
        for i in range(0, len(addr_list), 30):
            batch = addr_list[i:i+30]
            pairs = dex.get_tokens_batch(dex_chain, batch)

            for pair in pairs:
                base_addr = pair.get("baseToken", {}).get("address", "").lower()
                if base_addr and base_addr not in price_map:
                    price_map[base_addr] = {
                        "price_usd": safe_float(pair.get("priceUsd")),
                        "liquidity_usd": safe_float(pair.get("liquidity", {}).get("usd")),
                        "volume_24h": safe_float(pair.get("volume", {}).get("h24")),
                        "mcap": safe_float(pair.get("marketCap")),
                        "price_change_24h": safe_float(pair.get("priceChange", {}).get("h24")),
                        "name": pair.get("baseToken", {}).get("name", "?"),
                        "symbol": pair.get("baseToken", {}).get("symbol", "?"),
                        "chain": chain,
                        "dex_url": pair.get("url", ""),
                    }

            # Rate limit: DexScreener is generous but be respectful
            time.sleep(0.3)

    log.info(f"Fetched current prices for {len(price_map)} tokens")
    return price_map


# ─── Performance Calculation ──────────────────────────────────────────────

def calculate_performance(alerts: list[dict], price_map: dict) -> dict:
    """
    Calculate backtesting performance metrics.
    For each alert: compare entry_price vs current_price.
    """
    results = []
    total_gain_pct = 0
    winners = 0
    losers = 0
    dead_tokens = 0
    best_trade = None
    worst_trade = None

    for alert in alerts:
        addr = (alert.get("address") or alert.get("token_address") or "").lower()
        name = alert.get("name") or alert.get("token_name") or "Unknown"
        symbol = alert.get("token_symbol") or "?"
        entry_price = safe_float(alert.get("price_usd"))
        entry_score = safe_float(
            alert.get("composite_score") or alert.get("alpha_score", 0)
        )
        alert_time = alert.get("timestamp", "")
        network = alert.get("network", alert.get("chain", "?"))

        if not addr or entry_price <= 0:
            continue

        current_data = price_map.get(addr)

        if not current_data or current_data.get("price_usd", 0) <= 0:
            # Token no longer tradeable or has no liquidity
            dead_tokens += 1
            results.append({
                "name": name,
                "symbol": symbol,
                "address": addr,
                "network": network,
                "entry_price": entry_price,
                "current_price": 0,
                "gain_pct": -100.0,
                "status": "DEAD",
                "entry_score": entry_score,
                "alert_time": alert_time,
                "current_liquidity": 0,
            })
            losers += 1
            total_gain_pct += -100.0
            continue

        current_price = current_data["price_usd"]
        gain_pct = ((current_price - entry_price) / entry_price) * 100

        status = "WIN" if gain_pct > 0 else "LOSS"
        if gain_pct > 0:
            winners += 1
        else:
            losers += 1

        total_gain_pct += gain_pct

        result = {
            "name": current_data.get("name", name),
            "symbol": current_data.get("symbol", symbol),
            "address": addr,
            "network": network,
            "entry_price": entry_price,
            "current_price": current_price,
            "gain_pct": round(gain_pct, 2),
            "status": status,
            "entry_score": entry_score,
            "alert_time": alert_time,
            "current_liquidity": current_data.get("liquidity_usd", 0),
            "current_volume_24h": current_data.get("volume_24h", 0),
            "current_mcap": current_data.get("mcap", 0),
            "dex_url": current_data.get("dex_url", ""),
        }
        results.append(result)

        if best_trade is None or gain_pct > best_trade["gain_pct"]:
            best_trade = result
        if worst_trade is None or gain_pct < worst_trade["gain_pct"]:
            worst_trade = result

    total = winners + losers
    win_rate = (winners / total * 100) if total > 0 else 0
    avg_gain = (total_gain_pct / total) if total > 0 else 0

    # Sort results by gain descending
    results.sort(key=lambda r: r.get("gain_pct", -999), reverse=True)

    metrics = {
        "total_alerts": total,
        "winners": winners,
        "losers": losers,
        "dead_tokens": dead_tokens,
        "win_rate_pct": round(win_rate, 1),
        "avg_gain_pct": round(avg_gain, 1),
        "median_gain_pct": _median([r["gain_pct"] for r in results]) if results else 0,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "results": results,
        "timestamp": now_utc().isoformat(),
    }

    return metrics


def _median(values: list[float]) -> float:
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return round(s[n // 2], 1)
    return round((s[n // 2 - 1] + s[n // 2]) / 2, 1)


# ─── Output Formatting ────────────────────────────────────────────────────

def print_performance_report(metrics: dict):
    """Print a markdown-style performance report."""
    total = metrics["total_alerts"]
    if total == 0:
        print("\nNo alerts with valid entry prices found.")
        return

    print(f"\n{'='*80}")
    print(f"BACKTEST PERFORMANCE REPORT")
    print(f"{'='*80}\n")

    # Summary
    print(f"Total alerts analyzed:  {total}")
    print(f"Winners:               {metrics['winners']} ({metrics['win_rate_pct']:.1f}%)")
    print(f"Losers:                {metrics['losers']}")
    print(f"Dead tokens:           {metrics['dead_tokens']}")
    print(f"Average gain:          {metrics['avg_gain_pct']:+.1f}%")
    print(f"Median gain:           {metrics['median_gain_pct']:+.1f}%")

    # Best/Worst
    best = metrics.get("best_trade")
    worst = metrics.get("worst_trade")
    if best:
        print(f"\nBest trade:  {best['name']} ({best['symbol']}) "
              f"{best['gain_pct']:+.1f}% | Entry: ${best['entry_price']:.8f}")
    if worst:
        print(f"Worst trade: {worst['name']} ({worst['symbol']}) "
              f"{worst['gain_pct']:+.1f}% | Entry: ${worst['entry_price']:.8f}")

    # Detailed table
    results = metrics.get("results", [])
    if results:
        print(f"\n{'─'*100}")
        header = (
            f"{'#':>3}  {'Status':6}  {'Name':20}  {'Entry $':>14}  "
            f"{'Now $':>14}  {'Gain%':>8}  {'Score':>5}  {'Liq':>10}"
        )
        print(header)
        print(f"{'─'*100}")

        for i, r in enumerate(results[:50], 1):
            status = r["status"]
            name = r.get("name", "?")[:18]
            entry = r["entry_price"]
            current = r["current_price"]
            gain = r["gain_pct"]
            score = r.get("entry_score", 0)
            liq = r.get("current_liquidity", 0)

            # Color coding via emoji
            if status == "WIN":
                indicator = "+"
            elif status == "DEAD":
                indicator = "X"
            else:
                indicator = "-"

            print(
                f"{i:>3}  {indicator}{status:5}  {name:20}  "
                f"${entry:>13.8f}  ${current:>13.8f}  {gain:>+7.1f}%  "
                f"{score:>5.1f}  ${liq:>9,.0f}"
            )

        if len(results) > 50:
            print(f"\n... and {len(results) - 50} more results")

    # Score correlation analysis
    _print_score_correlation(results)

    print(f"\n{'='*80}")


def _print_score_correlation(results: list[dict]):
    """Show win rate by score bracket to see if higher scores = better results."""
    if len(results) < 5:
        return

    brackets = {
        "7.0-7.5": {"wins": 0, "total": 0, "gains": []},
        "7.5-8.0": {"wins": 0, "total": 0, "gains": []},
        "8.0-8.5": {"wins": 0, "total": 0, "gains": []},
        "8.5+":    {"wins": 0, "total": 0, "gains": []},
    }

    for r in results:
        score = r.get("entry_score", 0)
        gain = r.get("gain_pct", 0)

        if score < 7.0:
            continue
        elif score < 7.5:
            bracket = "7.0-7.5"
        elif score < 8.0:
            bracket = "7.5-8.0"
        elif score < 8.5:
            bracket = "8.0-8.5"
        else:
            bracket = "8.5+"

        brackets[bracket]["total"] += 1
        brackets[bracket]["gains"].append(gain)
        if gain > 0:
            brackets[bracket]["wins"] += 1

    print(f"\n{'─'*60}")
    print(f"SCORE CORRELATION ANALYSIS")
    print(f"{'─'*60}")
    print(f"{'Score':>10}  {'Alerts':>7}  {'Win Rate':>9}  {'Avg Gain':>9}")
    print(f"{'─'*10}  {'─'*7}  {'─'*9}  {'─'*9}")

    for bracket, data in brackets.items():
        if data["total"] == 0:
            continue
        wr = data["wins"] / data["total"] * 100
        avg = sum(data["gains"]) / len(data["gains"])
        print(f"{bracket:>10}  {data['total']:>7}  {wr:>8.1f}%  {avg:>+8.1f}%")


# ─── Save Report ──────────────────────────────────────────────────────────

def save_backtest_report(metrics: dict, filepath: str = None):
    """Save the full backtest results to JSON."""
    filepath = filepath or BACKTEST_REPORT_FILE
    save_json(filepath, metrics)
    log.info(f"Backtest report saved: {filepath}")


# ─── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtester - Analyze historical alert performance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 backtester.py                      # analyze all alerts
  python3 backtester.py --last-7-days        # recent alerts only
  python3 backtester.py --last-30-days       # last month
  python3 backtester.py --min-score 8.0      # high-conviction alerts only
  python3 backtester.py --export report.json # save detailed JSON
        """,
    )
    parser.add_argument("--last-7-days", action="store_true", help="Only last 7 days")
    parser.add_argument("--last-30-days", action="store_true", help="Only last 30 days")
    parser.add_argument("--days", type=int, help="Custom day range")
    parser.add_argument("--min-score", type=float, default=0, help="Minimum score filter")
    parser.add_argument("--export", type=str, help="Export results to JSON file")

    args = parser.parse_args()
    setup_logging()

    # Determine day filter
    days = None
    if args.last_7_days:
        days = 7
    elif args.last_30_days:
        days = 30
    elif args.days:
        days = args.days

    day_str = f"last {days} days" if days else "all time"
    print(f"\nLoading alerts ({day_str})...")

    # Load alerts
    alerts = load_historical_alerts(days=days, min_score=args.min_score)
    if not alerts:
        print("No alerts found. Run the monitor to generate alerts first.")
        print(f"  Expected files: {ALERTS_FILE} or {ALPHA_ALERTS_FILE}")
        return

    print(f"Found {len(alerts)} alerts to analyze")
    print("Fetching current prices from DexScreener...")

    # Fetch current prices
    price_map = fetch_current_prices(alerts)

    # Calculate performance
    print("Calculating performance...")
    metrics = calculate_performance(alerts, price_map)

    # Print report
    print_performance_report(metrics)

    # Save report
    export_path = args.export or BACKTEST_REPORT_FILE
    save_backtest_report(metrics, export_path)
    print(f"\nDetailed report saved: {export_path}")


if __name__ == "__main__":
    main()
