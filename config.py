# config.py

POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"

REFRESH_INTERVAL = 5
REQUEST_TIMEOUT  = 10
DEBUG_MODE       = False  # True = imprime datos crudos de la API

# Series activas: BTC, ETH, SOL, XRP  ×  5m, 15m, 1h
SERIES = [
    # 5-minute
    ("btc-up-or-down-5m",    "BTC",  5),
    ("eth-up-or-down-5m",    "ETH",  5),
    ("sol-up-or-down-5m",    "SOL",  5),
    ("xrp-up-or-down-5m",    "XRP",  5),
    # 15-minute
    ("btc-up-or-down-15m",   "BTC",  15),
    ("eth-up-or-down-15m",   "ETH",  15),
    ("sol-up-or-down-15m",   "SOL",  15),
    ("xrp-up-or-down-15m",   "XRP",  15),
    # 1-hour
    ("btc-up-or-down-hourly",    "BTC",  60),
    ("eth-up-or-down-hourly",    "ETH",  60),
    ("solana-up-or-down-hourly", "SOL",  60),
    ("xrp-up-or-down-hourly",    "XRP",  60),
]
