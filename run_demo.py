# run_demo.py — ejecuta el motor de estrategia en modo consola (sin UI)

import time
from clock import calibrate, now_utc
from data_feed import fetch_markets
from engine import DemoEngine

calibrate()
engine = DemoEngine()

print(f"Demo iniciado — capital: ${engine.portfolio.current_capital:.2f}")
print(f"Estrategia: precio [0.88–0.92] en últimos 15s\n")

cycle = 0
while not engine.portfolio.is_demo_finished:
    markets = fetch_markets()
    engine.run_cycle(markets)
    p = engine.portfolio
    cycle += 1
    print(f"[{now_utc().strftime('%H:%M:%S')}] ciclo {cycle:4d} | "
          f"capital ${p.current_capital:.2f} | "
          f"trades {len(p.closed_trades):3d} | "
          f"PnL ${p.total_pnl:+.2f}")
    time.sleep(5)

print("\nDemo finalizado.")
p = engine.portfolio
print(f"Trades: {len(p.closed_trades)} | Wins: {sum(1 for t in p.closed_trades if t.status=='won')} | PnL: ${p.total_pnl:+.2f}")
