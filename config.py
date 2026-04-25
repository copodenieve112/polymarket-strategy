# config.py

POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"

REFRESH_INTERVAL = 5
REQUEST_TIMEOUT  = 10
DEBUG_MODE       = False  # True = imprime datos crudos de la API

# Todas las series de interés: (series_slug, ticker, window_minutes)
# Cada serie tiene sus propios mercados en /events?series_slug=<slug>
SERIES = [
    # 5-minute
    ("btc-up-or-down-5m",    "BTC",  5),
    ("eth-up-or-down-5m",    "ETH",  5),
    ("sol-up-or-down-5m",    "SOL",  5),
    ("xrp-up-or-down-5m",    "XRP",  5),
    ("bnb-up-or-down-5m",    "BNB",  5),
    ("doge-up-or-down-5m",   "DOGE", 5),
    ("hype-up-or-down-5m",   "HYPE", 5),
    # 15-minute
    ("btc-up-or-down-15m",   "BTC",  15),
    ("eth-up-or-down-15m",   "ETH",  15),
    ("sol-up-or-down-15m",   "SOL",  15),
    ("xrp-up-or-down-15m",   "XRP",  15),
    ("bnb-up-or-down-15m",   "BNB",  15),
    ("doge-up-or-down-15m",  "DOGE", 15),
    ("hype-up-or-down-15m",  "HYPE", 15),
    # 1-hour
    ("btc-up-or-down-hourly",    "BTC",  60),
    ("eth-up-or-down-hourly",    "ETH",  60),
    ("solana-up-or-down-hourly", "SOL",  60),
    ("xrp-up-or-down-hourly",    "XRP",  60),
    ("bnb-up-or-down-hourly",    "BNB",  60),
    ("doge-up-or-down-hourly",   "DOGE", 60),
    ("hype-up-or-down-hourly",   "HYPE", 60),
]
