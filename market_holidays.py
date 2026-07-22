"""
market_holidays.py  —  Single source of truth for NSE/BSE market holidays.
Priority:  NSE live fetch  →  DB cache  →  hardcoded fallback

Features:
  - get_upcoming_holidays(days_ahead=5) — for dashboard banners
  - count_trading_days(start, end) — skips weekends + holidays
  - calculate_expiry_trading_days(start, n) — subscription expiry
  - is_market_holiday() / get_holiday_reason()
"""
import sqlite3, logging, requests
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# HARDCODED FALLBACK (always present even without DB / NSE API)
# ────────────────────────────────────────────────────────────────────────────
_HARDCODED: Dict[str, str] = {
    # 2025
    "2025-01-26": "Republic Day",
    "2025-03-14": "Holi",
    "2025-03-31": "Id-Ul-Fitr (Ramadan Eid)",
    "2025-04-10": "Shri Ram Navami",
    "2025-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2025-04-18": "Good Friday",
    "2025-05-01": "Maharashtra Day",
    "2025-08-15": "Independence Day",
    "2025-08-27": "Ganesh Chaturthi",
    "2025-10-02": "Gandhi Jayanti",
    "2025-10-24": "Diwali - Laxmi Pujan",
    "2025-11-05": "Gurunanak Jayanti",
    "2025-12-25": "Christmas",
    # 2026
    "2026-01-15": "Maharashtra Municipal Corporation Election",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id (Eid ul-Adha)",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti",
    "2026-10-20": "Diwali - Laxmi Pujan",
    "2026-10-21": "Diwali - Balipratipada",
    "2026-11-05": "Gurunanak Jayanti",
    "2026-12-25": "Christmas",
}

# Live copy — updated by refresh_holidays()
MARKET_HOLIDAYS: Dict[str, str] = dict(_HARDCODED)

_DB_FILE = "traderbro.db"

# ────────────────────────────────────────────────────────────────────────────
# DB TABLE
# ────────────────────────────────────────────────────────────────────────────
def _init_table():
    conn = sqlite3.connect(_DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS market_holidays_cache (
            date       TEXT PRIMARY KEY,
            reason     TEXT NOT NULL,
            source     TEXT DEFAULT 'hardcoded',
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def _save_to_db(holidays: Dict[str, str], source: str = "hardcoded"):
    conn = sqlite3.connect(_DB_FILE)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    for d, r in holidays.items():
        c.execute("""
            INSERT INTO market_holidays_cache (date, reason, source, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE
            SET reason=excluded.reason, source=excluded.source, updated_at=excluded.updated_at
        """, (d, r, source, now))
    conn.commit()
    conn.close()


def _load_from_db() -> Dict[str, str]:
    conn = sqlite3.connect(_DB_FILE)
    c = conn.cursor()
    c.execute("SELECT date, reason FROM market_holidays_cache")
    rows = c.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


# ────────────────────────────────────────────────────────────────────────────
# NSE LIVE FETCH
# ────────────────────────────────────────────────────────────────────────────
_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}


def _fetch_nse() -> Optional[Dict[str, str]]:
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=_NSE_HEADERS, timeout=8)
        resp = session.get(
            "https://www.nseindia.com/api/holiday-master?type=trading",
            headers=_NSE_HEADERS, timeout=8
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        holidays: Dict[str, str] = {}
        for seg in ("CM", "FO", "EQ"):
            entries = data.get(seg, [])
            if entries:
                for item in entries:
                    raw    = item.get("tradingDate", "")
                    reason = item.get("description", "Holiday")
                    try:
                        dt = datetime.strptime(raw.strip(), "%d-%b-%Y")
                        holidays[dt.strftime("%Y-%m-%d")] = reason.strip()
                    except Exception:
                        pass
                if holidays:
                    break
        return holidays if len(holidays) >= 8 else None
    except Exception as e:
        logger.warning(f"NSE fetch error: {e}")
        return None


# ────────────────────────────────────────────────────────────────────────────
# PUBLIC: refresh + load
# ────────────────────────────────────────────────────────────────────────────
def refresh_holidays():
    """Try NSE API → DB cache → hardcoded fallback."""
    global MARKET_HOLIDAYS
    fetched = _fetch_nse()
    if fetched:
        MARKET_HOLIDAYS = {**_HARDCODED, **fetched}
        _save_to_db(fetched, "nse_api")
        logger.info(f"Holidays refreshed from NSE API: {len(fetched)}")
        return
    cached = _load_from_db()
    if cached:
        MARKET_HOLIDAYS = {**_HARDCODED, **cached}
        logger.info(f"Holidays from DB cache: {len(cached)}")
        return
    MARKET_HOLIDAYS = dict(_HARDCODED)
    logger.info("Using hardcoded holidays only")


# ────────────────────────────────────────────────────────────────────────────
# BASIC QUERIES
# ────────────────────────────────────────────────────────────────────────────
def is_market_holiday(dt) -> bool:
    key = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
    return key in MARKET_HOLIDAYS


def get_holiday_reason(dt) -> Optional[str]:
    key = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
    return MARKET_HOLIDAYS.get(key)


def get_holidays_for_year(year: int) -> Dict[str, str]:
    return {k: v for k, v in MARKET_HOLIDAYS.items() if k.startswith(str(year))}


def get_today_holiday() -> Optional[str]:
    return get_holiday_reason(date.today())


def get_all_holidays_list() -> List[dict]:
    return [{"date": k, "reason": v} for k, v in sorted(MARKET_HOLIDAYS.items())]


# ────────────────────────────────────────────────────────────────────────────
# UPCOMING HOLIDAYS  (used for dashboard notifications)
# ────────────────────────────────────────────────────────────────────────────
def get_upcoming_holidays(days_ahead: int = 5) -> List[dict]:
    """
    Returns NSE holidays in the next `days_ahead` calendar days (default 5).
    Each entry has:
        date, reason, days_away, is_today,
        weekday_name, formatted_date, is_weekend_adjacent
    """
    today  = date.today()
    result = []

    for k, v in sorted(MARKET_HOLIDAYS.items()):
        try:
            hdate = date.fromisoformat(k)
            delta = (hdate - today).days
            if 0 <= delta <= days_ahead:
                dow = hdate.strftime("%A")
                # Flag if holiday is Monday (weekend-adjacent) or Friday
                is_adj = dow in ("Monday", "Friday")
                result.append({
                    "date":           k,
                    "reason":         v,
                    "days_away":      delta,
                    "is_today":       delta == 0,
                    "weekday_name":   dow,
                    "formatted_date": hdate.strftime("%d %b %Y"),
                    "is_weekend_adjacent": is_adj,
                    "long_weekend":   is_adj,
                })
        except Exception:
            pass

    return result


# ────────────────────────────────────────────────────────────────────────────
# SUBSCRIPTION HELPERS — trading-day aware
# ────────────────────────────────────────────────────────────────────────────
def is_trading_day(dt) -> bool:
    """True if dt is a Mon–Fri that is not an NSE holiday."""
    d = dt.date() if hasattr(dt, "date") else dt
    return d.weekday() < 5 and not is_market_holiday(d)


def count_trading_days_between(start_dt, end_dt) -> int:
    """
    Count trading days (Mon–Fri, non-holiday) from start_dt up to
    AND INCLUDING end_dt.
    """
    d = start_dt.date() if hasattr(start_dt, "date") else start_dt
    e = end_dt.date()   if hasattr(end_dt,   "date") else end_dt
    count = 0
    while d <= e:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def calculate_expiry_from_start(
    start_dt,
    trading_days: int
) -> Tuple[object, int, int, int]:
    """
    start_dt must be midnight (00:00) of the first trading day.
    Dashboard stays visible through the ENTIRE last trading day.
    Expiry = midnight (00:00) of the day AFTER the last trading day,
    so access cuts off exactly at 12 AM the next day.
    """
    if not hasattr(start_dt, "hour"):
        from datetime import datetime as _dt
        start_dt = _dt.combine(start_dt, _dt.min.time())

    last_day  = start_dt.replace(second=0, microsecond=0)
    added     = 0
    weekends  = 0
    holidays  = 0
    days_to_add = trading_days - 1   # start day is day #1

    while added < days_to_add:
        last_day = last_day + timedelta(days=1)
        dow = last_day.weekday()
        if dow >= 5:
            weekends += 1
            continue
        if is_market_holiday(last_day):
            holidays += 1
            continue
        added += 1

    total_calendar = (last_day.date() - start_dt.date()).days + 1
    expiry = last_day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    return expiry, total_calendar, weekends, holidays

def get_holidays_in_subscription(start_dt, end_dt) -> List[dict]:
    """
    Return all NSE weekday holidays that fall within a subscription window.
    Used to display to users which holidays are included/skipped.
    """
    s = start_dt.date() if hasattr(start_dt, "date") else start_dt
    e = end_dt.date()   if hasattr(end_dt,   "date") else end_dt
    result = []
    for k, v in sorted(MARKET_HOLIDAYS.items()):
        try:
            hdate = date.fromisoformat(k)
            if s <= hdate <= e and hdate.weekday() < 5:
                result.append({
                    "date":         k,
                    "reason":       v,
                    "weekday_name": hdate.strftime("%A"),
                    "formatted":    hdate.strftime("%d %b %Y"),
                })
        except Exception:
            pass
    return result


def count_holidays_between(start_dt, end_dt) -> int:
    return len(get_holidays_in_subscription(start_dt, end_dt))


# ────────────────────────────────────────────────────────────────────────────
# NEXT TRADING DAY
# ────────────────────────────────────────────────────────────────────────────
def get_next_trading_day_after(dt) -> object:
    """
    dt is expected to already be midnight of the day a previous plan deactivates.
    If that day is a trading day, the new plan starts exactly then;
    otherwise roll forward to the next trading day at midnight.
    """
    from datetime import datetime as _dt
    nxt = (dt if hasattr(dt, "hour") else _dt.combine(dt, _dt.min.time()))
    nxt = nxt.replace(hour=0, minute=0, second=0, microsecond=0)
    while nxt.weekday() >= 5 or is_market_holiday(nxt):
        nxt += timedelta(days=1)
    return nxt


# ────────────────────────────────────────────────────────────────────────────
# BOOT
# ────────────────────────────────────────────────────────────────────────────
_init_table()
_save_to_db(_HARDCODED, "hardcoded")
refresh_holidays()
