# strategy.py — Multi-timeframe systematic strategy with fractional Kelly sizing

from dataclasses import dataclass
from typing import Dict, List, Optional
from models import Market

# ── Parameters ────────────────────────────────────────────────────────────────

FEE_RATE = 0.072  # Polymarket taker fee

# Entry timing windows (seconds before close): (min, max)
ENTRY_WINDOW = {
    "5m":  (20,  90),
    "15m": (30, 120),
    "1h":  (60, 300),
}

MAX_SPREAD       = 0.06   # reject if bid-ask spread > 6%
SIGNAL_THRESHOLD = 0.12   # minimum |score| to enter
CONFLUENCE_MIN   = 0.55   # each confirming TF must show >= this probability
MIN_EDGE         = 0.02   # minimum net expected value after fees ($)

KELLY_FRACTION   = 0.25   # fractional Kelly multiplier (conservative)
MAX_RISK_PCT     = 0.03   # max 3% of bankroll per trade
MIN_SHARES       = 1
MAX_SHARES       = 200

MAX_POSITIONS    = 3
MIN_CAPITAL      = 10.0
STOP_LOSS_PCT    = 0.15   # exit early if price drops 15% from entry
TAKE_PROFIT_LVL  = 0.97   # exit early if price reaches 97%
MIN_HISTORY_BARS = 3      # bars needed for momentum feature

# Cooldown: skip N cycles after losing streak
LOSS_STREAK_COOLDOWN = 3   # consecutive losses before pausing
COOLDOWN_CYCLES      = 6   # cycles to pause (6 × 5s = 30s)

# Signal weights (must sum to 1.0)
W_BIAS     = 0.40  # current 5m price deviation
W_15M      = 0.25  # 15m cross-timeframe confirmation
W_1H       = 0.20  # 1h directional bias
W_MOMENTUM = 0.15  # recent price trend from history


# ── Trade signal ──────────────────────────────────────────────────────────────

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
    signal_score:    float = 0.0
    kelly_f:         float = 0.0
    p_est:           float = 0.0


# ── Fees ──────────────────────────────────────────────────────────────────────

def calc_fee(shares: float, price: float) -> float:
    """Polymarket fee: FEE_RATE × shares × p × (1−p). Zero at resolution (p=1)."""
    return FEE_RATE * shares * price * (1.0 - price)


# ── Features ──────────────────────────────────────────────────────────────────

def _normalize(p: float) -> float:
    """Map probability [0,1] → centered signal [−1,+1]."""
    return (p - 0.5) * 2.0


def _momentum(history: List[float]) -> float:
    """
    Linear regression slope over recent price history, normalized to [−1,+1].
    Positive = prices rising (YES trending up).
    """
    if len(history) < MIN_HISTORY_BARS:
        return 0.0
    n = min(len(history), 10)
    vals = history[-n:]
    x_mean = (n - 1) / 2.0
    y_mean = sum(vals) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(vals))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    slope = num / den
    # Normalize: 0.01 change per bar = strong trend
    return max(-1.0, min(1.0, slope / 0.01))


def compute_features(
    market:    Market,
    mkt_15m:   Optional[Market],
    mkt_1h:    Optional[Market],
    price_hist: List[float],
) -> dict:
    """
    Returns feature dict for a single market. All features in [−1,+1].
    Positive direction = bullish YES.
    """
    return {
        "f1_bias":     _normalize(market.price_yes),
        "f2_15m":      _normalize(mkt_15m.price_yes) if mkt_15m else 0.0,
        "f3_1h":       _normalize(mkt_1h.price_yes)  if mkt_1h  else 0.0,
        "f4_momentum": _momentum(price_hist),
    }


# ── Signal score ──────────────────────────────────────────────────────────────

def compute_signal(feats: dict) -> float:
    """
    Weighted sum → single score in [−1,+1].
    Positive → bet YES, Negative → bet NO.
    """
    score = (
        W_BIAS     * feats["f1_bias"] +
        W_15M      * feats["f2_15m"] +
        W_1H       * feats["f3_1h"]  +
        W_MOMENTUM * feats["f4_momentum"]
    )
    return max(-1.0, min(1.0, score))


# ── Kelly sizing ──────────────────────────────────────────────────────────────

def kelly_shares(p_est: float, ask: float, capital: float) -> tuple:
    """
    Fractional Kelly position sizing for a binary prediction market.

    Formula derivation:
      Standard Kelly: f* = (b·p − q) / b
      For binary market with execution at ask:
        b = (1 − ask) / ask   (net odds per unit staked)
        Simplifies to: f* = (p_est − ask) / (1 − ask)

    Returns (shares, raw_kelly_f, gross_edge_per_share).
    """
    if not (0.0 < ask < 1.0):
        return 0, 0.0, 0.0

    kelly_f = (p_est - ask) / (1.0 - ask)

    if kelly_f <= 0:
        return 0, kelly_f, 0.0

    f_adj    = kelly_f * KELLY_FRACTION
    f_capped = min(f_adj, MAX_RISK_PCT)

    dollar_risk = f_capped * capital
    shares = int(dollar_risk / ask)
    shares = max(MIN_SHARES, min(shares, MAX_SHARES))

    return shares, kelly_f, (1.0 - ask)


# ── Early exit logic ──────────────────────────────────────────────────────────

def should_exit_early(
    entry_price: float,
    current_price: float,
    time_left_secs: float,
    window_label: str,
) -> tuple:
    """
    Returns (exit: bool, reason: str).
    Called each cycle for open positions.
    """
    # Stop loss: price dropped 15% from entry
    if current_price <= entry_price * (1.0 - STOP_LOSS_PCT):
        return True, f"Stop loss: {current_price:.3f} ≤ {entry_price*(1-STOP_LOSS_PCT):.3f}"

    # Take profit: price near 1.0
    if current_price >= TAKE_PROFIT_LVL:
        return True, f"Take profit: {current_price:.3f} ≥ {TAKE_PROFIT_LVL}"

    # Time-based exit: for 1h markets, bail 90s before close to avoid resolution noise
    if window_label == "1h" and 0 < time_left_secs < 90:
        return True, f"1h time exit: {time_left_secs:.0f}s < 90s"

    return False, ""


# ── Main evaluate ─────────────────────────────────────────────────────────────

def evaluate(
    market:      Market,
    mkt_15m:     Optional[Market],
    mkt_1h:      Optional[Market],
    price_hist:  List[float],
    open_positions: int,
    capital:     float,
    cooldown_remaining: int = 0,
) -> TradeSignal:
    """
    Full decision engine. Returns a TradeSignal with execute=True only when
    all filters pass: timing, spread, signal strength, confluence, Kelly edge.
    """
    _no = TradeSignal(False, "", 0, 0, 0, 0, 0, 0, 0, "", 0.0, 0.0, 0.0)

    # ── Hard guards ───────────────────────────────────────────────────────────
    if cooldown_remaining > 0:
        return _reject(_no, f"Cooldown activo: {cooldown_remaining} ciclos")

    if capital < MIN_CAPITAL:
        return _reject(_no, f"Capital insuficiente: ${capital:.2f}")

    if open_positions >= MAX_POSITIONS:
        return _reject(_no, f"Posiciones máximas alcanzadas ({MAX_POSITIONS})")

    if not market.has_real_price:
        return _reject(_no, "Sin order book real (bid/ask ausente)")

    # ── Timing gate ───────────────────────────────────────────────────────────
    secs = market.time_left_seconds
    lo, hi = ENTRY_WINDOW.get(market.window_label, (20, 90))
    if not (lo <= secs <= hi):
        return _reject(_no, f"Fuera de ventana [{lo}–{hi}s]: {secs:.0f}s restantes")

    # ── Spread gate ───────────────────────────────────────────────────────────
    spread = market.spread
    if spread > MAX_SPREAD:
        return _reject(_no, f"Spread demasiado ancho: {spread:.3f} > {MAX_SPREAD}")

    # ── Features + signal ─────────────────────────────────────────────────────
    feats = compute_features(market, mkt_15m, mkt_1h, price_hist)
    score = compute_signal(feats)

    if abs(score) < SIGNAL_THRESHOLD:
        return _reject(_no, f"Señal débil: |{score:.3f}| < {SIGNAL_THRESHOLD}")

    direction = "YES" if score > 0 else "NO"

    # ── Execution price (use ask to buy YES, or ask of NO side) ───────────────
    if direction == "YES":
        exec_price = market.ask if market.ask > 0 else market.price_yes
        p_self  = market.price_yes
        p_15m   = mkt_15m.price_yes if mkt_15m else 0.5
        p_1h    = mkt_1h.price_yes  if mkt_1h  else 0.5
    else:
        # Buying NO: NO token price ≈ 1 − bid_yes
        exec_price = (1.0 - market.bid) if market.bid > 0 else market.price_no
        p_self  = market.price_no
        p_15m   = mkt_15m.price_no if mkt_15m else 0.5
        p_1h    = mkt_1h.price_no  if mkt_1h  else 0.5

    if not (0.0 < exec_price < 1.0):
        return _reject(_no, f"Precio de ejecución inválido: {exec_price:.3f}")

    # ── Confluence gate ───────────────────────────────────────────────────────
    # At least 2 of 3 timeframes must agree (show >= CONFLUENCE_MIN probability)
    confluence = sum(1 for p in [p_self, p_15m, p_1h] if p >= CONFLUENCE_MIN)
    if confluence < 2:
        return _reject(_no, f"Confluencia insuficiente: {confluence}/3 TF ≥ {CONFLUENCE_MIN}")

    # ── Probability estimate (weighted average across timeframes) ─────────────
    p_est = 0.50 * p_self + 0.30 * p_15m + 0.20 * p_1h
    p_est = max(0.01, min(0.99, p_est))

    # ── Kelly sizing ──────────────────────────────────────────────────────────
    shares, kelly_f, _ = kelly_shares(p_est, exec_price, capital)

    if shares < MIN_SHARES:
        return _reject(_no, f"Kelly→0 shares (p_est={p_est:.3f} ask={exec_price:.3f} f*={kelly_f:.3f})")

    cost = exec_price * shares
    if cost > capital:
        shares = max(MIN_SHARES, int(capital * MAX_RISK_PCT / exec_price))
        cost   = exec_price * shares

    # ── Edge check ────────────────────────────────────────────────────────────
    fee_entry    = calc_fee(shares, exec_price)
    fee_exit_est = calc_fee(shares, 1.0)          # ~0 at full resolution
    gross_edge   = (1.0 - exec_price) * shares
    net_edge     = gross_edge - fee_entry - fee_exit_est

    if net_edge < MIN_EDGE:
        return _reject(_no, f"Edge insuficiente: ${net_edge:.3f} < ${MIN_EDGE}")

    stop_loss = exec_price * (1.0 - STOP_LOSS_PCT)

    reason = (
        f"✓ {direction} | score={score:+.3f} | p_est={p_est:.3f} | "
        f"ask={exec_price:.3f} | {shares}sh | cost=${cost:.2f} | "
        f"net=${net_edge:.2f} | conf={confluence}/3 | "
        f"feats=[b={feats['f1_bias']:+.2f} 15m={feats['f2_15m']:+.2f} "
        f"1h={feats['f3_1h']:+.2f} mom={feats['f4_momentum']:+.2f}]"
    )

    return TradeSignal(
        execute=True,
        direction=direction,
        entry_price=exec_price,
        shares=shares,
        fee_entry=fee_entry,
        fee_exit_est=fee_exit_est,
        gross_edge=gross_edge,
        net_edge=net_edge,
        stop_loss_price=stop_loss,
        reason=reason,
        signal_score=score,
        kelly_f=kelly_f,
        p_est=p_est,
    )


def _reject(sig: TradeSignal, reason: str) -> TradeSignal:
    sig.reason = reason
    return sig
