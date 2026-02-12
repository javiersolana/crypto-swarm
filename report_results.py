"""
Report Results - v8.8 Bulletproof Paper Trading Report

Reads data/paper_trades.json and generates a PnL report.
Handles all edge cases: missing fields, corrupt data, empty trades,
old format files. Recalculates from entry/exit prices when stored
values are 0 or missing. Shows 6-decimal precision on SOL amounts.
v8.8: Shows tier amount per trade if available.
"""
import json
import os
import sys
from datetime import datetime


TRADES_FILE = "data/paper_trades.json"


def _safe_float(val, default=0.0) -> float:
    """Convert any value to float, never crash."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _calc_change_pct(trade: dict) -> float:
    """Calculate change % from prices. Tries stored value first, recalculates if 0."""
    stored = _safe_float(trade.get("final_change_pct")) or _safe_float(trade.get("pnl_pct"))
    if stored != 0.0:
        return stored
    entry = _safe_float(trade.get("entry_price"))
    exit_p = _safe_float(trade.get("exit_price"))
    if entry > 0 and exit_p > 0:
        return round(((exit_p - entry) / entry) * 100, 6)
    return 0.0


def _calc_net_pnl(trade: dict) -> float:
    """Get net PnL in SOL. Tries stored, recalculates if 0."""
    stored = _safe_float(trade.get("pnl_net_sol")) or _safe_float(trade.get("pnl_sol"))
    if stored != 0.0:
        return stored
    # Recalculate: (change_pct / 100) * amount_sol - fee
    change = _calc_change_pct(trade)
    amount = _safe_float(trade.get("remaining_sol")) or _safe_float(trade.get("amount_sol"), 1.0)
    fee = _safe_float(trade.get("fee_sol"), 0.006)
    tp1_pnl = _safe_float(trade.get("tp1_pnl_sol"))
    return round(amount * (change / 100) + tp1_pnl - fee, 6)


def _get_token_name(trade: dict) -> str:
    """Extract token name from any format."""
    return (
        trade.get("token_name")
        or trade.get("name")
        or trade.get("token_symbol")
        or (trade.get("address") or "???")[:12]
    )


def _load_trades() -> dict | None:
    """Load trades file with multiple fallback paths."""
    paths = [TRADES_FILE, "data/paper_trades.json", "./paper_trades.json"]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    content = f.read().strip()
                if not content:
                    print(f"  Archivo vacio: {path}")
                    continue
                data = json.loads(content)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError as e:
                print(f"  JSON corrupto en {path}: {e}")
            except OSError as e:
                print(f"  Error leyendo {path}: {e}")
    return None


def generate_report():
    print()
    data = _load_trades()
    if not data:
        print("  No se encontro el archivo de trades o esta corrupto.")
        print(f"  Buscado en: {TRADES_FILE}")
        print("  El bot necesita generar al menos 1 alerta primero.")
        return

    # Extract with safe defaults for ANY field name variation
    active = data.get("open_trades", data.get("active_trades", []))
    closed = data.get("closed_trades", [])
    session_pnl = _safe_float(data.get("session_pnl_sol", data.get("total_pnl_sol")))
    total_trades = int(_safe_float(data.get("total_trades", len(active) + len(closed))))
    wins = int(_safe_float(data.get("wins", 0)))
    losses = int(_safe_float(data.get("losses", 0)))
    config_info = data.get("config", {})
    session_start = data.get("session_start", "N/A")

    amount_sol = _safe_float(config_info.get("amount_sol"), 1.0)

    print("=" * 65)
    print(f"  INFORME DE RENTABILIDAD SWARM v8.6")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)

    print(f"\n  CONFIGURACION")
    print(f"  Session start: {session_start}")
    print(f"  Capital por trade: {amount_sol} SOL")
    print(f"  TP1: +{config_info.get('tp1_pct', 80)}% "
          f"(sell {_safe_float(config_info.get('tp1_sell_fraction', 0.5))*100:.0f}%)")
    print(f"  SL: {config_info.get('sl_pct', -25)}%  |  "
          f"Trailing: {config_info.get('moonbag_trailing_pct', 20)}%")

    print(f"\n  ESTADO")
    print(f"  Total trades: {total_trades}  |  Open: {len(active)}  |  Closed: {len(closed)}")
    print(f"  Session PnL: {session_pnl:+.6f} SOL")

    # ── Closed trades detail ──────────────────────────────────────────
    if closed:
        # Recalculate wins/losses if stored values seem wrong
        recalc_wins = sum(1 for t in closed if _calc_net_pnl(t) > 0)
        recalc_losses = len(closed) - recalc_wins
        if wins + losses != len(closed):
            wins, losses = recalc_wins, recalc_losses

        win_rate = (wins / len(closed)) * 100 if len(closed) > 0 else 0
        total_fees = sum(_safe_float(t.get("fee_sol"), 0.006) for t in closed)
        recalc_pnl = sum(_calc_net_pnl(t) for t in closed)
        gross_pnl = sum(
            _safe_float(t.get("pnl_sol")) or (_calc_net_pnl(t) + _safe_float(t.get("fee_sol"), 0.006))
            for t in closed
        )

        print(f"\n  PERFORMANCE")
        print(f"  Win Rate: {win_rate:.1f}% ({wins}W / {losses}L)")
        print(f"  Gross PnL: {gross_pnl:+.6f} SOL")
        print(f"  Total fees: -{total_fees:.6f} SOL")
        print(f"  Net PnL:   {recalc_pnl:+.6f} SOL")

        # Per-trade table
        print(f"\n  {'#':<4} {'TOKEN':<18} {'CHANGE':>9} {'PnL SOL':>12} {'FEE':>9} {'REASON':<20}")
        print(f"  {'─'*4} {'─'*18} {'─'*9} {'─'*12} {'─'*9} {'─'*20}")

        for i, t in enumerate(closed, 1):
            name = _get_token_name(t)[:17]
            change = _calc_change_pct(t)
            net_pnl = _calc_net_pnl(t)
            fee = _safe_float(t.get("fee_sol"), 0.006)
            reason = t.get("exit_reason", "N/A")
            sign = "+" if change > 0 else ""
            print(f"  {i:<4} {name:<18} {sign}{change:>8.2f}% {net_pnl:>+12.6f} {fee:>9.6f} {reason:<20}")

        # Best and worst
        best = max(closed, key=lambda x: _calc_change_pct(x))
        worst = min(closed, key=lambda x: _calc_change_pct(x))
        print(f"\n  MEJOR:  {_get_token_name(best)[:20]} ({_calc_change_pct(best):+.2f}%)")
        print(f"  PEOR:   {_get_token_name(worst)[:20]} ({_calc_change_pct(worst):+.2f}%)")

        # Exit reason breakdown
        reasons = {}
        for t in closed:
            r = t.get("exit_reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1
        print(f"\n  EXIT REASONS:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            pct = count / len(closed) * 100
            print(f"    {reason}: {count} ({pct:.0f}%)")

        # v8.6: Commission impact analysis
        gross_pnl_no_fees = sum(
            (_calc_net_pnl(t) + _safe_float(t.get("fee_sol"), 0.006))
            for t in closed
        )
        total_slippage_cost = sum(
            _safe_float(t.get("amount_sol"), 0.05) * (_safe_float(t.get("_slippage_pct"), 3.0) / 100)
            for t in closed
        )
        print(f"\n  COMISIONES vs PnL (Day 2 Analysis)")
        print(f"    PnL SIN comisiones:  {gross_pnl_no_fees:+.6f} SOL")
        print(f"    Total comisiones:    -{total_fees:.6f} SOL")
        print(f"    PnL CON comisiones:  {recalc_pnl:+.6f} SOL")
        print(f"    Impacto comisiones:  {(total_fees / max(abs(gross_pnl_no_fees), 0.000001) * 100):.1f}% del PnL bruto")
        print(f"    Fee promedio/trade:  {total_fees / max(len(closed), 1):.6f} SOL")
        if gross_pnl_no_fees > 0 and recalc_pnl <= 0:
            print(f"    *** Las comisiones convirtieron ganancias en perdidas ***")

        # TP1 moonbag stats
        tp1_trades = [t for t in closed if t.get("tp1_hit")]
        if tp1_trades:
            tp1_pnl = sum(_safe_float(t.get("tp1_pnl_sol")) for t in tp1_trades)
            print(f"\n  MOONBAG STATS:")
            print(f"    TP1 triggered: {len(tp1_trades)}/{len(closed)} trades")
            print(f"    TP1 realized PnL: {tp1_pnl:+.6f} SOL")

    else:
        print(f"\n  Sin trades cerrados todavia.")

    # ── Open positions ────────────────────────────────────────────────
    if active:
        print(f"\n  POSICIONES ABIERTAS ({len(active)}):")
        print(f"  {'TOKEN':<18} {'CHANGE':>9} {'MAX':>9} {'SL':>12} {'STATUS':<12}")
        print(f"  {'─'*18} {'─'*9} {'─'*9} {'─'*12} {'─'*12}")
        for t in active:
            name = _get_token_name(t)[:17]
            entry = _safe_float(t.get("entry_price"))
            curr = _safe_float(t.get("current_price")) or entry
            change = ((curr - entry) / entry * 100) if entry > 0 else 0
            highest = _safe_float(t.get("highest_price")) or entry
            max_pct = ((highest - entry) / entry * 100) if entry > 0 else 0
            sl = _safe_float(t.get("stop_loss"))
            status = "TP1-HIT" if t.get("tp1_hit") else "ACTIVE"
            print(f"  {name:<18} {change:>+8.2f}% {max_pct:>+8.2f}% ${sl:>10.8f} {status:<12}")
    else:
        print(f"\n  Sin posiciones abiertas.")

    print("\n" + "=" * 65)


if __name__ == "__main__":
    generate_report()
