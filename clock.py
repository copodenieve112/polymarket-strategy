# clock.py — Corrects local clock skew against Polymarket API server time.
#
# The machine's UTC clock may be off (e.g., 1h behind actual UTC).
# All time comparisons in data_feed, models, and app use now_utc() from here.

import time
import requests
from datetime import datetime
from email.utils import parsedate_to_datetime

_offset: float = 0.0       # seconds to add to time.time() to get real UTC
_calibrated: bool = False


def calibrate() -> float:
    """
    Fetch one request from the Gamma API and compute the offset between
    the server's Date header and our local clock. Stores result globally.
    Returns the offset in seconds.
    """
    global _offset, _calibrated
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"limit": 1},
            timeout=8,
            headers={"Cache-Control": "no-cache"},
        )
        date_hdr = r.headers.get("Date", "")
        if date_hdr:
            server_ts = parsedate_to_datetime(date_hdr).timestamp()
            _offset = server_ts - time.time()
    except Exception:
        _offset = 0.0
    _calibrated = True
    return _offset


def now_utc() -> datetime:
    """Return the current UTC time, corrected for local clock skew."""
    global _calibrated
    if not _calibrated:
        calibrate()
    return datetime.utcfromtimestamp(time.time() + _offset)
