#!/usr/bin/env python3
"""
Alpha Monitor - Enhanced Telegram alerts with triple confirmation.

Combines the original alert_monitor with alpha signals:
  - Runs the full v2 pipeline (including wallet tracking + social intel)
  - Sends PRIORITY alerts for triple-confirmed tokens
  - Sends STANDARD alerts for high-score tokens (same as v1)
  - Monitors smart wallets continuously in background

Usage:
  python3 alpha_monitor.py                    # daemon mode (every 15 min)
  python3 alpha_monitor.py --once             # single scan
  python3 alpha_monitor.py --interval 10      # custom interval
  python3 alpha_monitor.py --wallets-only     # only monitor wallets
  python3 alpha_monitor.py --test             # test Telegram
"""
import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import config_alpha
from utils import setup_logging, get_logger, load_json, save_json, ensure_data_dirs, now_utc
from alert_monitor import (
    send_telegram, load_seen_tokens, save_seen_tokens, is_token_new,
    mark_token_seen, format_alert, save_alert, ALERT_MIN_SCORE, ALERT_MAX_PUMP_PCT
)

log = get_logger("alpha_monitor")

# Alpha-specific settings
ALPHA_SCAN_INTERVAL = 15  # minutes (more frequent than v1's 30min)
WALLET_CHECK_INTERVAL = 120  # seconds for wallet-only checks


def run_alpha_scan_cycle() -> int:
    """Run one alpha-enhanced scan cycle. Returns number of alerts sent."""
    from scanner import Scout
    from auditor import Forense
    from sentiment import Narrator
    from technical import Quant
    from portfolio import Executor
    from alpha.smart_wallet_tracker import SmartWalletTracker
    from alpha.social_intel import SocialIntel
    from alpha.triple_confirm import TripleConfirmation

    log.info("=" * 50)
    log.info("ALPHA MONITOR: Starting scan cycle")
    log.info("=" * 50)

    seen = load_seen_tokens()
    alerts_sent = 0

    try:
        # Standard pipeline
        scout = Scout()
        candidates = scout.scan()
        log.info(f"Scout: {len(candidates)} candidates")
        if not candidates:
            return 0

        forense = Forense()
        audited = forense.audit(candidates)
        log.info(f"Forense: {len(audited)} passed")
        if not audited:
            return 0

        narrator = Narrator()
        with_sentiment = narrator.analyze(audited)

        # Alpha: Social Intelligence
        try:
            social = SocialIntel()
            with_social = social.analyze_batch(with_sentiment)
        except Exception as e:
            log.warning(f"Social intel skipped: {e}")
            with_social = with_sentiment

        # Alpha: Smart Wallet Check
        wallet_signals = []
        try:
            tracker = SmartWalletTracker()
            if tracker.db.load_wallets():
                wallet_signals = tracker.scan_all_wallets()
                wallet_signals = tracker.enrich_signals(wallet_signals)
        except Exception as e:
            log.warning(f"Wallet tracking skipped: {e}")

        # Technical analysis
        quant = Quant()
        with_technicals = quant.analyze(with_social)

        # Triple Confirmation
        tc = TripleConfirmation()
        confirmed = tc.evaluate(with_technicals, wallet_signals)

        # Compute enhanced scores
        executor = Executor(capital=config.DEFAULT_CAPITAL, mode="paper")
        scored = executor._compute_composite_scores(confirmed)

        for token in scored:
            enhanced = tc.compute_enhanced_composite(token)
            token["enhanced_composite"] = enhanced

        # Priority 1: Triple-confirmed alpha alerts
        alpha_alerts = tc.get_high_priority_alerts(scored)
        for token in alpha_alerts:
            address = token.get("address", "")
            if not is_token_new(address, seen):
                continue

            alert_msg = tc.format_alpha_alert(token)
            send_telegram(alert_msg)
            mark_token_seen(address, seen)
            save_alert(token, alert_msg)
            alerts_sent += 1
            log.info(f"  ALPHA ALERT: {token.get('name')} "
                     f"(alpha={token.get('alpha_score', 0):.1f})")

        # Priority 2: Standard high-score alerts (not already sent as alpha)
        alpha_addresses = set(t.get("address", "").lower() for t in alpha_alerts)
        for token in scored:
            address = token.get("address", "")
            if address.lower() in alpha_addresses:
                continue

            composite = token.get("enhanced_composite", token.get("composite_score", 0))
            change = token.get("price_change_24h", 0)

            if composite < ALERT_MIN_SCORE:
                continue
            if change > ALERT_MAX_PUMP_PCT:
                continue
            if not is_token_new(address, seen):
                continue

            alert_msg = format_alert(token)
            send_telegram(alert_msg)
            mark_token_seen(address, seen)
            save_alert(token, alert_msg)
            alerts_sent += 1
            log.info(f"  ALERT: {token.get('name')} (score={composite:.2f})")

        save_seen_tokens(seen)

        # Also send wallet buy alerts
        for sig in wallet_signals:
            addr = sig.get("token_address", "")
            if addr and is_token_new(addr, seen) and sig.get("liquidity_usd", 0) >= 30000:
                from alpha.smart_wallet_tracker import format_wallet_alert
                alert_msg = format_wallet_alert(sig)
                send_telegram(alert_msg)
                mark_token_seen(addr, seen)
                alerts_sent += 1

        log.info(f"Scan cycle complete. {alerts_sent} alerts sent.")
        return alerts_sent

    except Exception as e:
        log.error(f"Alpha scan error: {e}", exc_info=True)
        try:
            send_telegram(f"<b>Alpha Monitor Error</b>\n\n{str(e)[:200]}")
        except Exception:
            pass
        return 0


def run_wallet_monitor():
    """Lightweight wallet-only monitoring loop."""
    from alpha.smart_wallet_tracker import SmartWalletTracker, format_wallet_alert

    tracker = SmartWalletTracker()
    wallets = tracker.db.load_wallets()
    if not wallets:
        print("No wallets tracked. Add some first.")
        return

    log.info(f"Wallet monitor: tracking {len(wallets)} wallets "
             f"(check every {WALLET_CHECK_INTERVAL}s)")
    send_telegram(
        f"<b>Wallet Monitor Started</b>\n"
        f"Tracking {len(wallets)} wallets\n"
        f"Interval: {WALLET_CHECK_INTERVAL}s"
    )

    seen = load_seen_tokens()
    while True:
        try:
            signals = tracker.scan_all_wallets()
            signals = tracker.enrich_signals(signals)

            for sig in signals:
                addr = sig.get("token_address", "")
                if addr and is_token_new(addr, seen):
                    if sig.get("liquidity_usd", 0) >= config.SCAN_MIN_LIQUIDITY:
                        alert = format_wallet_alert(sig)
                        send_telegram(alert)
                        mark_token_seen(addr, seen)
                        tracker.db.save_trade(sig)
                        save_seen_tokens(seen)
                        log.info(f"Wallet alert: {sig.get('wallet_label')} -> "
                                 f"{sig.get('token_name')}")
        except Exception as e:
            log.error(f"Wallet monitor error: {e}")

        time.sleep(WALLET_CHECK_INTERVAL)


def daemon_loop(interval_min: int = 15, wallets_only: bool = False):
    """Main daemon loop."""
    setup_logging()
    ensure_data_dirs()

    if wallets_only:
        run_wallet_monitor()
        return

    log.info(f"Alpha Monitor daemon starting (interval: {interval_min} min)")

    # Check what APIs are configured
    apis = []
    if config_alpha.HELIUS_API_KEY:
        apis.append("Helius (wallet tracking)")
    if config_alpha.CRYPTOPANIC_API_KEY:
        apis.append("CryptoPanic (news)")
    if config_alpha.GITHUB_TOKEN:
        apis.append("GitHub (dev activity)")
    if config_alpha.BIRDEYE_API_KEY:
        apis.append("Birdeye (Solana data)")

    api_str = ", ".join(apis) if apis else "None (basic mode)"
    log.info(f"APIs configured: {api_str}")

    send_telegram(
        "<b>Alpha Monitor v2.0 Started</b>\n\n"
        f"Scan interval: {interval_min} min\n"
        f"APIs: {api_str}\n"
        f"Min score: {ALERT_MIN_SCORE}/10"
    )

    running = True
    def _shutdown(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    cycle = 0
    while running:
        cycle += 1
        log.info(f"\n--- Alpha Cycle #{cycle} at {now_utc().strftime('%H:%M UTC')} ---")

        try:
            alerts = run_alpha_scan_cycle()
            log.info(f"Cycle #{cycle}: {alerts} alerts")
        except Exception as e:
            log.error(f"Cycle #{cycle} failed: {e}", exc_info=True)

        if not running:
            break

        for _ in range(interval_min * 60):
            if not running:
                break
            time.sleep(1)

    send_telegram("<b>Alpha Monitor Stopped</b>")


def main():
    parser = argparse.ArgumentParser(description="Alpha Monitor - Enhanced Alerts")
    parser.add_argument("--once", action="store_true", help="Single scan")
    parser.add_argument("--interval", type=int, default=ALPHA_SCAN_INTERVAL,
                        help=f"Minutes between scans (default: {ALPHA_SCAN_INTERVAL})")
    parser.add_argument("--wallets-only", action="store_true",
                        help="Only monitor wallet activity")
    parser.add_argument("--test", action="store_true", help="Test Telegram")

    args = parser.parse_args()

    if args.test:
        setup_logging()
        send_telegram(
            "<b>Alpha Monitor v2.0 Test</b>\n\n"
            f"Time: {now_utc().strftime('%Y-%m-%d %H:%M UTC')}\n"
            "Alpha Hunter system ready."
        )
        return

    if args.once:
        setup_logging()
        ensure_data_dirs()
        alerts = run_alpha_scan_cycle()
        print(f"\nScan complete: {alerts} alerts sent")
        return

    daemon_loop(args.interval, args.wallets_only)


if __name__ == "__main__":
    main()
