# run_demo.py — ejecuta el motor de estrategia en modo consola (sin UI)

import time
from clock import calibrate, now_utc
from data_feed import fetch_markets
from engine import DemoEngine
from strategy import ENTRY_WINDOW, SIGNAL_THRESHOLD, KELLY_FRACTION, MAX_RISK_PCT, MAX_POSITIONS

calibrate()
engine = DemoEngine()
p = engine.portfolio

print("=" * 60)
print("polybot — Demo Mode")
print("=" * 60)
print(f"Capital inicial : ${p.capital:.2f}")
print(f"Capital actual  : ${p.current_capital:.2f}")
print(f"Estrategia      : multi-timeframe + Kelly fraccionario")
print(f"  Ventanas      : 5m={ENTRY_WINDOW['5m']}s  15m={ENTRY_WINDOW['15m']}s  1h={ENTRY_WINDOW['1h']}s")
print(f"  Umbral señal  : |score| >= {SIGNAL_THRESHOLD}")
print(f"  Kelly         : {KELLY_FRACTION} × f*  cap {MAX_RISK_PCT*100:.0f}% bankroll")
print(f"  Max posiciones: {MAX_POSITIONS}")
print("=" * 60)
print()

cycle = 0
try:
    while not engine.portfolio.is_demo_finished:
        markets = fetch_markets()
        engine.run_cycle(markets)
        p = engine.portfolio
        cycle += 1

        open_pos = len(p.open_trades)
        closed   = len(p.closed_trades)
        wins     = sum(1 for t in p.closed_trades if t.status == "won")
        wr       = f"{wins/closed*100:.0f}%" if closed else "—"
        cd       = f" [COOLDOWN:{engine.cooldown_cycles}]" if engine.cooldown_cycles > 0 else ""

        print(
            f"[{now_utc().strftime('%H:%M:%S')}] "
            f"ciclo {cycle:4d} | "
            f"capital ${p.current_capital:8.2f} | "
            f"open {open_pos} | "
            f"closed {closed:3d} ({wr} WR) | "
            f"PnL ${p.total_pnl:+7.2f}"
            f"{cd}"
        )

        # Print any new executions this cycle
        for entry in p.decision_log[-21:]:
            if entry["action"] in ("EXECUTE", "EXIT_EARLY", "COOLDOWN"):
                print(f"  >>> {entry['action']}: {entry['market']} — {entry['detail'][:70]}")

        time.sleep(5)

except KeyboardInterrupt:
    print("\n[Interrumpido por usuario]")

print()
print("=" * 60)
print("Demo finalizado")
p = engine.portfolio
closed = p.closed_trades
wins   = sum(1 for t in closed if t.status == "won")
losses = sum(1 for t in closed if t.status == "lost")
print(f"Trades totales : {len(closed)}")
print(f"Wins / Losses  : {wins} / {losses}  ({wins/len(closed)*100:.1f}% WR)" if closed else "Sin trades")
print(f"PnL total      : ${p.total_pnl:+.2f}")
print(f"Capital final  : ${p.current_capital:.2f}")
print(f"ROI            : {(p.total_pnl / p.capital * 100):+.2f}%")
print("=" * 60)
