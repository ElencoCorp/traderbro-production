"""
market_holidays.py  —  Single source of truth for NSE/BSE market holidays.
Priority:  NSE live fetch  →  DB cache  →  hardcoded fallback
"""
import sqlite3, logging, requests
from datetime import datetime, date
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_HARDCODED: Dict[str, str] = {
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

MARKET_HOLIDAYS: Dict[str, str] = dict(_HARDCODED)
_DB_FILE = "traderbro.db"

def _init_table():
    conn = sqlite3.connect(_DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS market_holidays_cache (
            date TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            source TEXT DEFAULT 'hardcoded',
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
            ON CONFLICT(date) DO UPDATE SET reason=excluded.reason,
              source=excluded.source, updated_at=excluded.updated_at
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
        holidays = {}
        for seg in ("CM", "FO", "EQ"):
            entries = data.get(seg, [])
            if entries:
                for item in entries:
                    raw = item.get("tradingDate", "")
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

def refresh_holidays():
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
    logger.info("Using hardcoded holidays")

def is_market_holiday(dt) -> bool:
    key = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
    return key in MARKET_HOLIDAYS

def get_holiday_reason(dt) -> Optional[str]:
    key = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10]
    return MARKET_HOLIDAYS.get(key)

def get_holidays_for_year(year: int) -> Dict[str, str]:
    return {k: v for k, v in MARKET_HOLIDAYS.items() if k.startswith(str(year))}

def get_upcoming_holidays(days_ahead: int = 60):
    today = date.today()
    result = []
    for k, v in sorted(MARKET_HOLIDAYS.items()):
        try:
            hdate = date.fromisoformat(k)
            delta = (hdate - today).days
            if 0 <= delta <= days_ahead:
                result.append({
                    "date": k, "reason": v,
                    "days_away": delta, "is_today": delta == 0,
                })
        except Exception:
            pass
    return result

def get_today_holiday() -> Optional[str]:
    return get_holiday_reason(date.today())

def get_all_holidays_list():
    return [{"date": k, "reason": v} for k, v in sorted(MARKET_HOLIDAYS.items())]

def count_holidays_between(start_dt, end_dt) -> int:
    """Count NSE weekday holidays between two datetimes (inclusive)."""
    count = 0
    d = start_dt.date() if hasattr(start_dt, 'date') else start_dt
    e = end_dt.date() if hasattr(end_dt, 'date') else end_dt
    for k in MARKET_HOLIDAYS:
        try:
            hdate = date.fromisoformat(k)
            if d <= hdate <= e and hdate.weekday() < 5:
                count += 1
        except Exception:
            pass
    return count

_init_table()
_save_to_db(_HARDCODED, "hardcoded")
refresh_holidays()