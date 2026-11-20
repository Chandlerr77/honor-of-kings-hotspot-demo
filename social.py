import re
from datetime import datetime, timedelta, timezone

DAYS = 7

def count(value):
    return int(value) if re.fullmatch(r"\d{1,18}", str(value)) else None

def recent(value):
    ts = count(value)
    if ts is None:
        return None
    if ts > 10**11:
        ts /= 1000
    if ts > 10**10:
        return None
    when = datetime.fromtimestamp(ts, timezone.utc)
    now = datetime.now(timezone.utc)
    return when.isoformat() if now - timedelta(days=DAYS) <= when <= now else None
