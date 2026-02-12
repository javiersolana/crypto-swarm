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
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import config_alpha
from utils import setup_logging, get_logger, load_json, save_json, ensure_data_dirs, now_utc, safe_float
from paper_trader import PaperTrader
from alert_monitor import (
    send_telegram, load_seen_tokens, save_seen_tokens, is_token_new,
    mark_token_seen, format_alert, save_alert, ALERT_MIN_SCORE, ALERT_MAX_PUMP_PCT
)

log = get_logger("alpha_monitor")

# Alpha-specific settings
ALPHA_SCAN_INTERVAL = 15  # minutes (more frequent than v1's 30min)
WALLET_CHECK_INTERVAL = 120  # seconds for wallet-only checks
EARLY_ALERT_MIN_SCORE = 8.0  # score threshold for immediate early alerts during audit
SIGNAL_ACCUMULATION_TTL = 1800  # 30 minutes — wallet signals expire after this


class SignalAccumulator:
    """Thread-safe accumulator for wallet signals with TTL-based expiration.

    v7.0: Instead of overwriting signals each scan cycle, signals accumulate
    over a 30-minute window. Each signal is timestamped; expired signals are
    pruned on every read. This ensures triple confirmation sees ALL recent
    whale activity, not just the last scan.
    """

    def __init__(self, ttl_seconds: int = SIGNAL_ACCUMULATION_TTL):
        self._lock = threading.Lock()
        self._signals: list[dict] = []  # each has "_acc_ts" timestamp
        self._ttl = ttl_seconds

    def update(self, new_signals: list[dict]):
        """Add new signals, deduplicating by (wallet_address, token_address)."""
        now = time.time()
        with self._lock:
            # Build set of existing keys for dedup
            existing_keys = set()
            for s in self._signals:
                key = (s.get("wallet_address", "").lower(),
                       s.get("token_address", "").lower())
                existing_keys.add(key)

            added = 0
            for sig in new_signals:
                key = (sig.get("wallet_address", "").lower(),
                       sig.get("token_address", "").lower())
                if key not in existing_keys:
                    sig["_acc_ts"] = now
                    self._signals.append(sig)
                    existing_keys.add(key)
                    added += 1

            # Prune expired
            cutoff = now - self._ttl
            self._signals = [s for s in self._signals if s.get("_acc_ts", 0) > cutoff]

            if added > 0:
                log.info(f"Signal accumulator: +{added} new, "
                         f"{len(self._signals)} total (TTL={self._ttl}s)")

    def get_all(self) -> list[dict]:
        """Return all non-expired signals."""
        now = time.time()
        cutoff = now - self._ttl
        with self._lock:
            self._signals = [s for s in self._signals if s.get("_acc_ts", 0) > cutoff]
            return list(self._signals)

    def count(self) -> int:
        with self._lock:
            return len(self._signals)


# v8.6: Missed Opportunities Tracker
_missed_opp_lock = threading.Lock()


def _track_missed_opportunity(token: dict, reason: str):
    """Record a rejected token for missed opportunity analysis."""
    try:
        entry = {
            "address": token.get("address", ""),
            "chain": token.get("chain", ""),
            "name": token.get("name") or token.get("address", "?")[:12],
            "price_at_rejection": safe_float(token.get("price_usd", 0)),
            "liquidity_usd": safe_float(token.get("liquidity_usd", 0)),
            "forense_score": safe_float(token.get("forense_score", 0)),
            "rejection_reason": reason,
            "rejected_at": datetime.now(timezone.utc).isoformat(),
            "rejected_ts": time.time(),
            "checked": False,
            "final_pump_pct": None,
        }
        with _missed_opp_lock:
            data = load_json(config.MISSED_OPPORTUNITIES_FILE) or {"rejected": [], "missed": []}
            # Deduplicate by address
            existing = set(r.get("address", "").lower() for r in data["rejected"])
            if entry["address"].lower() not in existing:
                data["rejected"].append(entry)
                save_json(config.MISSED_OPPORTUNITIES_FILE, data)
    except Exception as e:
        log.debug(f"Missed opp tracking error: {e}")


def _check_missed_opportunities():
    """Check if any rejected tokens pumped >50%. Runs periodically from exit manager."""
    from api_client import DexScreenerClient
    dex = DexScreenerClient()
    threshold = getattr(config, 'MISSED_OPP_PUMP_THRESHOLD', 50)
    ttl = getattr(config, 'MISSED_OPP_TTL', 86400)

    try:
        with _missed_opp_lock:
            data = load_json(config.MISSED_OPPORTUNITIES_FILE) or {"rejected": [], "missed": []}

        now = time.time()
        unchecked = [r for r in data["rejected"]
                     if not r.get("checked") and now - r.get("rejected_ts", 0) < ttl]

        if not unchecked:
            return

        # Batch by chain
        by_chain = {}
        for r in unchecked:
            chain = r.get("chain", "solana")
            by_chain.setdefault(chain, []).append(r)

        newly_missed = []
        for chain, rejects in by_chain.items():
            addrs = [r["address"] for r in rejects if r.get("address")]
            for i in range(0, len(addrs), 30):
                batch = addrs[i:i + 30]
                try:
                    pairs = dex.get_tokens_batch(chain, batch)
                    price_map = {}
                    for p in pairs:
                        addr = p.get("baseToken", {}).get("address", "").lower()
                        price = safe_float(p.get("priceUsd", 0))
                        if addr and price > 0:
                            price_map[addr] = price
                except Exception:
                    continue

                for r in rejects:
                    if r["address"].lower() in price_map:
                        current = price_map[r["address"].lower()]
                        rejection_price = r.get("price_at_rejection", 0)
                        if rejection_price > 0:
                            pump_pct = ((current - rejection_price) / rejection_price) * 100
                            r["checked"] = True
                            r["final_pump_pct"] = round(pump_pct, 2)
                            r["checked_at"] = datetime.now(timezone.utc).isoformat()
                            if pump_pct >= threshold:
                                newly_missed.append(r)
                                log.warning(f"  [MISSED OPP] {r['name']} pumped {pump_pct:+.1f}% "
                                           f"after rejection ({r['rejection_reason']})")

        # Prune expired entries
        data["rejected"] = [r for r in data["rejected"]
                           if now - r.get("rejected_ts", 0) < ttl]
        data["missed"].extend(newly_missed)

        with _missed_opp_lock:
            save_json(config.MISSED_OPPORTUNITIES_FILE, data)

    except Exception as e:
        log.debug(f"Missed opp check error: {e}")


def _compute_tier_amount(token: dict, wallet_signals: list = None) -> float:
    """v8.8: Compute position size based on confidence tier.

    Tier A (0.05 SOL): Whale signal + forense>=8 + RSI<60 → highest confidence
    Tier B (0.025 SOL): forense>=8 + RSI<70 + h1_vol>$15k
    Tier C (0.015 SOL): forense>=7.5 + h1_vol>$10k + chain=base → lowest confidence

    Falls back to Tier C if no tier matches.
    """
    forense = safe_float(token.get("forense_score", 0))
    chain = token.get("chain", "")
    source = token.get("source", "")

    # Extract RSI from forense_flags if available
    rsi_val = None
    for flag in token.get("forense_flags", []):
        if isinstance(flag, str) and flag.startswith("rsi_"):
            try:
                rsi_val = float(flag.split("_")[-1])
            except (ValueError, IndexError):
                pass

    # Check if this token has whale signals
    has_whale = source == "whale_inject"
    if not has_whale and wallet_signals:
        addr = token.get("address", "").lower()
        has_whale = any(
            s.get("token_address", "").lower() == addr
            for s in wallet_signals
        )

    tier_a = getattr(config, 'PAPER_TIER_A_SOL', 0.05)
    tier_b = getattr(config, 'PAPER_TIER_B_SOL', 0.025)
    tier_c = getattr(config, 'PAPER_TIER_C_SOL', 0.015)

    # Tier A: whale + forense>=8 + RSI<60
    if has_whale and forense >= 8.0 and (rsi_val is None or rsi_val < 60):
        log.info(f"  [TIER A] {token.get('name','?')} — whale={has_whale}, "
                 f"forense={forense:.1f}, rsi={rsi_val} → {tier_a} SOL")
        return tier_a

    # Tier B: forense>=8 + RSI<70
    if forense >= 8.0 and (rsi_val is None or rsi_val < 70):
        log.info(f"  [TIER B] {token.get('name','?')} — forense={forense:.1f}, "
                 f"rsi={rsi_val} → {tier_b} SOL")
        return tier_b

    # Tier C: forense>=7.5 (default for anything that passes audit)
    log.info(f"  [TIER C] {token.get('name','?')} — forense={forense:.1f}, "
             f"chain={chain}, rsi={rsi_val} → {tier_c} SOL")
    return tier_c


def run_alpha_scan_cycle(wallet_signals_holder=None,
                         paper_trader: PaperTrader = None) -> int:
    """Run one alpha-enhanced scan cycle. Returns number of alerts sent.

    v7.0: wallet_signals_holder is now a SignalAccumulator (or legacy dict).
    Signals accumulate over 30min window for better triple confirmation matching.
    """
    from scanner import Scout
    from auditor import Forense
    from sentiment import Narrator
    from technical import Quant
    from portfolio import Executor
    from alpha.social_intel import SocialIntel
    from alpha.triple_confirm import TripleConfirmation

    log.info("=" * 50)
    log.info("ALPHA MONITOR v8.8 - Starting scan cycle")
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

        token_name = token.get("name") or token.get("address", "unknown")[:8]
        msg = (
            f"<b>EARLY ALERT</b> (forense={score:.1f}/10)\n\n"
            f"<b>{token_name}</b>\n"
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
        log.info(f"  EARLY ALERT: {token_name} (forense={score:.1f})")

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

        # v8.6: Track Anti-Fomo rejected tokens for missed opportunity analysis
        audited_addrs = set(t.get("address", "").lower() for t in audited)
        for c in candidates:
            if c.get("address", "").lower() not in audited_addrs:
                reason = c.get("forense_reject_reason", "")
                if reason and ("Anti-Fomo" in reason or "RSI" in reason.upper()
                               or "overbought" in reason.lower()):
                    _track_missed_opportunity(c, reason)

        if not audited:
            log.info(f"Scan cycle complete. {alerts_sent} early alerts sent.")
            with seen_lock:
                save_seen_tokens(seen)
            return alerts_sent

        # ── Phase 3: Sentiment (Narrator) ─────────────────────────────────
        # v6.0: Priority graduated tokens skip Narrator (time-sensitive)
        t0 = time.monotonic()
        priority_tokens = [t for t in audited if t.get("priority_graduated")]
        normal_tokens = [t for t in audited if not t.get("priority_graduated")]

        if normal_tokens:
            narrator = Narrator()
            normal_tokens = narrator.analyze(normal_tokens)

        for t in priority_tokens:
            t["narrator_score"] = 5.0
            t["narrator_signals"] = ["skipped_priority_graduated"]

        with_sentiment = priority_tokens + normal_tokens
        log.info(f"Narrator: done ({time.monotonic()-t0:.1f}s) "
                 f"({len(priority_tokens)} priority skipped)")

        # ── Phase 4: Social Intelligence ──────────────────────────────────
        t0 = time.monotonic()
        try:
            social = SocialIntel()
            with_social = social.analyze_batch(with_sentiment)
        except Exception as e:
            log.warning(f"Social intel skipped: {e}")
            with_social = with_sentiment
        log.info(f"Social Intel: done ({time.monotonic()-t0:.1f}s)")

        # ── Phase 5: Get wallet signals from accumulator (v7.0) ─────────────
        wallet_signals = []
        if wallet_signals_holder:
            if isinstance(wallet_signals_holder, SignalAccumulator):
                wallet_signals = wallet_signals_holder.get_all()
            elif isinstance(wallet_signals_holder, dict) and "signals" in wallet_signals_holder:
                wallet_signals = wallet_signals_holder["signals"]
            log.info(f"Wallet signals: {len(wallet_signals)} from accumulator (30min window)")

        # ── Debug: whale signal state ─────────────────────────────────────
        if wallet_signals:
            whale_token_addrs = set()
            for ws in wallet_signals:
                whale_token_addrs.add(ws.get("token_address", "").lower())
            log.info(f"[DEBUG] Whale signals active: {len(wallet_signals)} signals, "
                     f"{len(whale_token_addrs)} unique tokens")
            for ws in wallet_signals[:10]:
                log.info(f"[DEBUG]   Wallet={ws.get('wallet_label','?')} "
                         f"Token={ws.get('token_name','?')} "
                         f"({ws.get('token_address','')[:20]}...) "
                         f"chain={ws.get('chain','?')} "
                         f"liq=${ws.get('liquidity_usd', 0):,.0f}")
            if len(wallet_signals) > 10:
                log.info(f"[DEBUG]   ... and {len(wallet_signals)-10} more signals")

            # Log match analysis: how many wallet tokens are in scanner candidates?
            candidate_addrs = set(t.get("address", "").lower() for t in with_social)
            matches = whale_token_addrs & candidate_addrs
            only_whale = whale_token_addrs - candidate_addrs
            log.info(f"[DEBUG] Scanner candidates: {len(candidate_addrs)}, "
                     f"Whale tokens: {len(whale_token_addrs)}, "
                     f"MATCHES: {len(matches)}, WHALE-ONLY: {len(only_whale)}")
            if only_whale:
                log.info(f"[DEBUG] Whale-only tokens (will be injected): "
                         f"{list(only_whale)[:5]}")
        else:
            log.info("[DEBUG] Whale signals active: NONE (0 signals in accumulator)")

        # ── Phase 6: Technical analysis ───────────────────────────────────
        t0 = time.monotonic()
        quant = Quant()
        with_technicals = quant.analyze(with_social)
        log.info(f"Quant: done ({time.monotonic()-t0:.1f}s)")

        # ── Phase 7: Triple Confirmation ──────────────────────────────────
        tc = TripleConfirmation()
        confirmed = tc.evaluate(with_technicals, wallet_signals)

        # ── Phase 7 Summary: match diagnostics ───────────────────────────
        _ws_addrs = set(s.get("token_address", "").lower() for s in wallet_signals)
        _cand_addrs = set(t.get("address", "").lower() for t in with_technicals)
        _match_count = len(_ws_addrs & _cand_addrs)
        log.info(f"[DEBUG] Signals en memoria: {len(wallet_signals)} | "
                 f"Candidatos Scout: {len(with_technicals)} | "
                 f"Matches encontrados: {_match_count}")

        # ── Debug: post-confirmation analysis ─────────────────────────────
        whale_addr_set = set(s.get("token_address", "").lower() for s in wallet_signals)
        injected_count = sum(1 for t in confirmed if t.get("source") == "whale_inject")
        log.info(f"[DEBUG] Post-confirmation: {len(confirmed)} tokens "
                 f"({injected_count} whale-injected)")
        for token in confirmed[:20]:
            addr = token.get("address", "").lower()
            has_whale = addr in whale_addr_set
            log.info(f"[DEBUG] Token {token.get('name','?')}: "
                     f"Scout={token.get('scout_score', 0):.1f}, "
                     f"Forense={token.get('forense_score', 0):.1f}, "
                     f"Alpha={token.get('alpha_score', 0):.1f} "
                     f"({token.get('alpha_signal_count', 0)} signals), "
                     f"Whale={'SI' if has_whale else 'NO'}, "
                     f"Source={token.get('source', 'scanner')}")

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
            log.info(f"  ALPHA ALERT: {token.get('name') or token.get('address', '?')[:8]} "
                     f"(alpha={token.get('alpha_score', 0):.1f})")

            # v8.8: Paper trade on PRIORITY alerts with tiered sizing
            if paper_trader:
                tier_amount = _compute_tier_amount(token, wallet_signals)
                trade = paper_trader.open_trade(token, amount_sol=tier_amount)
                if trade:
                    log.info(f"  PAPER BUY: {trade['token_name']} @ ${trade['entry_price']:.8f} "
                             f"(tier={tier_amount} SOL)")
                    send_telegram(paper_trader.format_open_message(trade))

        # v8.6: Build whale wallet count per token for Double Whale Confirmation
        _whale_wallet_counts = {}  # {token_address_lower: set(wallet_addresses)}
        for ws in wallet_signals:
            tok = ws.get("token_address", "").lower()
            wal = ws.get("wallet_address", "").lower()
            if tok and wal:
                _whale_wallet_counts.setdefault(tok, set()).add(wal)

        min_whales = getattr(config, 'DOUBLE_WHALE_MIN_WALLETS', 2)

        # v7.5: Whale fast-track paper trading (FORCED ACTIVATION)
        # v8.6: Requires 2+ unique wallets (Double Whale Confirmation)
        alpha_addresses = set(t.get("address", "").lower() for t in alpha_alerts)
        if paper_trader:
            whale_traded = 0
            for token in scored:
                if token.get("source") != "whale_inject":
                    continue
                address = token.get("address", "")
                if address.lower() in alpha_addresses or address.lower() in early_alerted:
                    continue  # already handled

                forense = safe_float(token.get("forense_score", 0))
                if forense < 7.0:
                    log.info(f"  WHALE SKIP (low forense): {token.get('name') or token.get('address','?')[:8]} "
                             f"forense={forense:.1f} < 7.0")
                    continue

                # v8.6: Double Whale Confirmation — require 2+ unique wallets
                whale_count = len(_whale_wallet_counts.get(address.lower(), set()))
                if whale_count < min_whales:
                    token_name_display = token.get('name') or token.get('address', '?')[:8]
                    log.info(f"  [ALERTA DE SEGUIMIENTO] {token_name_display} — "
                             f"Solo {whale_count} ballena(s) (min={min_whales}). "
                             f"NO se abre trade. Forense={forense:.1f}, "
                             f"Liq=${safe_float(token.get('liquidity_usd',0)):,.0f}")
                    # v8.6: Track as potential missed opportunity
                    _track_missed_opportunity(token, f"single_whale_{whale_count}")
                    continue

                with seen_lock:
                    if not is_token_new(address, seen):
                        continue

                # Ensure price_usd is available (whale tokens get it from DexScreener)
                if safe_float(token.get("price_usd", 0)) <= 0:
                    log.warning(f"  WHALE SKIP (no price): {token.get('name') or token.get('address','?')[:8]}")
                    continue

                # v8.1: Enforce minimum liquidity even for whale fast-track
                # Plush Solana ($22k liq) bypassed this and lost 97.8%
                whale_liq = safe_float(token.get("liquidity_usd", 0))
                whale_min_liq = getattr(config, 'WHALE_MIN_LIQUIDITY', config.SCAN_MIN_LIQUIDITY)
                if whale_liq < whale_min_liq:
                    log.info(f"  WHALE SKIP (low liq): {token.get('name') or token.get('address','?')[:8]} "
                             f"liq=${whale_liq:,.0f} < ${whale_min_liq:,.0f}")
                    continue

                # v8.1: Enforce liq/mcap ratio for whale tokens (anti-fragility)
                whale_mcap = safe_float(token.get("mcap", 0))
                if whale_mcap > 0 and whale_liq > 0:
                    liq_mcap = whale_liq / whale_mcap
                    if liq_mcap < getattr(config, 'AUDIT_MIN_LIQ_MCAP_RATIO', 0.10):
                        log.info(f"  WHALE SKIP (fragile liq): {token.get('name') or token.get('address','?')[:8]} "
                                 f"liq/mcap={liq_mcap:.1%}")
                        continue

                # v8.8: Tiered position sizing
                tier_amount = _compute_tier_amount(token, wallet_signals)
                trade = paper_trader.open_trade(token, amount_sol=tier_amount)
                if trade:
                    with seen_lock:
                        mark_token_seen(address, seen)
                    whale_traded += 1
                    log.info(f"  WHALE FAST-TRACK BUY: {trade['token_name']} "
                             f"@ ${trade['entry_price']:.8f} "
                             f"(forense={forense:.1f}, "
                             f"liq=${token.get('liquidity_usd',0):,.0f}, "
                             f"wallets={whale_count}/{min_whales}, "
                             f"amount={tier_amount} SOL)")
                    whale_msg = (
                        f"<b>WHALE FAST-TRACK</b>\n\n"
                        f"{paper_trader.format_open_message(trade)}\n"
                        f"Whale wallets: {whale_count}\n"
                        f"Forense: {forense:.1f}/10"
                    )
                    send_telegram(whale_msg)
                    with alerts_lock:
                        alerts_sent += 1
                else:
                    log.warning(f"  WHALE TRADE FAILED: {token.get('name') or token.get('address','?')[:8]} "
                                f"(price=${safe_float(token.get('price_usd',0)):.8f}, "
                                f"open_trades={len(paper_trader.get_open_trades())})")

            if whale_traded:
                log.info(f"  Whale fast-track: {whale_traded} paper trades opened")

        # Priority 2: Standard high-score alerts (not already sent as alpha or early)
        # v7.5: Also open paper trades for tokens with score > 7.5
        skip_addresses = alpha_addresses | early_alerted
        # Track whale-traded addresses to avoid duplicates
        whale_traded_addrs = set(
            t.get("address", "").lower() for t in scored
            if t.get("source") == "whale_inject"
            and t.get("address", "").lower() not in alpha_addresses
            and t.get("address", "").lower() not in early_alerted
        )
        standard_traded = 0
        for token in scored:
            address = token.get("address", "")
            if address.lower() in skip_addresses:
                continue
            if address.lower() in whale_traded_addrs:
                continue  # already handled by whale fast-track

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
            log.info(f"  ALERT: {token.get('name') or token.get('address', '?')[:8]} (score={composite:.2f})")

            # v8.8: Paper trade on high-score standard alerts WITH alpha guard
            # Day 1 showed that forense=8.0 + alpha=0.83 + signal_count=0 = guaranteed loss
            min_alpha = getattr(config, 'MIN_ALPHA_SCORE_FOR_TRADE', 1.0)
            token_alpha = safe_float(token.get("alpha_score", 0))
            if paper_trader and composite >= 7.5:
                if token_alpha < min_alpha and token.get("signal_count", 0) < 1:
                    log.info(f"  ALPHA GUARD: {token.get('name','?')} BLOCKED — "
                             f"alpha={token_alpha:.2f} < {min_alpha}, signals={token.get('signal_count',0)}. "
                             f"No blind entries.")
                else:
                    tier_amount = _compute_tier_amount(token, wallet_signals)
                    trade = paper_trader.open_trade(token, amount_sol=tier_amount)
                    if trade:
                        standard_traded += 1
                        log.info(f"  PAPER BUY (standard): {trade['token_name']} "
                                 f"@ ${trade['entry_price']:.8f} "
                                 f"(composite={composite:.2f}, alpha={token_alpha:.2f}, "
                                 f"tier={tier_amount} SOL)")
                        send_telegram(
                            f"<b>PAPER BUY (Score {composite:.1f})</b>\n\n"
                            f"{paper_trader.format_open_message(trade)}"
                        )

        if standard_traded:
            log.info(f"  Standard paper trades: {standard_traded} opened")

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
        # v8.2: Detailed cycle heartbeat
        open_trades = paper_trader.get_open_trades() if paper_trader else []
        pt_summary = paper_trader.get_session_summary() if paper_trader else {}
        log.info(f"[HEARTBEAT] Cycle complete in {elapsed:.1f}s | "
                 f"Scanned: {len(candidates)} | Passed audit: {len(audited)} | "
                 f"Alerts sent: {alerts_sent} | "
                 f"Open trades: {len(open_trades)} | "
                 f"Session PnL: {pt_summary.get('session_pnl_sol', 0):+.4f} SOL")
        return alerts_sent

    except Exception as e:
        log.error(f"Alpha scan error: {e}", exc_info=True)
        try:
            send_telegram(f"<b>Alpha Monitor Error</b>\n\n{str(e)[:200]}")
        except Exception:
            pass
        return 0


def _run_wallet_background(stop_event: threading.Event,
                           signal_accumulator: SignalAccumulator):
    """Background thread: WebSocket (primary) + polling fallback.

    v7.0: Signals are accumulated via SignalAccumulator (30min TTL) instead of
    overwriting. This ensures triple confirmation sees all recent whale activity.
    """
    from alpha.smart_wallet_tracker import SmartWalletTracker, format_wallet_alert

    try:
        tracker = SmartWalletTracker()
        if not tracker.db.load_wallets():
            log.info("Wallet background: no wallets tracked")
            return

        log.info(f"Wallet background thread started "
                 f"({len(tracker.db.load_wallets())} wallets)")

        # Callback: when WS detects a buy, enrich + add to accumulator immediately
        def _on_ws_signal(signal):
            """Immediate processing of WebSocket buy events."""
            if signal:
                signal_accumulator.update([signal])
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
                signal_accumulator.update(signals)
                elapsed = time.monotonic() - t0

                # v8.2: Heartbeat — count how many passed minimum liquidity
                passed_safety = sum(
                    1 for s in signals
                    if safe_float(s.get("liquidity_usd", 0)) >= config.SCAN_MIN_LIQUIDITY
                )
                total_acc = signal_accumulator.count()
                log.info(f"[HEARTBEAT] Wallet scan: {len(signals)} whale signals found | "
                         f"{passed_safety} passed initial safety (liq>=${config.SCAN_MIN_LIQUIDITY:,.0f}) | "
                         f"{total_acc} total in accumulator | {elapsed:.1f}s")
            except Exception as e:
                log.warning(f"Wallet background error: {e}")

            # Adjust interval based on WS state
            if tracker.ws_connected:
                interval = config_alpha.WS_FALLBACK_POLL_INTERVAL  # 600s
            elif tracker.ws_failed:
                interval = config_alpha.POLLING_FAST_INTERVAL  # 90s
            else:
                interval = WALLET_CHECK_INTERVAL  # 120s
            stop_event.wait(timeout=interval)

        tracker.stop_websocket()
    except Exception as e:
        log.error(f"Wallet background thread fatal: {e}")


def _fetch_batch_prices(dex, open_trades: list) -> dict:
    """Batch fetch current prices for open trades via DexScreener.

    Returns {address_lower: price_usd}.
    """
    price_map = {}
    # Group by chain
    by_chain = {}
    for t in open_trades:
        chain = t.get("chain", "")
        addr = t.get("address", "")
        if chain and addr:
            by_chain.setdefault(chain, []).append(addr)

    for chain, addresses in by_chain.items():
        for i in range(0, len(addresses), 30):
            batch = addresses[i:i + 30]
            try:
                pairs = dex.get_tokens_batch(chain, batch)
                for pair in pairs:
                    base_addr = pair.get("baseToken", {}).get("address", "")
                    price = safe_float(pair.get("priceUsd"))
                    if base_addr and price > 0:
                        price_map[base_addr.lower()] = price
            except Exception as e:
                log.warning(f"Batch price fetch error ({chain}): {e}")

    return price_map


def _run_exit_manager(stop_event: threading.Event, paper_trader: PaperTrader):
    """Background thread: check TP1/Moonbag/SL/Emergency every 45s.

    v7.0: Also checks Rugcheck for Danger status on open Solana trades.
    Emergency exit fires immediately if a token is flagged as Danger.
    """
    from api_client import DexScreenerClient, RugcheckClient

    dex = DexScreenerClient()
    rugcheck = RugcheckClient()
    log.info("Exit manager thread started")

    rugcheck_cycle = 0  # Only check Rugcheck every 4th cycle (~3min)
    missed_opp_cycle = 0  # v8.6: Check missed opportunities every ~5min

    while not stop_event.is_set():
        try:
            open_trades = paper_trader.get_open_trades()
            if open_trades:
                price_map = _fetch_batch_prices(dex, open_trades)
                if price_map:
                    paper_trader.update_prices(price_map)
                    closed = paper_trader.check_exits(price_map)
                    for trade in closed:
                        msg = paper_trader.format_exit_message(trade)
                        send_telegram(msg)
                        # v8.0: Diagnostic exit log
                        reason = trade.get('exit_reason', trade.get('type', '?'))
                        pnl = trade.get('pnl_pct', 0)
                        name = trade.get('token_name', '?')
                        opened_at = trade.get('opened_at', '')
                        hold_seconds = 0
                        if opened_at:
                            try:
                                opened_dt = datetime.fromisoformat(opened_at)
                                hold_seconds = (datetime.now(timezone.utc) - opened_dt).total_seconds()
                            except (ValueError, TypeError):
                                pass
                        log.info(f"  [EXIT] Token: {name} | "
                                 f"Cambio: {pnl:+.1f}% | "
                                 f"Motivo: {reason} | "
                                 f"Tiempo en posición: {hold_seconds:.0f} seg")

                # v7.0: Rugcheck emergency exit (Solana only, every ~3min)
                rugcheck_cycle += 1
                if config.PAPER_EMERGENCY_RUGCHECK and rugcheck_cycle % 4 == 0:
                    sol_trades = [t for t in open_trades if t.get("chain") == "solana"]
                    for trade in sol_trades:
                        try:
                            report = rugcheck.get_token_report(trade["address"])
                            if report:
                                risks = report.get("risks", [])
                                has_danger = any(
                                    r.get("level", "").lower() == "danger"
                                    for r in risks
                                )
                                if has_danger:
                                    current = price_map.get(trade["address"].lower(),
                                                           trade.get("current_price", 0))
                                    result = paper_trader.emergency_exit(
                                        trade["address"], current
                                    )
                                    if result:
                                        msg = paper_trader.format_exit_message(result)
                                        send_telegram(msg)
                                        log.warning(f"  EMERGENCY EXIT: {trade['token_name']} "
                                                    f"— Rugcheck Danger!")
                        except Exception as e:
                            log.debug(f"Rugcheck check failed for {trade.get('token_name', '?')}: {e}")
            # v8.6: Check missed opportunities periodically (~5min)
            missed_opp_cycle += 1
            missed_interval = getattr(config, 'MISSED_OPP_CHECK_INTERVAL', 300)
            cycles_per_check = max(1, missed_interval // config.PAPER_EXIT_CHECK_INTERVAL)
            if missed_opp_cycle % cycles_per_check == 0:
                try:
                    _check_missed_opportunities()
                except Exception as e:
                    log.debug(f"Missed opp check error: {e}")

        except Exception as e:
            log.warning(f"Exit manager error: {e}")

        stop_event.wait(timeout=config.PAPER_EXIT_CHECK_INTERVAL)

    log.info("Exit manager thread stopped")


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

    log.info(f"Alpha Monitor v8.8 daemon starting (interval: {interval_min} min)")

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

    # Initialize paper trader (v6.0)
    paper_trader = PaperTrader()
    pt_summary = paper_trader.get_session_summary()
    pt_status = (f"Paper trading: {pt_summary['open_trades']} open, "
                 f"{pt_summary['session_pnl_sol']:+.4f} SOL PnL")
    log.info(pt_status)

    send_telegram(
        "<b>Alpha Monitor v8.8 Started</b>\n\n"
        f"Scan interval: {interval_min} min\n"
        f"Signal accumulation: {SIGNAL_ACCUMULATION_TTL//60} min window\n"
        f"APIs: {api_str}\n"
        f"Min score: {ALERT_MIN_SCORE}/10\n"
        f"Early alerts: forense >= {EARLY_ALERT_MIN_SCORE}\n"
        f"{pt_status}"
    )

    # Start wallet tracker in background thread (v7.0: accumulator replaces dict)
    wallet_stop = threading.Event()
    wallet_signals = SignalAccumulator(ttl_seconds=SIGNAL_ACCUMULATION_TTL)
    wallet_thread = threading.Thread(
        target=_run_wallet_background,
        args=(wallet_stop, wallet_signals),
        daemon=True,
        name="wallet-bg"
    )
    wallet_thread.start()
    log.info("Wallet background thread launched")

    # Start exit manager in background thread (v6.0)
    exit_thread = threading.Thread(
        target=_run_exit_manager,
        args=(wallet_stop, paper_trader),
        daemon=True,
        name="exit-manager"
    )
    exit_thread.start()
    log.info("Exit manager thread launched")

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
            alerts = run_alpha_scan_cycle(wallet_signals_holder=wallet_signals,
                                           paper_trader=paper_trader)
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
    exit_thread.join(timeout=5)
    pt_final = paper_trader.get_session_summary()
    send_telegram(
        "<b>Alpha Monitor v8.8 Stopped</b>\n\n"
        f"Session PnL: {pt_final['session_pnl_sol']:+.4f} SOL\n"
        f"W/L: {pt_final['wins']}/{pt_final['losses']} "
        f"({pt_final['win_rate']:.0f}% WR)\n"
        f"Open trades: {pt_final['open_trades']}"
    )


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
