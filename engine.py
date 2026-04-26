# engine.py - Motor de trading demo: gestión de trades, portfolio y resolución

import json
import uuid
import warnings
import requests
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from clock import now_utc
from models import Market
from strategy import TradeSignal, calc_fee, evaluate, should_exit_early, LOSS_STREAK_COOLDOWN, COOLDOWN_CYCLES

warnings.filterwarnings("ignore")

STATE_FILE    = Path(__file__).parent / "demo_state.json"
INITIAL_CAP   = 1000.0
MAX_RUNTIME_H = 24
MAX_TRADES    = 50
RESOLVE_DELAY = 45   # segundos tras cierre antes de consultar resolución
MAX_HISTORY   = 30   # precio history bars por mercado


# ── Modelos de datos ──────────────────────────────────────────────────────────

@dataclass
class Trade:
    id:              str
    timestamp:       str        # ISO UTC
    coin:            str
    window:          str
    question:        str
    series_slug:     str
    direction:       str        # "YES" / "NO"
    entry_price:     float
    shares:          int
    fee_entry:       float
    net_edge_est:    float
    stop_loss:       float
    end_time_iso:    str        # ISO UTC del cierre del mercado
    status:          str        # "open" | "won" | "lost" | "expired"
    exit_price:      float      = 0.0
    fee_exit:        float      = 0.0
    pnl:             float      = 0.0
    resolved_at:     str        = ""
    signal_reason:   str        = ""
    signal_score:    float      = 0.0
    kelly_f:         float      = 0.0
    p_est:           float      = 0.0

    @property
    def end_time(self) -> datetime:
        return datetime.fromisoformat(self.end_time_iso)

    @property
    def seconds_since_close(self) -> float:
        return (now_utc() - self.end_time).total_seconds()


@dataclass
class Portfolio:
    capital:         float           = INITIAL_CAP
    trades:          List[Trade]     = field(default_factory=list)
    decision_log:    List[dict]      = field(default_factory=list)
    started_at:      str             = ""
    last_updated:    str             = ""

    # ── Métricas derivadas ────────────────────────────────────────────────────

    @property
    def open_trades(self) -> List[Trade]:
        return [t for t in self.trades if t.status == "open"]

    @property
    def closed_trades(self) -> List[Trade]:
        return [t for t in self.trades if t.status != "open"]

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fee_entry + t.fee_exit for t in self.trades)

    @property
    def win_rate(self) -> float:
        wins = sum(1 for t in self.closed_trades if t.status == "won")
        n    = len(self.closed_trades)
        return (wins / n * 100) if n else 0.0

    @property
    def best_trade(self) -> float:
        if not self.closed_trades:
            return 0.0
        return max(t.pnl for t in self.closed_trades)

    @property
    def worst_trade(self) -> float:
        if not self.closed_trades:
            return 0.0
        return min(t.pnl for t in self.closed_trades)

    @property
    def avg_edge(self) -> float:
        if not self.closed_trades:
            return 0.0
        return sum(t.net_edge_est for t in self.closed_trades) / len(self.closed_trades)

    @property
    def current_capital(self) -> float:
        # Capital disponible = inicial + PnL realizado - coste posiciones abiertas
        locked = sum(t.entry_price * t.shares for t in self.open_trades)
        return self.capital + self.total_pnl - locked

    @property
    def runtime_hours(self) -> float:
        if not self.started_at:
            return 0.0
        start = datetime.fromisoformat(self.started_at)
        return (now_utc() - start).total_seconds() / 3600

    @property
    def is_demo_finished(self) -> bool:
        return (
            len(self.trades) >= MAX_TRADES
            or self.runtime_hours >= MAX_RUNTIME_H
        )

    def pnl_series(self) -> List[dict]:
        """Serie temporal de PnL acumulado para el gráfico."""
        series = []
        cumulative = 0.0
        for t in sorted(self.closed_trades, key=lambda x: x.timestamp):
            cumulative += t.pnl
            series.append({"time": t.resolved_at or t.timestamp, "pnl": round(cumulative, 4)})
        return series


# ── Demo Engine ───────────────────────────────────────────────────────────────

class DemoEngine:
    """
    Gestiona el ciclo de vida del demo:
    evalúa oportunidades → abre trades → resuelve posiciones → actualiza métricas.
    """

    def __init__(self):
        self.portfolio        = self._load_state()
        self.price_history:   dict  = {}  # key="COIN_TF" → List[float]
        self.cooldown_cycles: int   = 0   # cycles remaining in loss cooldown
        self._prev_loss_count: int  = 0   # track consecutive losses

    # ── Estado persistente ────────────────────────────────────────────────────

    def _load_state(self) -> Portfolio:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                p = Portfolio(
                    capital=data.get("capital", INITIAL_CAP),
                    started_at=data.get("started_at", ""),
                    last_updated=data.get("last_updated", ""),
                )
                p.trades = [Trade(**t) for t in data.get("trades", [])]
                p.decision_log = data.get("decision_log", [])
                return p
            except Exception:
                pass
        p = Portfolio(started_at=now_utc().isoformat())
        self._save(p)
        return p

    def _save(self, p: Portfolio):
        p.last_updated = now_utc().isoformat()
        data = {
            "capital":      p.capital,
            "started_at":   p.started_at,
            "last_updated": p.last_updated,
            "trades":       [asdict(t) for t in p.trades],
            "decision_log": p.decision_log[-500:],  # Últimas 500 entradas
        }
        STATE_FILE.write_text(json.dumps(data, indent=2))

    # ── Ciclo principal ───────────────────────────────────────────────────────

    def run_cycle(self, markets: List[Market]):
        """
        Llamar en cada refresh (cada 5s):
        1. Actualiza price history.
        2. Resuelve trades abiertos cuyo mercado ya cerró.
        3. Comprueba salidas anticipadas.
        4. Evalúa nuevas oportunidades.
        5. Gestiona cooldown por rachas de pérdidas.
        6. Guarda estado.
        """
        if self.portfolio.is_demo_finished:
            return

        self._update_price_history(markets)
        self._resolve_open_trades()
        self._check_early_exits(markets)
        self._update_cooldown()
        self._evaluate_opportunities(markets)
        self._save(self.portfolio)

    def _update_price_history(self, markets: List[Market]):
        for m in markets:
            key = f"{m.coin}_{m.window_label}"
            hist = self.price_history.get(key, [])
            hist.append(round(m.price_yes, 4))
            self.price_history[key] = hist[-MAX_HISTORY:]

    def _update_cooldown(self):
        """Activate cooldown if loss streak exceeds threshold."""
        closed = self.portfolio.closed_trades
        if not closed:
            return
        # Count consecutive losses from the end
        streak = 0
        for t in reversed(closed):
            if t.status == "lost":
                streak += 1
            else:
                break
        if streak >= LOSS_STREAK_COOLDOWN and self.cooldown_cycles == 0:
            self.cooldown_cycles = COOLDOWN_CYCLES
            self._log("ENGINE", "COOLDOWN", f"Racha de {streak} pérdidas → pausa {COOLDOWN_CYCLES} ciclos")
        elif self.cooldown_cycles > 0:
            self.cooldown_cycles -= 1

    # ── Resolución de trades ──────────────────────────────────────────────────

    def _resolve_open_trades(self):
        for trade in list(self.portfolio.open_trades):
            if trade.seconds_since_close < RESOLVE_DELAY:
                continue  # Esperar al menos RESOLVE_DELAY segundos
            resolution = self._fetch_resolution(trade)
            if resolution is not None:
                self._close_trade(trade, resolution)

    def _fetch_resolution(self, trade: Trade) -> Optional[float]:
        """
        Consulta la API para saber si el mercado resolvió YES (≈1.0) o NO (≈0.0).
        Devuelve el precio de resolución o None si aún no está disponible.
        """
        try:
            url = "https://gamma-api.polymarket.com/events"
            params = {
                "series_slug": trade.series_slug,
                "limit": 20,
                "order": "endDate",
                "ascending": "false",
            }
            r = requests.get(url, params=params, timeout=8)
            events = r.json()
            trade_end = trade.end_time

            for event in events:
                try:
                    ev_end = datetime.fromisoformat(
                        event.get("endDate", "").replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except Exception:
                    continue

                if abs((ev_end - trade_end).total_seconds()) > 120:
                    continue

                for m in event.get("markets", []):
                    # outcomePrices es la fuente canónica de resolución
                    op_raw = m.get("outcomePrices", [])
                    if isinstance(op_raw, str):
                        try:
                            op_raw = json.loads(op_raw)
                        except Exception:
                            op_raw = []
                    if len(op_raw) >= 2:
                        op_yes = float(op_raw[0])
                        if op_yes >= 0.99:
                            return 1.0  # YES ganó
                        if op_yes <= 0.01:
                            return 0.0  # NO ganó

                    # Fallback: lastTradePrice exactamente en 0 ó 1
                    last = m.get("lastTradePrice")
                    if last is not None and float(last) in (0.0, 1.0):
                        return float(last)

                    # Fallback: bestAsk del token YES
                    # ask bajo → YES barato → YES resolvió a 0 (NO ganó)
                    # ask alto → YES caro  → YES resolvió a 1 (YES ganó)
                    best_ask = m.get("bestAsk") or m.get("ask")
                    if best_ask is not None:
                        a = float(best_ask)
                        if a <= 0.02:
                            return 0.0  # YES token vale casi 0 → NO ganó
                        if a >= 0.98:
                            return 1.0  # YES token vale casi 1 → YES ganó
        except Exception:
            pass
        return None

    def _close_trade(self, trade: Trade, resolution_price: float):
        """Cierra un trade con el precio de resolución real."""
        # En resolución: YES → 1.0, NO → 0.0 (para el token YES)
        yes_resolved = resolution_price >= 0.5

        if trade.direction == "YES":
            exit_price = 1.0 if yes_resolved else 0.0
        else:
            # Compramos NO → ganamos si NO gana (YES = 0)
            exit_price = 1.0 if not yes_resolved else 0.0

        fee_exit = calc_fee(trade.shares, exit_price)
        gross    = (exit_price - trade.entry_price) * trade.shares
        pnl      = gross - trade.fee_entry - fee_exit

        trade.exit_price  = exit_price
        trade.fee_exit    = fee_exit
        trade.pnl         = round(pnl, 4)
        trade.status      = "won" if pnl > 0 else "lost"
        trade.resolved_at = now_utc().isoformat()

        self._log(
            market=trade.question,
            action="RESOLVED",
            detail=(
                f"{trade.direction} exit={exit_price:.2f} | "
                f"PnL=${pnl:+.2f} | status={trade.status}"
            ),
        )

    # ── Early exit check ──────────────────────────────────────────────────────

    def _check_early_exits(self, markets: List[Market]):
        """For each open trade, check stop-loss / take-profit / time-based exit."""
        mkt_by_q = {m.question: m for m in markets}

        for trade in list(self.portfolio.open_trades):
            m = mkt_by_q.get(trade.question)
            if m is None:
                continue

            cur_price = m.price_yes if trade.direction == "YES" else m.price_no
            secs_left = m.time_left_seconds

            exit_now, exit_reason = should_exit_early(
                trade.entry_price, cur_price, secs_left, trade.window
            )

            if exit_now:
                self._close_trade_early(trade, cur_price, exit_reason)

    def _close_trade_early(self, trade, exit_price: float, reason: str):
        """Close a trade early at current market price (simulated market order)."""
        fee_exit = calc_fee(trade.shares, exit_price)
        gross    = (exit_price - trade.entry_price) * trade.shares
        pnl      = gross - trade.fee_entry - fee_exit

        trade.exit_price  = exit_price
        trade.fee_exit    = fee_exit
        trade.pnl         = round(pnl, 4)
        trade.status      = "won" if pnl > 0 else "lost"
        trade.resolved_at = now_utc().isoformat()

        self._log(
            market=trade.question,
            action="EXIT_EARLY",
            detail=f"{reason} | exit={exit_price:.3f} | PnL=${pnl:+.2f} | {trade.status}",
        )

    # ── Evaluación de nuevas oportunidades ────────────────────────────────────

    def _evaluate_opportunities(self, markets: List[Market]):
        already_trading = {t.question for t in self.portfolio.open_trades}

        # Build lookup: (coin, tf) → market
        mkt_map: dict = {(m.coin, m.window_label): m for m in markets}

        for market in markets:
            if market.question in already_trading:
                continue

            coin = market.coin
            tf   = market.window_label

            # Retrieve related timeframe markets for multi-TF signal
            mkt_15m = mkt_map.get((coin, "15m")) if tf != "15m" else None
            mkt_1h  = mkt_map.get((coin, "1h"))  if tf != "1h"  else None

            # For 15m markets, use 1h as the only higher-TF reference
            if tf == "15m":
                mkt_15m = None

            # For 1h markets, no higher TF available; 15m as lower confirmation
            if tf == "1h":
                mkt_15m = mkt_map.get((coin, "15m"))
                mkt_1h  = None

            key      = f"{coin}_{tf}"
            price_hist = self.price_history.get(key, [])

            signal = evaluate(
                market=market,
                mkt_15m=mkt_15m,
                mkt_1h=mkt_1h,
                price_hist=price_hist,
                open_positions=len(self.portfolio.open_trades),
                capital=self.portfolio.current_capital,
                cooldown_remaining=self.cooldown_cycles,
            )

            self._log(
                market=f"{coin} {tf}",
                action="EXECUTE" if signal.execute else "SKIP",
                detail=signal.reason,
            )

            if signal.execute:
                self._open_trade(market, signal)

    def _open_trade(self, market: Market, signal: TradeSignal):
        series_slug = _coin_window_to_slug(market.coin, market.window_label)

        trade = Trade(
            id=str(uuid.uuid4())[:8],
            timestamp=now_utc().isoformat(),
            coin=market.coin,
            window=market.window_label,
            question=market.question,
            series_slug=series_slug,
            direction=signal.direction,
            entry_price=signal.entry_price,
            shares=signal.shares,
            fee_entry=signal.fee_entry,
            net_edge_est=signal.net_edge,
            stop_loss=signal.stop_loss_price,
            end_time_iso=market.end_time.isoformat() if market.end_time else "",
            status="open",
            signal_reason=signal.reason,
            signal_score=signal.signal_score,
            kelly_f=signal.kelly_f,
            p_est=signal.p_est,
        )
        self.portfolio.trades.append(trade)
        self._log(
            market=market.question,
            action="OPEN",
            detail=(
                f"{signal.direction} @ {signal.entry_price:.3f} | "
                f"{signal.shares} shares | cost=${signal.entry_price * signal.shares:.2f} | "
                f"edge neto est.=${signal.net_edge:.2f}"
            ),
        )

    # ── Log de decisiones ─────────────────────────────────────────────────────

    def _log(self, market: str, action: str, detail: str):
        self.portfolio.decision_log.append({
            "time":   now_utc().strftime("%H:%M:%S"),
            "market": market,
            "action": action,
            "detail": detail,
        })

    def reset(self):
        """Reinicia el demo desde cero."""
        STATE_FILE.unlink(missing_ok=True)
        self.portfolio        = Portfolio(started_at=now_utc().isoformat())
        self.price_history    = {}
        self.cooldown_cycles  = 0
        self._save(self.portfolio)


# ── Helpers ───────────────────────────────────────────────────────────────────

_SLUG_MAP = {
    ("BTC",  "5m"):  "btc-up-or-down-5m",
    ("BTC",  "15m"): "btc-up-or-down-15m",
    ("BTC",  "1h"):  "btc-up-or-down-hourly",
    ("ETH",  "5m"):  "eth-up-or-down-5m",
    ("ETH",  "15m"): "eth-up-or-down-15m",
    ("ETH",  "1h"):  "eth-up-or-down-hourly",
    ("SOL",  "5m"):  "sol-up-or-down-5m",
    ("SOL",  "15m"): "sol-up-or-down-15m",
    ("SOL",  "1h"):  "solana-up-or-down-hourly",
    ("XRP",  "5m"):  "xrp-up-or-down-5m",
    ("XRP",  "15m"): "xrp-up-or-down-15m",
    ("XRP",  "1h"):  "xrp-up-or-down-hourly",
    ("BNB",  "5m"):  "bnb-up-or-down-5m",
    ("BNB",  "15m"): "bnb-up-or-down-15m",
    ("BNB",  "1h"):  "bnb-up-or-down-hourly",
    ("DOGE", "5m"):  "doge-up-or-down-5m",
    ("DOGE", "15m"): "doge-up-or-down-15m",
    ("DOGE", "1h"):  "doge-up-or-down-hourly",
    ("HYPE", "5m"):  "hype-up-or-down-5m",
    ("HYPE", "15m"): "hype-up-or-down-15m",
    ("HYPE", "1h"):  "hype-up-or-down-hourly",
}

def _coin_window_to_slug(coin: str, window: str) -> str:
    return _SLUG_MAP.get((coin, window), f"{coin.lower()}-up-or-down-{window}")
