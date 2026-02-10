#!/usr/bin/env python3
"""
Alpha Monitor - Enhanced Telegram alerts with triple confirmation.

Combines the original alert_monitor with alpha signals:
  - Runs the full v2 pipeline (including wallet tracking + social intel)
  - Sends PRIORITY alerts for triple-confirmed tokens
  - Sends STANDARD alerts for high-score tokens (same as v1)
  - Monitors smart wallets continuously in background

v3.0: Early alerting, parallel wallet tracking, ~2min cycle target.

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
import threading
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
EARLY_ALERT_MIN_SCORE = 8.0  # score threshold for immediate early alerts during audit


def run_alpha_scan_cycle(wallet_signals_holder: dict = None) -> int:
    """Run one alpha-enhanced scan cycle. Returns number of alerts sent.

    v3.0: Early alerting during audit phase. Wallet signals received from
    parallel background thread via wallet_signals_holder dict.
    """
    from scanner import Scout
    from auditor import Forense
    from sentiment import Narrator
    from technical import Quant
    from portfolio import Executor
    from alpha.social_intel import SocialIntel
    from alpha.triple_confirm import TripleConfirmation

    log.info("=" * 50)
    log.info("ALPHA MONITOR v3.0: Starting scan cycle")
    log.info("=" * 50)
    cycle_start = time.monotonic()

    seen = load_seen_tokens()
    seen_lock = threading.Lock()
    alerts_sent = 0
    alerts_lock = threading.Lock()
    early_alerted = set()  # addresses already sent as early alerts

    # ── Early alert callback: fires during audit for high-scoring tokens ──
    def _on_audit_pass(token):
        """Called by Forense when a token passes audit. Sends immediate alert
        if forense_score is high enough, without waiting for full pipeline."""
        nonlocal alerts_sent
        address = token.get("address", "")
        score = token.get("forense_score", 0)

        if score < EARLY_ALERT_MIN_SCORE:
            return
        if not address:
            return

        with seen_lock:
            if not is_token_new(address, seen):
                return
            mark_token_seen(address, seen)
            early_alerted.add(address.lower())

        msg = (
            f"<b>EARLY ALERT</b> (forense={score:.1f}/10)\n\n"
            f"<b>{token.get('name', '?')}</b>\n"
            f"Network: {token.get('network', '?')}\n"
            f"Liquidity: ${token.get('liquidity_usd', 0):,.0f}\n"
            f"Volume 24h: ${token.get('volume_24h', 0):,.0f}\n"
            f"Price: ${token.get('price_usd', 0):.6f}\n"
            f"Change 24h: {token.get('price_change_24h', 0):.1f}%\n\n"
            f"Full analysis in progress..."
        )
        send_telegram(msg)
        with alerts_lock:
            alerts_sent += 1
        log.info(f"  EARLY ALERT: {token.get('name')} (forense={score:.1f})")

    try:
        # ── Phase 1: Scout ────────────────────────────────────────────────
        t0 = time.monotonic()
        scout = Scout()
        candidates = scout.scan()
        log.info(f"Scout: {len(candidates)} candidates ({time.monotonic()-t0:.1f}s)")
        if not candidates:
            return 0

        # ── Phase 2: Forense (parallel, with early alerting) ─────────────
        t0 = time.monotonic()
        forense = Forense()
        audited = forense.audit(candidates, on_pass_callback=_on_audit_pass)
        log.info(f"Forense: {len(audited)} passed ({time.monotonic()-t0:.1f}s)")
        if not audited:
            log.info(f"Scan cycle complete. {alerts_sent} early alerts sent.")
            with seen_lock:
                save_seen_tokens(seen)
            return alerts_sent

        # ── Phase 3: Sentiment (Narrator) ─────────────────────────────────
        t0 = time.monotonic()
        narrator = Narrator()
        with_sentiment = narrator.analyze(audited)
        log.info(f"Narrator: done ({time.monotonic()-t0:.1f}s)")

        # ── Phase 4: Social Intelligence ──────────────────────────────────
        t0 = time.monotonic()
        try:
            social = SocialIntel()
            with_social = social.analyze_batch(with_sentiment)
        except Exception as e:
            log.warning(f"Social intel skipped: {e}")
            with_social = with_sentiment
        log.info(f"Social Intel: done ({time.monotonic()-t0:.1f}s)")

        # ── Phase 5: Get wallet signals from background thread ────────────
        wallet_signals = []
        if wallet_signals_holder and "signals" in wallet_signals_holder:
            wallet_signals = wallet_signals_holder["signals"]
            log.info(f"Wallet signals: {len(wallet_signals)} from background thread")

        # ── Phase 6: Technical analysis ───────────────────────────────────
        t0 = time.monotonic()
        quant = Quant()
        with_technicals = quant.analyze(with_social)
        log.info(f"Quant: done ({time.monotonic()-t0:.1f}s)")

        # ── Phase 7: Triple Confirmation ──────────────────────────────────
        tc = TripleConfirmation()
        confirmed = tc.evaluate(with_technicals, wallet_signals)

        # Compute enhanced scores
        executor = Executor(capital=config.DEFAULT_CAPITAL, mode="paper")
        scored = executor._compute_composite_scores(confirmed)

        for token in scored:
            enhanced = tc.compute_enhanced_composite(token)
            token["enhanced_composite"] = enhanced

        # ── Phase 8: Send alerts (skip early-alerted tokens) ─────────────
        # Priority 1: Triple-confirmed alpha alerts
        alpha_alerts = tc.get_high_priority_alerts(scored)
        for token in alpha_alerts:
            address = token.get("address", "")
            if address.lower() in early_alerted:
                continue

            with seen_lock:
                if not is_token_new(address, seen):
                    continue
                mark_token_seen(address, seen)

            alert_msg = tc.format_alpha_alert(token)
            send_telegram(alert_msg)
            save_alert(token, alert_msg)
            with alerts_lock:
                alerts_sent += 1
            log.info(f"  ALPHA ALERT: {token.get('name')} "
                     f"(alpha={token.get('alpha_score', 0):.1f})")

        # Priority 2: Standard high-score alerts (not already sent as alpha or early)
        alpha_addresses = set(t.get("address", "").lower() for t in alpha_alerts)
        skip_addresses = alpha_addresses | early_alerted
        for token in scored:
            address = token.get("address", "")
            if address.lower() in skip_addresses:
                continue

            composite = token.get("enhanced_composite", token.get("composite_score", 0))
            change = token.get("price_change_24h", 0)

            if composite < ALERT_MIN_SCORE:
                continue
            if change > ALERT_MAX_PUMP_PCT:
                continue

            with seen_lock:
                if not is_token_new(address, seen):
                    continue
                mark_token_seen(address, seen)

            alert_msg = format_alert(token)
            send_telegram(alert_msg)
            save_alert(token, alert_msg)
            with alerts_lock:
                alerts_sent += 1
            log.info(f"  ALERT: {token.get('name')} (score={composite:.2f})")

        with seen_lock:
            save_seen_tokens(seen)

        # Also send wallet buy alerts
        for sig in wallet_signals:
            addr = sig.get("token_address", "")
            if addr and addr.lower() not in early_alerted:
                with seen_lock:
                    if is_token_new(addr, seen) and sig.get("liquidity_usd", 0) >= 30000:
                        from alpha.smart_wallet_tracker import format_wallet_alert
                        alert_msg = format_wallet_alert(sig)
                        send_telegram(alert_msg)
                        mark_token_seen(addr, seen)
                        with alerts_lock:
                            alerts_sent += 1

        elapsed = time.monotonic() - cycle_start
        log.info(f"Scan cycle complete. {alerts_sent} alerts sent in {elapsed:.1f}s.")
        return alerts_sent

    except Exception as e:
        log.error(f"Alpha scan error: {e}", exc_info=True)
        try:
            send_telegram(f"<b>Alpha Monitor Error</b>\n\n{str(e)[:200]}")
        except Exception:
            pass
        return 0


def _run_wallet_background(stop_event: threading.Event, result_holder: dict):
    """Background thread: WebSocket (primary) + polling fallback.

    v4.0: Starts Helius WebSocket for real-time Solana wallet monitoring.
    Polling interval extends to 10min when WebSocket is active (was 2min).
    """
    from alpha.smart_wallet_tracker import SmartWalletTracker, format_wallet_alert

    try:
        tracker = SmartWalletTracker()
        if not tracker.db.load_wallets():
            log.info("Wallet background: no wallets tracked")
            return

        log.info(f"Wallet background thread started "
                 f"({len(tracker.db.load_wallets())} wallets)")

        # Callback: when WS detects a buy, enrich + add to signals immediately
        def _on_ws_signal(signal):
            """Immediate processing of WebSocket buy events."""
            if signal:
                current = result_holder.get("signals", [])
                current.append(signal)
                result_holder["signals"] = current
                # Immediate Telegram alert for real-time WS events
                if signal.get("liquidity_usd", 0) >= config.SCAN_MIN_LIQUIDITY:
                    send_telegram("\u26a1 " + format_wallet_alert(signal))
                    log.info(f"[WS] Immediate alert: {signal.get('wallet_label')} -> {signal.get('token_name')}")

        # Start WebSocket (primary, real-time)
        tracker.start_websocket(_on_ws_signal)

        while not stop_event.is_set():
            try:
                t0 = time.monotonic()
                signals = tracker.scan_all_wallets()
                signals = tracker.enrich_signals(signals)
                result_holder["signals"] = signals
                elapsed = time.monotonic() - t0
                log.info(f"Wallet background: {len(signals)} signals ({elapsed:.1f}s)")
            except Exception as e:
                log.warning(f"Wallet background error: {e}")

            # Adjust interval based on WS state
            interval = config_alpha.WS_FALLBACK_POLL_INTERVAL if tracker.ws_connected else WALLET_CHECK_INTERVAL
            stop_event.wait(timeout=interval)

        tracker.stop_websocket()
    except Exception as e:
        log.error(f"Wallet background thread fatal: {e}")


def run_wallet_monitor():
    """Lightweight wallet-only monitoring loop (standalone mode)."""
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
    """Main daemon loop.

    v3.0: Wallet tracker runs in a separate background thread, independent of
    the main scan pipeline. Latency target < 30s for wallet signals.
    """
    setup_logging()
    ensure_data_dirs()

    if wallets_only:
        run_wallet_monitor()
        return

    log.info(f"Alpha Monitor v3.0 daemon starting (interval: {interval_min} min)")

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
        "<b>Alpha Monitor v3.0 Started</b>\n\n"
        f"Scan interval: {interval_min} min\n"
        f"APIs: {api_str}\n"
        f"Min score: {ALERT_MIN_SCORE}/10\n"
        f"Early alerts: forense >= {EARLY_ALERT_MIN_SCORE}"
    )

    # Start wallet tracker in background thread
    wallet_stop = threading.Event()
    wallet_signals = {"signals": []}
    wallet_thread = threading.Thread(
        target=_run_wallet_background,
        args=(wallet_stop, wallet_signals),
        daemon=True,
        name="wallet-bg"
    )
    wallet_thread.start()
    log.info("Wallet background thread launched")

    running = True
    def _shutdown(sig, frame):
        nonlocal running
        running = False
        wallet_stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    cycle = 0
    while running:
        cycle += 1
        log.info(f"\n--- Alpha Cycle #{cycle} at {now_utc().strftime('%H:%M UTC')} ---")

        try:
            alerts = run_alpha_scan_cycle(wallet_signals_holder=wallet_signals)
            log.info(f"Cycle #{cycle}: {alerts} alerts")
        except Exception as e:
            log.error(f"Cycle #{cycle} failed: {e}", exc_info=True)

        if not running:
            break

        for _ in range(interval_min * 60):
            if not running:
                break
            time.sleep(1)

    wallet_stop.set()
    wallet_thread.join(timeout=5)
    send_telegram("<b>Alpha Monitor v3.0 Stopped</b>")


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
