"""
Report Results - v8.1 Robust Paper Trading Report

Reads data/paper_trades.json and generates a PnL report.
If pnl_pct/final_change_pct is 0 or missing, recalculates from entry/exit prices.
Shows net PnL after fees.
"""
import json
import os
from datetime import datetime

TRADES_FILE = "data/paper_trades.json"


def _calc_change_pct(trade: dict) -> float:
    """Calculate change % from prices if the stored value is 0 or missing."""
    stored = trade.get("final_change_pct") or trade.get("pnl_pct") or 0.0
    if stored != 0:
        return float(stored)
    # Fallback: calculate from entry/exit prices
    entry = trade.get("entry_price", 0)
    exit_p = trade.get("exit_price", 0)
    if entry and entry > 0 and exit_p:
        return round(((exit_p - entry) / entry) * 100, 2)
    return 0.0


def _calc_net_pnl(trade: dict) -> float:
    """Get net PnL in SOL, accounting for fees."""
    stored = trade.get("pnl_net_sol") or trade.get("pnl_sol") or 0.0
    return float(stored)


def generate_report():
    if not os.path.exists(TRADES_FILE):
        print("No se encontro el archivo de trades. Ha generado el bot alguna alerta?")
        return

    try:
        with open(TRADES_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error leyendo el JSON: {e}")
        return

    # v8.1: Use correct field names from paper_trader.py
    active = data.get("open_trades", [])
    closed = data.get("closed_trades", [])
    session_pnl = data.get("session_pnl_sol", 0.0)
    total_trades = data.get("total_trades", 0)
    wins = data.get("wins", 0)
    losses = data.get("losses", 0)
    config_info = data.get("config", {})

    amount_sol = config_info.get("amount_sol", 1.0)

    print("=" * 65)
    print(f"  INFORME DE RENTABILIDAD SWARM v8.1 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 65)

    print(f"\n  RESUMEN DE CARTERA")
    print(f"  Capital por trade: {amount_sol} SOL")
    print(f"  TP1: +{config_info.get('tp1_pct', 80)}% (sell {config_info.get('tp1_sell_fraction', 0.5)*100:.0f}%)")
    print(f"  SL: {config_info.get('sl_pct', -25)}%  |  Trailing: {config_info.get('moonbag_trailing_pct', 20)}%")
    print(f"  Total trades: {total_trades}  |  Open: {len(active)}  |  Closed: {len(closed)}")
    print(f"  Session PnL: {session_pnl:+.4f} SOL")

    if closed:
        win_rate = (wins / len(closed)) * 100 if len(closed) > 0 else 0
        total_fees = sum(float(t.get("fee_sol", 0.006)) for t in closed)

        # Recalculate net PnL from individual trades for accuracy
        recalc_pnl = sum(_calc_net_pnl(t) for t in closed)

        print(f"\n  PERFORMANCE")
        print(f"  Win Rate: {win_rate:.1f}% ({wins}W / {losses}L)")
        print(f"  Total fees paid: {total_fees:.4f} SOL")
        print(f"  Net PnL (recalc): {recalc_pnl:+.4f} SOL")

        print(f"\n  {'TOKEN':<20} {'CHANGE':>8} {'PnL SOL':>10} {'FEE':>7} {'REASON':<20}")
        print(f"  {'-'*20} {'-'*8} {'-'*10} {'-'*7} {'-'*20}")

        for t in closed:
            name = (t.get("token_name") or t.get("address", "???")[:12])[:19]
            change = _calc_change_pct(t)
            net_pnl = _calc_net_pnl(t)
            fee = float(t.get("fee_sol", 0.006))
            reason = t.get("exit_reason", "N/A")
            icon = "+" if change > 0 else ""
            print(f"  {name:<20} {icon}{change:>7.1f}% {net_pnl:>+10.4f} {fee:>7.4f} {reason:<20}")

        # Best and worst trades
        best = max(closed, key=lambda x: _calc_change_pct(x))
        worst = min(closed, key=lambda x: _calc_change_pct(x))
        best_name = (best.get("token_name") or "?")[:20]
        worst_name = (worst.get("token_name") or "?")[:20]
        print(f"\n  MEJOR:  {best_name} ({_calc_change_pct(best):+.1f}%)")
        print(f"  PEOR:   {worst_name} ({_calc_change_pct(worst):+.1f}%)")

        # Exit reason breakdown
        reasons = {}
        for t in closed:
            r = t.get("exit_reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1
        print(f"\n  EXIT REASONS:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {count}")

    if active:
        print(f"\n  POSICIONES ABIERTAS ({len(active)}):")
        for t in active:
            name = (t.get("token_name") or t.get("address", "?")[:12])[:19]
            entry = t.get("entry_price", 0)
            curr = t.get("current_price", entry)
            change = ((curr - entry) / entry * 100) if entry > 0 else 0
            highest = t.get("highest_price", entry)
            max_pct = ((highest - entry) / entry * 100) if entry > 0 else 0
            tp1 = "TP1-HIT" if t.get("tp1_hit") else ""
            print(f"    {name:<20} {change:>+7.1f}% (max: {max_pct:+.1f}%) {tp1}")

    print("\n" + "=" * 65)


if __name__ == "__main__":
    generate_report()
