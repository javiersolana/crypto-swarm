#!/usr/bin/env python3
"""
Crypto Swarm Intelligence System - Main Orchestrator
Pipeline: Scanner -> Auditor -> Sentiment -> Technical -> Portfolio
Usage: python3 swarm.py --capital 300 --mode paper
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import config
from utils import setup_logging, get_logger, save_json, ensure_data_dirs, format_usd, format_pct, format_score, now_utc
from scanner import Scout
from auditor import Forense
from sentiment import Narrator
from technical import Quant
from portfolio import Executor

log = get_logger("swarm")


def generate_report(positions: list[dict], portfolio_summary: dict,
                    pipeline_stats: dict) -> str:
    """Generate a formatted Weekly Alpha Report."""
    now = now_utc()
    report = []
    report.append("=" * 70)
    report.append("       CRYPTO SWARM INTELLIGENCE - WEEKLY ALPHA REPORT")
    report.append(f"       Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    report.append(f"       Mode: {portfolio_summary['mode'].upper()}")
    report.append("=" * 70)
    report.append("")

    # Pipeline Summary
    report.append("--- PIPELINE SUMMARY ---")
    report.append(f"  Tokens scanned:    {pipeline_stats.get('scanned', 0)}")
    report.append(f"  Passed audit:      {pipeline_stats.get('audited', 0)}")
    report.append(f"  Sentiment scored:  {pipeline_stats.get('sentiment', 0)}")
    report.append(f"  Technical scored:  {pipeline_stats.get('technical', 0)}")
    report.append(f"  Final positions:   {pipeline_stats.get('positions', 0)}")
    report.append("")

    # Capital Allocation
    report.append("--- CAPITAL ALLOCATION ---")
    report.append(f"  Total Capital:     EUR {portfolio_summary['total_capital']:.2f}")
    report.append(f"  Reserve:           EUR {portfolio_summary['reserve']:.2f}")
    report.append(f"  Investable:        EUR {portfolio_summary['investable']:.2f}")
    report.append(f"  Allocated:         EUR {portfolio_summary['allocated']:.2f}")
    report.append(f"  Available:         EUR {portfolio_summary['available']:.2f}")
    report.append("")

    # Positions
    if positions:
        report.append("--- RECOMMENDED POSITIONS ---")
        report.append("")
        for i, pos in enumerate(positions, 1):
            pct_of_portfolio = (pos['allocated_eur'] / portfolio_summary['investable'] * 100
                                if portfolio_summary['investable'] > 0 else 0)
            report.append(f"  #{i} {pos['name']}")
            report.append(f"     Network:        {pos['network']}")
            report.append(f"     Address:        {pos['address'][:20]}..." if len(pos.get('address', '')) > 20
                          else f"     Address:        {pos.get('address', 'N/A')}")
            report.append(f"     Entry Price:    ${pos['entry_price']:.8f}")
            report.append(f"     Current Price:  ${pos['current_price']:.8f}")
            report.append(f"     Allocation:     EUR {pos['allocated_eur']:.2f} ({pct_of_portfolio:.1f}%)")
            report.append(f"     Stop-Loss:      ${pos['stop_loss']:.8f} ({config.STOP_LOSS_PCT}%)")
            report.append(f"     Take-Profit:    ${pos['take_profit']:.8f} (+{config.TAKE_PROFIT_PCT}%)")
            report.append(f"     Composite:      {format_score(pos['composite_score'])}")
            report.append(f"       Scout:        {format_score(pos['scout_score'])}")
            report.append(f"       Forense:      {format_score(pos['forense_score'])}")
            report.append(f"       Narrator:     {format_score(pos['narrator_score'])}")
            report.append(f"       Quant:        {format_score(pos['quant_score'])}")
            report.append(f"       Executor:     {format_score(pos['executor_score'])}")

            # Key signals
            signals = pos.get("signals", {})
            all_signals = (
                signals.get("early_entry_signals", []) +
                signals.get("forense_flags", []) +
                signals.get("narrator_signals", []) +
                signals.get("quant_signals", [])
            )
            if all_signals:
                report.append(f"     Key Signals:    {', '.join(all_signals[:5])}")
            report.append("")
    else:
        report.append("--- NO POSITIONS RECOMMENDED ---")
        report.append("  The swarm could not find tokens meeting all safety criteria.")
        report.append("  This is normal - safety first. Will scan again next week.")
        report.append("")

    # Risk Warnings
    report.append("--- RISK WARNINGS ---")
    report.append("  - This is for EDUCATIONAL/RESEARCH purposes only")
    report.append("  - Paper trading mode: NO real money at risk")
    report.append("  - New DEX tokens are EXTREMELY risky (>90% lose value)")
    report.append("  - Never invest more than you can afford to lose")
    report.append("  - Past scans do not predict future performance")
    report.append("")
    report.append("=" * 70)
    report.append("  Crypto Swarm Intelligence v1.0 - Research Tool")
    report.append("=" * 70)

    return "\n".join(report)


def run_pipeline(capital: float, mode: str):
    """Execute the full swarm pipeline."""
    setup_logging()
    ensure_data_dirs()

    log.info("=" * 60)
    log.info("CRYPTO SWARM INTELLIGENCE SYSTEM - Starting Pipeline")
    log.info(f"Capital: EUR {capital:.2f} | Mode: {mode}")
    log.info("=" * 60)

    pipeline_stats = {}

    # ─── Stage 1: THE SCOUT ──────────────────────────────────────────────
    log.info("\n[1/5] THE SCOUT - Scanning for new tokens...")
    scout = Scout()
    candidates = scout.scan()
    pipeline_stats["scanned"] = len(candidates)
    log.info(f"Scout found {len(candidates)} candidates")

    if not candidates:
        log.warning("No candidates found. Market may be quiet or APIs unavailable.")
        _save_empty_report(capital, mode, pipeline_stats)
        return

    # ─── Stage 2: THE FORENSE ────────────────────────────────────────────
    log.info("\n[2/5] THE FORENSE - Auditing for scams...")
    forense = Forense()
    audited = forense.audit(candidates)
    pipeline_stats["audited"] = len(audited)
    log.info(f"Forense passed {len(audited)}/{len(candidates)} candidates")

    if not audited:
        log.warning("All candidates rejected by Forense. Safety first!")
        _save_empty_report(capital, mode, pipeline_stats)
        return

    # ─── Stage 3: THE NARRATOR ───────────────────────────────────────────
    log.info("\n[3/5] THE NARRATOR - Analyzing sentiment...")
    narrator = Narrator()
    with_sentiment = narrator.analyze(audited)
    pipeline_stats["sentiment"] = len(with_sentiment)

    # ─── Stage 4: THE QUANT ──────────────────────────────────────────────
    log.info("\n[4/5] THE QUANT - Technical analysis...")
    quant = Quant()
    with_technicals = quant.analyze(with_sentiment)
    pipeline_stats["technical"] = len(with_technicals)

    # ─── Stage 5: THE EXECUTOR ───────────────────────────────────────────
    log.info("\n[5/5] THE EXECUTOR - Allocating capital...")
    executor = Executor(capital=capital, mode=mode)
    positions = executor.select_and_allocate(with_technicals)
    pipeline_stats["positions"] = len(positions)

    portfolio_summary = executor.get_portfolio_summary()

    # ─── Generate Report ─────────────────────────────────────────────────
    report = generate_report(positions, portfolio_summary, pipeline_stats)

    # Save report
    timestamp = now_utc().strftime("%Y-%m-%d_%H%M")
    report_path = os.path.join(config.WEEKLY_REPORTS_DIR, f"report_{timestamp}.txt")
    with open(report_path, "w") as f:
        f.write(report)

    # Save scan history
    _save_scan_history(candidates, pipeline_stats)

    # Print report
    print("\n")
    print(report)
    print(f"\nReport saved to: {report_path}")
    print(f"Portfolio saved to: {config.PORTFOLIO_FILE}")

    return positions


def _save_empty_report(capital: float, mode: str, pipeline_stats: dict):
    """Save an empty report when no positions are found."""
    portfolio_summary = {
        "mode": mode,
        "total_capital": capital,
        "reserve": config.RESERVE_AMOUNT,
        "investable": capital - config.RESERVE_AMOUNT,
        "allocated": 0,
        "available": capital - config.RESERVE_AMOUNT,
        "num_positions": 0,
    }
    report = generate_report([], portfolio_summary, pipeline_stats)
    timestamp = now_utc().strftime("%Y-%m-%d_%H%M")
    report_path = os.path.join(config.WEEKLY_REPORTS_DIR, f"report_{timestamp}.txt")
    with open(report_path, "w") as f:
        f.write(report)
    print("\n")
    print(report)
    print(f"\nReport saved to: {report_path}")


def _save_scan_history(candidates: list[dict], stats: dict):
    """Append scan results to history."""
    history = load_json_safe(config.SCAN_HISTORY_FILE)
    if not isinstance(history, list):
        history = []

    entry = {
        "timestamp": now_utc().isoformat(),
        "stats": stats,
        "top_candidates": [
            {
                "name": c.get("name"),
                "address": c.get("address"),
                "network": c.get("network"),
                "scout_score": c.get("scout_score"),
                "composite_score": c.get("composite_score"),
            }
            for c in candidates[:10]
        ],
    }
    history.append(entry)
    save_json(config.SCAN_HISTORY_FILE, history)


def load_json_safe(filepath):
    """Load JSON, return empty list if file missing or invalid."""
    from utils import load_json
    data = load_json(filepath)
    return data if data else []


def main():
    parser = argparse.ArgumentParser(
        description="Crypto Swarm Intelligence System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 swarm.py --capital 300 --mode paper
  python3 swarm.py --capital 500 --mode paper
  python3 swarm.py --update   (update existing positions)
        """,
    )
    parser.add_argument(
        "--capital", type=float, default=config.DEFAULT_CAPITAL,
        help=f"Total monthly capital in EUR (default: {config.DEFAULT_CAPITAL})",
    )
    parser.add_argument(
        "--mode", choices=["paper", "analysis"],
        default="paper",
        help="Execution mode: paper (track positions) or analysis (report only)",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Update existing positions with current prices",
    )

    args = parser.parse_args()

    if args.update:
        setup_logging()
        ensure_data_dirs()
        log.info("Updating existing positions...")
        executor = Executor(capital=args.capital, mode=args.mode)
        positions = executor.update_positions()
        summary = executor.get_portfolio_summary()
        print(f"\nPortfolio: {summary['num_positions']} positions")
        print(f"Total PnL: EUR {summary['total_pnl_eur']:.2f}")
        for p in positions:
            if p["status"] == "open":
                print(f"  {p['name']}: {format_pct(p.get('pnl_pct', 0))} (EUR {p.get('pnl_eur', 0):.2f})")
    else:
        run_pipeline(args.capital, args.mode)


if __name__ == "__main__":
    main()
