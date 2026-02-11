import json
import os
from datetime import datetime

# Archivo donde el bot v6.0 guarda los trades
TRADES_FILE = "data/paper_trades.json"

def generate_report():
    if not os.path.exists(TRADES_FILE):
        print("❌ No se encontró el archivo de trades. ¿Ha generado el bot alguna alerta?")
        return

    with open(TRADES_FILE, 'r') as f:
        data = json.load(f)

    active = data.get("active_trades", [])
    closed = data.get("closed_trades", [])
    total_pnl = data.get("total_pnl_sol", 0.0)

    print("=" * 50)
    print(f"📊 INFORME DE BACKTESTING - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    print(f"\n📈 RESUMEN GENERAL:")
    print(f"   - Trades Totales Cerrados: {len(closed)}")
    print(f"   - Trades Actualmente Abiertos: {len(active)}")
    print(f"   - PnL Total Acumulado: {total_pnl:.4f} SOL")

    if closed:
        wins = [t for t in closed if t.get('pnl_sol', 0) > 0]
        losses = [t for t in closed if t.get('pnl_sol', 0) <= 0]
        win_rate = (len(wins) / len(closed)) * 100
        
        print(f"\n🎯 PERFORMANCE:")
        print(f"   - Win Rate: {win_rate:.2f}%")
        print(f"   - Ganadores: {len(wins)} ✅")
        print(f"   - Perdedores: {len(losses)} ❌")
        
        best_trade = max(closed, key=lambda x: x.get('final_change_pct', 0))
        print(f"\n🏆 MEJOR TRADE: {best_trade['name']} (+{best_trade['final_change_pct']:.2f}%)")

    if active:
        print(f"\n👀 POSICIONES ABIERTAS:")
        for t in active:
            # Calculamos cambio actual si tenemos el precio actualizado
            entry = t.get('entry_price', 1)
            curr = t.get('current_price', entry)
            change = ((curr - entry) / entry) * 100
            print(f"   - {t['name']}: {change:.2f}% (Máx. alcanzado: {t.get('max_reached_pct', 0):.2f}%)")

    print("\n" + "=" * 50)

if __name__ == "__main__":
    generate_report()
