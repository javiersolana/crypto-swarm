#!/usr/bin/env python3
"""
Crypto Swarm Intelligence System v2.0 - Enhanced with Alpha Hunter

Extended pipeline:
  Scanner -> Auditor -> Sentiment -> [Alpha: Social Intel + Wallet Tracking] ->
  Technical -> Triple Confirmation -> Portfolio

New stages (integrated between existing ones):
  - Smart Wallet Tracking: Checks if tracked wallets are buying any candidates
  - Social Intelligence: News sentiment + GitHub activity scoring
  - Triple Confirmation: Multi-signal convergence scoring

Usage:
  python3 swarm_v2.py --capital 300 --mode paper           # full pipeline
  python3 swarm_v2.py --capital 300 --alpha-only            # only show alpha signals
  python3 swarm_v2.py --wallet-scan                         # just scan wallets
  python3 swarm_v2.py --update                              # update positions
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import config
import config_alpha
from utils import (setup_logging, get_logger, save_json, load_json,
                   ensure_data_dirs, format_usd, format_pct, format_score, now_utc)
from scanner import Scout
from auditor import Forense
from sentiment import Narrator
from technical import Quant
from portfolio import Executor
from alpha.smart_wallet_tracker import SmartWalletTracker
from alpha.social_intel import SocialIntel
from alpha.triple_confirm import TripleConfirmation

log = get_logger("swarm_v2")


def run_alpha_pipeline(capital: float, mode: str, alpha_only: bool = False):
    """Execute the enhanced swarm pipeline with alpha signals."""
    setup_logging()
    ensure_data_dirs()

    log.info("=" * 70)
    log.info("  CRYPTO SWARM INTELLIGENCE v2.0 - ALPHA HUNTER PIPELINE")
    log.info(f"  Capital: EUR {capital:.2f} | Mode: {mode}")
    log.info("=" * 70)

    stats = {}

    # ─── Stage 1: THE SCOUT ──────────────────────────────────────────
    log.info("\n[1/7] THE SCOUT - Scanning for new tokens...")
    scout = Scout()
    candidates = scout.scan()
    stats["scanned"] = len(candidates)
    log.info(f"  Scout found {len(candidates)} candidates")

    if not candidates:
        log.warning("No candidates found. Market may be quiet.")
        _save_report([], {}, stats, capital, mode)
        return []

    # ─── Stage 2: THE FORENSE ────────────────────────────────────────
    log.info("\n[2/7] THE FORENSE - Auditing for scams...")
    forense = Forense()
    audited = forense.audit(candidates)
    stats["audited"] = len(audited)
    log.info(f"  Forense passed {len(audited)}/{len(candidates)} candidates")

    if not audited:
        log.warning("All candidates rejected by Forense. Safety first!")
        _save_report([], {}, stats, capital, mode)
        return []

    # ─── Stage 3: THE NARRATOR ───────────────────────────────────────
    log.info("\n[3/7] THE NARRATOR - Analyzing sentiment...")
    narrator = Narrator()
    with_sentiment = narrator.analyze(audited)
    stats["sentiment"] = len(with_sentiment)

    # ─── Stage 4: ALPHA - Smart Wallet Check ─────────────────────────
    log.info("\n[4/7] ALPHA - Smart Wallet Tracking...")
    wallet_signals = []
    try:
        tracker = SmartWalletTracker()
        wallets = tracker.db.load_wallets()
        if wallets:
            wallet_signals = tracker.scan_all_wallets()
            wallet_signals = tracker.enrich_signals(wallet_signals)
            log.info(f"  Wallet tracker: {len(wallet_signals)} buy signals from {len(wallets)} wallets")
        else:
            log.info("  No wallets tracked. Add wallets with: "
                     "python3 alpha/smart_wallet_tracker.py --add-wallet <addr>")
    except Exception as e:
        log.warning(f"  Wallet tracking error: {e}")
    stats["wallet_signals"] = len(wallet_signals)

    # ─── Stage 5: ALPHA - Social Intelligence ────────────────────────
    log.info("\n[5/7] ALPHA - Social Intelligence...")
    try:
        social = SocialIntel()
        with_social = social.analyze_batch(with_sentiment)
        social_signals = sum(1 for t in with_social if t.get("social_intel_signals"))
        log.info(f"  Social intel: {social_signals}/{len(with_social)} tokens have social signals")
    except Exception as e:
        log.warning(f"  Social intel error: {e}")
        with_social = with_sentiment
    stats["social_signals"] = social_signals if 'social_signals' in dir() else 0

    # ─── Stage 6: THE QUANT ──────────────────────────────────────────
    log.info("\n[6/7] THE QUANT - Technical analysis...")
    quant = Quant()
    with_technicals = quant.analyze(with_social)
    stats["technical"] = len(with_technicals)

    # ─── Stage 7: ALPHA - Triple Confirmation ────────────────────────
    log.info("\n[7/7] ALPHA - Triple Confirmation...")
    tc = TripleConfirmation()
    confirmed = tc.evaluate(with_technicals, wallet_signals)

    # Compute enhanced composite scores
    for token in confirmed:
        enhanced = tc.compute_enhanced_composite(token)
        token["enhanced_composite"] = enhanced

    # Get high-priority alpha signals
    alpha_alerts = tc.get_high_priority_alerts(confirmed)
    stats["alpha_alerts"] = len(alpha_alerts)
    stats["triple_confirmed"] = sum(1 for t in confirmed
                                     if t.get("alpha_signal_count", 0) >= 3)

    if alpha_alerts:
        log.info(f"\n  HIGH PRIORITY ALPHA ALERTS: {len(alpha_alerts)}")
        for a in alpha_alerts:
            log.info(f"    {a.get('name')}: alpha={a['alpha_score']:.1f}, "
                     f"signals={a.get('alpha_signal_count', 0)}")

    # ─── Portfolio Selection ─────────────────────────────────────────
    if not alpha_only:
        log.info("\n--- THE EXECUTOR - Allocating capital...")
        executor = Executor(capital=capital, mode=mode)

        # Use enhanced composite for selection
        for token in confirmed:
            # Override composite_score with enhanced version for portfolio selection
            if token.get("enhanced_composite"):
                token["original_composite"] = token.get("composite_score", 0)
                token["composite_score"] = token["enhanced_composite"]

        positions = executor.select_and_allocate(confirmed)
        stats["positions"] = len(positions)
        portfolio_summary = executor.get_portfolio_summary()
    else:
        positions = []
        portfolio_summary = {
            "mode": mode, "total_capital": capital,
            "reserve": config.RESERVE_AMOUNT,
            "investable": capital - config.RESERVE_AMOUNT,
            "allocated": 0, "available": capital - config.RESERVE_AMOUNT,
        }
        stats["positions"] = 0

    # ─── Generate Report ─────────────────────────────────────────────
    report = generate_alpha_report(
        positions, confirmed, alpha_alerts, wallet_signals,
        portfolio_summary, stats
    )

    _save_report(positions, portfolio_summary, stats, capital, mode, report)

    # Print report
    print("\n")
    print(report)

    # Save alpha alerts separately
    if alpha_alerts:
        save_json(config_alpha.ALPHA_ALERTS_FILE, [
            {
                "timestamp": now_utc().isoformat(),
                "name": a.get("name"),
                "address": a.get("address"),
                "network": a.get("network"),
                "alpha_score": a.get("alpha_score"),
                "alpha_signals": a.get("alpha_signals"),
                "alpha_signal_count": a.get("alpha_signal_count"),
                "composite_score": a.get("composite_score"),
                "enhanced_composite": a.get("enhanced_composite"),
                "price_usd": a.get("price_usd"),
                "liquidity_usd": a.get("liquidity_usd"),
            }
            for a in alpha_alerts
        ])

    return positions


def generate_alpha_report(positions: list, candidates: list,
                          alpha_alerts: list, wallet_signals: list,
                          portfolio_summary: dict, stats: dict) -> str:
    """Generate the enhanced Alpha Hunter report."""
    now = now_utc()
    r = []
    r.append("=" * 70)
    r.append("    CRYPTO SWARM v2.0 - ALPHA HUNTER INTELLIGENCE REPORT")
    r.append(f"    Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    r.append(f"    Mode: {portfolio_summary.get('mode', 'paper').upper()}")
    r.append("=" * 70)
    r.append("")

    # Pipeline Summary
    r.append("--- PIPELINE SUMMARY ---")
    r.append(f"  Tokens scanned:      {stats.get('scanned', 0)}")
    r.append(f"  Passed audit:        {stats.get('audited', 0)}")
    r.append(f"  Sentiment scored:    {stats.get('sentiment', 0)}")
    r.append(f"  Wallet signals:      {stats.get('wallet_signals', 0)}")
    r.append(f"  Social signals:      {stats.get('social_signals', 0)}")
    r.append(f"  Technical scored:    {stats.get('technical', 0)}")
    r.append(f"  Triple confirmed:    {stats.get('triple_confirmed', 0)}")
    r.append(f"  Alpha alerts:        {stats.get('alpha_alerts', 0)}")
    r.append(f"  Final positions:     {stats.get('positions', 0)}")
    r.append("")

    # Smart Wallet Activity
    if wallet_signals:
        r.append("--- SMART WALLET ACTIVITY ---")
        for sig in wallet_signals[:5]:
            r.append(f"  {sig.get('wallet_label', '?')} bought "
                     f"{sig.get('token_name', '?')} ({sig.get('chain', '?')})")
            if sig.get("liquidity_usd"):
                r.append(f"    Liq: ${sig['liquidity_usd']:,.0f} | "
                         f"Age: {sig.get('pool_age_hours', 0):.1f}h")
        r.append("")

    # Alpha Alerts (Triple Confirmed)
    if alpha_alerts:
        r.append("--- HIGH PRIORITY ALPHA ALERTS ---")
        r.append("")
        for i, alert in enumerate(alpha_alerts[:5], 1):
            r.append(f"  #{i} {alert.get('name', '?')}")
            r.append(f"     Network:         {alert.get('network', '?')}")
            r.append(f"     Alpha Score:     {alert.get('alpha_score', 0):.1f}/10 "
                     f"({alert.get('alpha_signal_count', 0)} signals)")
            r.append(f"     Enhanced Score:  {alert.get('enhanced_composite', 0):.1f}/10")
            r.append(f"     Price:           ${alert.get('price_usd', 0):.8f}")
            r.append(f"     Liquidity:       ${alert.get('liquidity_usd', 0):,.0f}")
            r.append(f"     24h Change:      {alert.get('price_change_24h', 0):+.1f}%")
            sigs = alert.get("alpha_signals", [])
            if sigs:
                r.append(f"     Signals:         {', '.join(sigs[:5])}")
            r.append("")
    else:
        r.append("--- NO TRIPLE-CONFIRMED SIGNALS ---")
        r.append("  No tokens passed triple confirmation this cycle.")
        r.append("  The swarm found tokens but none had 3+ converging signals.")
        r.append("")

    # Top Candidates by Alpha Score
    top_alpha = sorted(candidates, key=lambda x: x.get("alpha_score", 0), reverse=True)[:10]
    r.append("--- TOP 10 BY ALPHA SCORE ---")
    r.append(f"  {'Name':<30} {'Alpha':>6} {'Comp':>6} {'Enh':>6} {'Signals':>8}")
    r.append("  " + "-" * 62)
    for t in top_alpha:
        name = t.get("name", "?")[:28]
        alpha = t.get("alpha_score", 0)
        comp = t.get("composite_score", 0)
        enh = t.get("enhanced_composite", comp)
        sigs = t.get("alpha_signal_count", 0)
        r.append(f"  {name:<30} {alpha:>5.1f} {comp:>5.1f} {enh:>5.1f} {sigs:>8d}")
    r.append("")

    # Capital Allocation
    r.append("--- CAPITAL ALLOCATION ---")
    r.append(f"  Total Capital:     EUR {portfolio_summary.get('total_capital', 0):.2f}")
    r.append(f"  Reserve:           EUR {portfolio_summary.get('reserve', 0):.2f}")
    r.append(f"  Investable:        EUR {portfolio_summary.get('investable', 0):.2f}")
    r.append(f"  Allocated:         EUR {portfolio_summary.get('allocated', 0):.2f}")
    r.append(f"  Available:         EUR {portfolio_summary.get('available', 0):.2f}")
    r.append("")

    # Positions
    if positions:
        r.append("--- POSITIONS ---")
        r.append("")
        for i, pos in enumerate(positions, 1):
            pct = (pos['allocated_eur'] / portfolio_summary.get('investable', 1) * 100
                   if portfolio_summary.get('investable') else 0)
            r.append(f"  #{i} {pos['name']}")
            r.append(f"     Network:        {pos.get('network', '?')}")
            addr = pos.get('address', 'N/A')
            r.append(f"     Address:        {addr[:20]}..." if len(addr) > 20 else f"     Address:        {addr}")
            r.append(f"     Entry Price:    ${pos['entry_price']:.8f}")
            r.append(f"     Allocation:     EUR {pos['allocated_eur']:.2f} ({pct:.1f}%)")
            r.append(f"     Stop-Loss:      ${pos['stop_loss']:.8f} ({config.STOP_LOSS_PCT}%)")
            r.append(f"     Take-Profit:    ${pos['take_profit']:.8f} (+{config.TAKE_PROFIT_PCT}%)")
            r.append(f"     Composite:      {format_score(pos.get('composite_score', 0))}")
            alpha_sc = pos.get("alpha_score", 0)
            if alpha_sc > 0:
                r.append(f"     Alpha Score:    {format_score(alpha_sc)}")
            sigs = pos.get("alpha_signals", [])
            if sigs:
                r.append(f"     Alpha Signals:  {', '.join(sigs[:4])}")
            r.append("")

    # Risk Warnings
    r.append("--- RISK WARNINGS ---")
    r.append("  - This is for EDUCATIONAL/RESEARCH purposes only")
    r.append("  - Paper trading mode: NO real money at risk")
    r.append("  - Smart wallet signals do NOT guarantee profits")
    r.append("  - Triple confirmation reduces risk but doesn't eliminate it")
    r.append("  - Never invest more than you can afford to lose")
    r.append("")
    r.append("=" * 70)
    r.append("  Crypto Swarm Intelligence v2.0 - Alpha Hunter")
    r.append("=" * 70)

    return "\n".join(r)


def _save_report(positions, portfolio_summary, stats, capital, mode, report=None):
    """Save report and scan history."""
    timestamp = now_utc().strftime("%Y-%m-%d_%H%M")
    report_path = os.path.join(config.WEEKLY_REPORTS_DIR, f"alpha_report_{timestamp}.txt")

    if report:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            f.write(report)
        print(f"\nReport saved to: {report_path}")

    # Save scan history
    history_file = config.SCAN_HISTORY_FILE
    history = load_json(history_file)
    if not isinstance(history, list):
        history = []
    history.append({
        "timestamp": now_utc().isoformat(),
        "version": "v2.0-alpha",
        "stats": stats,
    })
    save_json(history_file, history)


def run_wallet_scan_only():
    """Quick wallet scan without full pipeline."""
    setup_logging()
    ensure_data_dirs()

    log.info("Running wallet-only scan...")
    tracker = SmartWalletTracker()

    wallets = tracker.db.load_wallets()
    if not wallets:
        print("No wallets tracked. Add wallets first:")
        print("  python3 alpha/smart_wallet_tracker.py --add-wallet <address> --chain solana")
        return

    signals = tracker.scan_all_wallets()
    signals = tracker.enrich_signals(signals)

    if signals:
        print(f"\nDetected {len(signals)} buy signal(s):")
        for sig in signals:
            print(f"\n  Wallet: {sig.get('wallet_label', '?')}")
            print(f"  Bought: {sig.get('token_name', '?')} ({sig.get('token_symbol', '?')})")
            print(f"  Chain:  {sig.get('chain', '?')}")
            if sig.get("price_usd"):
                print(f"  Price:  ${sig['price_usd']:.8f}")
            if sig.get("liquidity_usd"):
                print(f"  Liq:    ${sig['liquidity_usd']:,.0f}")
            if sig.get("pool_age_hours"):
                print(f"  Age:    {sig['pool_age_hours']:.1f}h")
    else:
        print("No new buy signals detected from tracked wallets.")


def main():
    parser = argparse.ArgumentParser(
        description="Crypto Swarm Intelligence v2.0 - Alpha Hunter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 swarm_v2.py --capital 300 --mode paper    # full alpha pipeline
  python3 swarm_v2.py --alpha-only                  # show only alpha signals
  python3 swarm_v2.py --wallet-scan                 # quick wallet check
  python3 swarm_v2.py --update                      # update positions

Setup (optional - enhances signals):
  export HELIUS_API_KEY="your_key"        # Smart wallet tracking (Solana)
  export CRYPTOPANIC_API_KEY="your_key"   # News sentiment
  export GITHUB_TOKEN="your_token"        # GitHub activity monitoring
  export BIRDEYE_API_KEY="your_key"       # Extended Solana data
  export BASESCAN_API_KEY="your_key"      # Base wallet tracking
        """,
    )
    parser.add_argument("--capital", type=float, default=config.DEFAULT_CAPITAL,
                        help=f"Total capital in EUR (default: {config.DEFAULT_CAPITAL})")
    parser.add_argument("--mode", choices=["paper", "analysis"], default="paper")
    parser.add_argument("--alpha-only", action="store_true",
                        help="Only show alpha signals, skip portfolio allocation")
    parser.add_argument("--wallet-scan", action="store_true",
                        help="Quick scan of tracked wallets only")
    parser.add_argument("--update", action="store_true",
                        help="Update existing positions")

    args = parser.parse_args()

    if args.wallet_scan:
        run_wallet_scan_only()
        return

    if args.update:
        setup_logging()
        ensure_data_dirs()
        executor = Executor(capital=args.capital, mode=args.mode)
        positions = executor.update_positions()
        summary = executor.get_portfolio_summary()
        print(f"\nPortfolio: {summary['num_positions']} positions")
        for p in positions:
            if p["status"] == "open":
                print(f"  {p['name']}: {format_pct(p.get('pnl_pct', 0))}")
        return

    run_alpha_pipeline(args.capital, args.mode, args.alpha_only)


if __name__ == "__main__":
    main()
