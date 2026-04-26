# models.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from clock import now_utc

# Mercados que abren más de 2 horas en el futuro se consideran "próximos" (pre-market)
UPCOMING_THRESHOLD_SECONDS = 2 * 3600


@dataclass
class Market:
    id: str
    question: str
    coin: str
    window_minutes: int
    price_yes: float
    price_no: float
    volume: float
    end_time: Optional[datetime]
    has_real_price: bool = False
    last_fetched_ms: int = 0
    updated_at_ms: int = 0
    bid: float = 0.0
    ask: float = 0.0
    last_trade_price: float = 0.0

    @property
    def spread(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return self.ask - self.bid
        return abs(self.price_yes + self.price_no - 1)

    @property
    def time_left_seconds(self) -> float:
        if self.end_time is None:
            return float("inf")
        return (self.end_time - now_utc()).total_seconds()

    @property
    def is_upcoming(self) -> bool:
        """True si el mercado aún no está en su ventana activa (pre-market)."""
        return self.time_left_seconds > UPCOMING_THRESHOLD_SECONDS

    @property
    def time_left(self) -> str:
        secs = self.time_left_seconds
        if secs <= 0:
            return "CLOSED"
        mins = int(secs // 60)
        secs_rem = int(secs % 60)
        if mins >= 60:
            h = mins // 60
            m = mins % 60
            return f"{h}h {m}m"
        elif mins >= 1:
            return f"{mins}m {secs_rem:02d}s"
        else:
            return f"{secs_rem}s"

    @property
    def window_label(self) -> str:
        if self.window_minutes <= 5:
            return "5m"
        elif self.window_minutes <= 15:
            return "15m"
        else:
            return "1h"
