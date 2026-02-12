#!/usr/bin/env python3
"""
Crypto Swarm Alert Monitor - Real-time Telegram Notifications
Runs as a background daemon, scanning every 30 minutes.
Alerts ONLY on new high-score tokens that haven't already pumped.

Setup:
  1. Create a Telegram bot: message @BotFather on Telegram, send /newbot
  2. Copy the bot token
  3. Message your bot once, then get your chat_id from:
     https://api.telegram.org/bot<TOKEN>/getUpdates
  4. Set environment variables:
     export TELEGRAM_BOT_TOKEN="your_token_here"
     export TELEGRAM_CHAT_ID="your_chat_id_here"
  5. Run:
     nohup python3 alert_monitor.py &
     # or: python3 alert_monitor.py --once  (single scan)

Usage:
  python3 alert_monitor.py                  # daemon mode (every 30 min)
  python3 alert_monitor.py --once           # single scan + alert
  python3 alert_monitor.py --interval 15    # custom interval (minutes)
  python3 alert_monitor.py --test           # send test message to verify setup
"""
import argparse
import json
import os
import signal
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import config
from utils import setup_logging, get_logger, load_json, save_json, ensure_data_dirs, now_utc

log = get_logger("alert_monitor")

# ─── Configuration ──────────────────────────────────────────────────────────

ALERT_MIN_SCORE = 6.8              # minimum composite score to alert (v7.5: set to 6.8 for backtesting)
ALERT_MAX_PUMP_PCT = 50.0          # skip tokens already up >50%
ALERT_COOLDOWN_HOURS = 24          # don't re-alert same token within 24h
SCAN_INTERVAL_MIN = 30             # minutes between scans
ALERTS_FILE = os.path.join(config.DATA_DIR, "alerts.json")
SEEN_TOKENS_FILE = os.path.join(config.DATA_DIR, "seen_tokens.json")


def _update_min_score(new_score: float):
    global ALERT_MIN_SCORE
    ALERT_MIN_SCORE = new_score

# Telegram config from environment
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ─── Telegram Sender ────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        # Fallback: print to stdout and try desktop notification
        print(f"\n{'='*50}\nALERT: {message}\n{'='*50}\n")
        _desktop_notify(message)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log.info("Telegram alert sent successfully")
                return True
            else:
                log.warning(f"Telegram API error: {result}")
                return False
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        log.warning(f"Telegram send failed: {e}")
        return False


def _desktop_notify(message: str):
    """Fallback: Linux desktop notification via notify-send."""
    try:
        import subprocess
        # Strip HTML tags for desktop notification
        clean = message.replace("<b>", "").replace("</b>", "")
        clean = clean.replace("<code>", "").replace("</code>", "")
        subprocess.run(
            ["notify-send", "-u", "critical", "Crypto Swarm Alert", clean[:200]],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass


# ─── Token Tracking ─────────────────────────────────────────────────────────

def load_seen_tokens() -> dict:
    """Load previously seen token addresses with timestamps."""
    data = load_json(SEEN_TOKENS_FILE)
    return data if isinstance(data, dict) else {}


def save_seen_tokens(seen: dict):
    save_json(SEEN_TOKENS_FILE, seen)


def is_token_new(address: str, seen: dict) -> bool:
    """Check if a token hasn't been alerted within the cooldown period."""
    if not address:
        return True
    key = address.lower()
    if key not in seen:
        return True
    last_seen = seen[key]
    try:
        last_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        hours_since = (now_utc() - last_dt).total_seconds() / 3600
        return hours_since > ALERT_COOLDOWN_HOURS
    except (ValueError, TypeError):
        return True


def mark_token_seen(address: str, seen: dict):
    if address:
        seen[address.lower()] = now_utc().isoformat()


# ─── Alert Formatting ───────────────────────────────────────────────────────

def format_alert(token: dict) -> str:
    """Format a token alert for Telegram."""
    name = token.get("name", "Unknown")
    network = token.get("network", "?").upper()
    address = token.get("address", "N/A")
    score = token.get("composite_score", 0)
    price = token.get("price_usd", 0)
    change_24h = token.get("price_change_24h", 0)
    liquidity = token.get("liquidity_usd", 0)
    volume = token.get("volume_24h", 0)
    forense = token.get("forense_score", 0)
    rsi = token.get("rsi")

    # Score emoji
    if score >= 8:
        emoji = "🔥"
    elif score >= 7.5:
        emoji = "🚨"
    else:
        emoji = "📊"

    # Network explorer URL
    if network == "SOLANA":
        explorer = f"https://solscan.io/token/{address}"
    elif network == "BASE":
        explorer = f"https://basescan.org/token/{address}"
    elif network in ("ETHEREUM", "ETH"):
        explorer = f"https://etherscan.io/token/{address}"
    else:
        explorer = f"https://dexscreener.com/{token.get('chain', 'solana')}/{address}"

    dex_url = f"https://dexscreener.com/{token.get('chain', 'solana')}/{address}"

    # Key signals
    signals = []
    if token.get("accumulation_detected"):
        signals.append("Accumulation")
    if token.get("discourse_quality") == "technical":
        signals.append("Tech Project")
    quant_sigs = token.get("quant_signals", [])
    for s in quant_sigs:
        if "oversold" in s:
            signals.append("Oversold (RSI)")
        if "volume_surging" in s:
            signals.append("Vol Surging")
        if "near_support" in s:
            signals.append("Near Support")

    msg = (
        f"{emoji} <b>ALERTA CRYPTO SWARM</b>\n\n"
        f"<b>{name}</b> ({network})\n"
        f"Score: <b>{score:.2f}/10</b>\n"
        f"Price: <b>${price:.8f}</b> ({change_24h:+.1f}% 24h)\n"
        f"Liquidity: ${liquidity:,.0f}\n"
        f"Volume 24h: ${volume:,.0f}\n"
        f"Safety (Forense): {forense:.1f}/10\n"
    )
    if rsi is not None:
        msg += f"RSI: {rsi:.0f}\n"
    if signals:
        msg += f"Signals: {', '.join(signals)}\n"
    msg += (
        f"\n<code>{address}</code>\n"
        f"\n<a href=\"{dex_url}\">DexScreener</a> | "
        f"<a href=\"{explorer}\">Explorer</a>"
    )
    return msg


# ─── Alert History ───────────────────────────────────────────────────────────

def save_alert(token: dict, alert_msg: str):
    """Append alert to alerts.json for history."""
    alerts = load_json(ALERTS_FILE)
    if not isinstance(alerts, list):
        alerts = []

    alerts.append({
        "timestamp": now_utc().isoformat(),
        "name": token.get("name"),
        "address": token.get("address"),
        "network": token.get("network"),
        "composite_score": token.get("composite_score"),
        "price_usd": token.get("price_usd"),
        "price_change_24h": token.get("price_change_24h"),
        "liquidity_usd": token.get("liquidity_usd"),
        "forense_score": token.get("forense_score"),
        "rsi": token.get("rsi"),
    })

    # Keep last 500 alerts
    if len(alerts) > 500:
        alerts = alerts[-500:]

    save_json(ALERTS_FILE, alerts)


# ─── Main Scan & Alert Logic ────────────────────────────────────────────────

def run_scan_and_alert():
    """Run one scan cycle and send alerts for qualifying tokens."""
    from scanner import Scout
    from auditor import Forense
    from sentiment import Narrator
    from technical import Quant
    from portfolio import Executor

    log.info("=" * 50)
    log.info("ALERT MONITOR: Starting scan cycle")
    log.info("=" * 50)

    seen = load_seen_tokens()
    alerts_sent = 0

    try:
        # Run the pipeline
        scout = Scout()
        candidates = scout.scan()
        log.info(f"Scout: {len(candidates)} candidates")

        if not candidates:
            log.info("No candidates found this cycle")
            return 0

        forense = Forense()
        audited = forense.audit(candidates)
        log.info(f"Forense: {len(audited)} passed audit")

        if not audited:
            log.info("No candidates passed audit")
            return 0

        narrator = Narrator()
        with_sentiment = narrator.analyze(audited)

        quant = Quant()
        with_technicals = quant.analyze(with_sentiment)

        executor = Executor(capital=config.DEFAULT_CAPITAL, mode="paper")
        scored = executor._compute_composite_scores(with_technicals)

        # Filter for alertable tokens
        for token in scored:
            composite = token.get("composite_score", 0)
            change = token.get("price_change_24h", 0)
            address = token.get("address", "")

            # Must meet minimum score
            if composite < ALERT_MIN_SCORE:
                continue

            # Must not have already pumped
            if change > ALERT_MAX_PUMP_PCT:
                log.info(f"  Skipping {token.get('name')}: already pumped {change:.0f}%")
                continue

            # Must be a new token we haven't alerted recently
            if not is_token_new(address, seen):
                log.info(f"  Skipping {token.get('name')}: already alerted within {ALERT_COOLDOWN_HOURS}h")
                continue

            # Send alert!
            alert_msg = format_alert(token)
            success = send_telegram(alert_msg)

            mark_token_seen(address, seen)
            save_alert(token, alert_msg)
            alerts_sent += 1

            log.info(f"  ALERT: {token.get('name')} (score={composite:.2f})")

        save_seen_tokens(seen)
        log.info(f"Scan cycle complete. {alerts_sent} alerts sent.")
        return alerts_sent

    except Exception as e:
        log.error(f"Scan cycle error: {e}", exc_info=True)
        try:
            send_telegram(f"⚠️ <b>Swarm Error</b>\n\n{str(e)[:200]}")
        except Exception:
            pass
        return 0


# ─── Daemon Loop ─────────────────────────────────────────────────────────────

def daemon_loop(interval_min: int = 30):
    """Run continuous scan loop."""
    setup_logging()
    ensure_data_dirs()

    log.info(f"Alert Monitor daemon starting (interval: {interval_min} min)")
    log.info(f"Telegram configured: {'YES' if TELEGRAM_BOT_TOKEN else 'NO'}")
    log.info(f"Min score: {ALERT_MIN_SCORE}, Max pump: {ALERT_MAX_PUMP_PCT}%")

    # Graceful shutdown
    running = True

    def _shutdown(sig, frame):
        nonlocal running
        log.info("Shutdown signal received. Stopping after current cycle...")
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Send startup notification
    send_telegram(
        "🟢 <b>Crypto Swarm Monitor Started</b>\n\n"
        f"Scanning every {interval_min} minutes\n"
        f"Min score: {ALERT_MIN_SCORE}/10\n"
        f"Max pump filter: {ALERT_MAX_PUMP_PCT}%"
    )

    cycle = 0
    while running:
        cycle += 1
        log.info(f"\n--- Cycle #{cycle} at {now_utc().strftime('%H:%M UTC')} ---")

        try:
            alerts = run_scan_and_alert()
            log.info(f"Cycle #{cycle} done: {alerts} alerts sent")
        except Exception as e:
            log.error(f"Cycle #{cycle} failed: {e}", exc_info=True)

        if not running:
            break

        # Sleep in small intervals for responsive shutdown
        sleep_secs = interval_min * 60
        log.info(f"Next scan in {interval_min} minutes...")
        for _ in range(sleep_secs):
            if not running:
                break
            time.sleep(1)

    send_telegram("🔴 <b>Crypto Swarm Monitor Stopped</b>")
    log.info("Alert Monitor daemon stopped")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Crypto Swarm Alert Monitor - Telegram Notifications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Setup:
  1. Create bot: message @BotFather on Telegram -> /newbot
  2. Set token: export TELEGRAM_BOT_TOKEN="your_token"
  3. Get chat_id: message your bot, then visit:
     https://api.telegram.org/bot<TOKEN>/getUpdates
  4. Set chat_id: export TELEGRAM_CHAT_ID="your_id"
  5. Run: nohup python3 alert_monitor.py &

Examples:
  python3 alert_monitor.py              # daemon (every 30 min)
  python3 alert_monitor.py --once       # single scan
  python3 alert_monitor.py --interval 15
  python3 alert_monitor.py --test
        """,
    )
    parser.add_argument("--once", action="store_true", help="Run single scan then exit")
    parser.add_argument("--interval", type=int, default=SCAN_INTERVAL_MIN,
                        help=f"Minutes between scans (default: {SCAN_INTERVAL_MIN})")
    parser.add_argument("--test", action="store_true", help="Send test message to verify Telegram")
    parser.add_argument("--min-score", type=float, default=ALERT_MIN_SCORE,
                        help=f"Minimum composite score to alert (default: {ALERT_MIN_SCORE})")

    args = parser.parse_args()

    if args.min_score != ALERT_MIN_SCORE:
        _update_min_score(args.min_score)

    if args.test:
        setup_logging()
        print("Sending test message to Telegram...")
        success = send_telegram(
            "🧪 <b>Test Alert</b>\n\n"
            "Crypto Swarm Alert Monitor is working!\n"
            f"Time: {now_utc().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        if success:
            print("Test message sent successfully!")
        else:
            print("Failed to send. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return

    if args.once:
        setup_logging()
        ensure_data_dirs()
        log.info("Running single scan...")
        alerts = run_scan_and_alert()
        print(f"\nScan complete: {alerts} alerts sent")
        print(f"Alert history: {ALERTS_FILE}")
        return

    daemon_loop(args.interval)


if __name__ == "__main__":
    main()
