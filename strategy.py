# strategy.py — Estrategia: apostar al favorito fuerte (<15s, precio 0.88–0.92)

from dataclasses import dataclass
from models import Market

# ── Parámetros ────────────────────────────────────────────────────────────────
FEE_RATE          = 0.072   # Taker fee Polymarket crypto
ENTRY_WINDOW_SECS = 15      # Solo entrar si quedan ≤ 15s para el cierre
BET_SHARES        = 10      # Tamaño fijo de posición (shares)
BET_RANGE_LO      = 0.88    # Rango objetivo de precio: mínimo
BET_RANGE_HI      = 0.92    # Rango objetivo de precio: máximo (≈ 0.90 ± 0.02)
MAX_POSITIONS     = 3       # Máximo de posiciones abiertas simultáneas
STOP_LOSS_PCT     = 0.02    # Stop loss de referencia (2%)


# ── Señal de trade ────────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    execute:         bool
    direction:       str    # "YES" | "NO"
    entry_price:     float
    shares:          int
    fee_entry:       float
    fee_exit_est:    float
    gross_edge:      float
    net_edge:        float
    stop_loss_price: float
    reason:          str


# ── Fees ──────────────────────────────────────────────────────────────────────

def calc_fee(shares: float, price: float) -> float:
    """Fee = FEE_RATE × shares × p × (1−p). En resolución (p=1) la fee ≈ 0."""
    return FEE_RATE * shares * price * (1.0 - price)


def round_trip_fee_per_share(entry_price: float) -> float:
    return calc_fee(1, entry_price)


# ── Evaluación ────────────────────────────────────────────────────────────────

def evaluate(market: Market, open_positions: int, capital: float) -> TradeSignal:
    """
    Condiciones de entrada:
    1. Quedan ≤ ENTRY_WINDOW_SECS (15s) para el cierre
    2. price_yes ∈ [0.88, 0.92]  → apostar YES
       price_no  ∈ [0.88, 0.92]  → apostar NO
    3. Tamaño fijo: BET_SHARES (10 shares)
    4. No más de MAX_POSITIONS abiertas
    """
    _no = TradeSignal(False, "", 0, 0, 0, 0, 0, 0, 0, "")

    # ── Filtros previos ──
    if market.is_upcoming:
        return _reject(_no, "Pre-market: ventana no abierta aún")

    secs = market.time_left_seconds

    if secs > ENTRY_WINDOW_SECS:
        return _reject(_no, f"Tiempo restante {market.time_left} > {ENTRY_WINDOW_SECS}s")

    if secs <= 0:
        return _reject(_no, "Mercado ya cerrado")

    if not market.has_real_price:
        return _reject(_no, "Sin bid/ask real — precio no confiable")

    if open_positions >= MAX_POSITIONS:
        return _reject(_no, f"Posiciones abiertas ({open_positions}) ≥ máx ({MAX_POSITIONS})")

    # ── Selección de dirección: el lado que cotiza entre 0.88 y 0.92 ──
    price_yes = market.price_yes
    price_no  = market.price_no

    if BET_RANGE_LO <= price_yes <= BET_RANGE_HI:
        direction   = "YES"
        entry_price = price_yes
    elif BET_RANGE_LO <= price_no <= BET_RANGE_HI:
        direction   = "NO"
        entry_price = price_no
    else:
        return _reject(_no, (
            f"Precio fuera del rango [{BET_RANGE_LO}–{BET_RANGE_HI}]: "
            f"YES={price_yes:.3f}  NO={price_no:.3f}"
        ))

    # ── Tamaño fijo + validación de capital ──
    shares = BET_SHARES
    cost   = entry_price * shares

    if cost > capital:
        return _reject(_no, f"Capital insuficiente: coste=${cost:.2f} > capital=${capital:.2f}")

    # ── Cálculo de rentabilidad esperada ──
    fee_entry    = calc_fee(shares, entry_price)
    fee_exit_est = calc_fee(shares, 1.0)            # ≈ 0 en resolución completa
    gross_edge   = (1.0 - entry_price) * shares
    net_edge     = gross_edge - fee_entry - fee_exit_est
    stop_loss    = entry_price * (1 - STOP_LOSS_PCT)

    reason = (
        f"✓ {direction} @ {entry_price:.3f} | {shares} shares | "
        f"cost=${cost:.2f} | gross=${gross_edge:.2f} | net=${net_edge:.2f} | "
        f"time={market.time_left}"
    )

    return TradeSignal(
        execute=True,
        direction=direction,
        entry_price=entry_price,
        shares=shares,
        fee_entry=fee_entry,
        fee_exit_est=fee_exit_est,
        gross_edge=gross_edge,
        net_edge=net_edge,
        stop_loss_price=stop_loss,
        reason=reason,
    )


def _reject(sig: TradeSignal, reason: str) -> TradeSignal:
    sig.reason = reason
    return sig
