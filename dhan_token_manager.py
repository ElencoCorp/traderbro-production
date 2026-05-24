"""
dhan_token_manager.py
─────────────────────────────────────────────────────────────────────────────
Automatic Dhan API token renewal system for TraderBro.

FIXES in this version:
  1. .env is always the source of truth on startup — token_state.json is only
     used to restore renewal_count / timestamps, NEVER to overwrite a manually
     updated .env token.  This means pasting a new token in .env and restarting
     the server is always sufficient.
  2. set_token_from_env() endpoint lets admin hot-reload the .env token into
     memory WITHOUT restarting the server — no more stale in-memory token.
  3. Smart renewal scheduling: on weekdays the first renewal is attempted at
     09:00 IST (before market opens); on weekends / after 401 the scheduler
     backs off and retries the next weekday morning instead of hammering Dhan
     with expired-token calls every 18 h.
  4. renew_token() detects 401 (expired/invalid token) and immediately stops
     retrying — it logs a clear message and waits for the next weekday window
     or for the admin to inject a fresh token via /api/admin/set-token.
  5. Countdown and progress bar in the UI now reflect the *actual* next-run
     time stored in the APScheduler job, not a stale string in state.
─────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import json
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

IST                    = pytz.timezone("Asia/Kolkata")
ENV_FILE               = Path(".env")
LOG_FILE               = Path("token_renewal.log")
TOKEN_STATE_FILE       = Path("token_state.json")

DHAN_RENEW_URL         = "https://api.dhan.co/v2/RenewToken"
RENEWAL_INTERVAL_HOURS = 18   # normal cadence
RENEWAL_HOUR_IST       = 9    # preferred daily renewal hour (9 AM IST)

# ─────────────────────────────────────────────────────────────────────────────
# LOGGER
# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger("dhan_token_manager")
logger.setLevel(logging.INFO)

if not logger.handlers:
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(_fh)

    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("[TOKEN] %(levelname)s  %(message)s"))
    logger.addHandler(_ch)

# ─────────────────────────────────────────────────────────────────────────────
# TOKEN STATE  (in-memory + JSON file)
# ─────────────────────────────────────────────────────────────────────────────

_state = {
    "current_token":   None,
    "client_id":       None,
    "last_renewed_at": None,
    "next_renewal_at": None,
    "renewal_count":   0,
    "last_error":      None,
    "headers_ref":     None,
    "scheduler_ref":   None,   # kept so we can reschedule after a 401
    "token_invalid":   False,  # True after a 401 — stops automatic retries
}


def _read_env_token() -> str:
    """Read ACCESS_TOKEN directly from .env file (not os.environ cache)."""
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("ACCESS_TOKEN="):
            return line[len("ACCESS_TOKEN="):].strip()
    return ""


def _load_state():
    """
    Restore renewal_count / timestamps from JSON.
    NEVER overwrites current_token — the .env file is always authoritative.
    """
    if not TOKEN_STATE_FILE.exists():
        return

    try:
        data = json.loads(TOKEN_STATE_FILE.read_text(encoding="utf-8"))

        _state["renewal_count"]   = data.get("renewal_count", 0)
        _state["last_renewed_at"] = data.get("last_renewed_at")
        _state["next_renewal_at"] = data.get("next_renewal_at")
        _state["last_error"]      = data.get("last_error")
        _state["token_invalid"]   = data.get("token_invalid", False)

        logger.info(
            f"State restored from JSON — "
            f"renewals: {_state['renewal_count']}, "
            f"last: {_state['last_renewed_at']}, "
            f"token_invalid: {_state['token_invalid']}"
        )
    except Exception as e:
        logger.warning(f"Could not load token state: {e}")


def _save_state():
    """Persist FULL state to JSON — called after every renewal attempt."""
    try:
        TOKEN_STATE_FILE.write_text(json.dumps({
            "current_token":   _state["current_token"],
            "renewal_count":   _state["renewal_count"],
            "last_renewed_at": _state["last_renewed_at"],
            "next_renewal_at": _state["next_renewal_at"],
            "last_error":      _state["last_error"],
            "token_invalid":   _state["token_invalid"],
        }, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not save token state: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# .ENV UPDATER
# ─────────────────────────────────────────────────────────────────────────────

def _update_env_file(new_token: str):
    """Replace ACCESS_TOKEN line in .env with the new token."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"ACCESS_TOKEN={new_token}\n", encoding="utf-8")
        logger.info(".env created with new token.")
        return

    content = ENV_FILE.read_text(encoding="utf-8")
    pattern = r"^ACCESS_TOKEN=.*$"
    new_line = f"ACCESS_TOKEN={new_token}"

    if re.search(pattern, content, flags=re.MULTILINE):
        updated = re.sub(pattern, new_line, content, flags=re.MULTILINE)
    else:
        updated = content.rstrip("\n") + f"\nACCESS_TOKEN={new_token}\n"

    ENV_FILE.write_text(updated, encoding="utf-8")
    logger.info(".env updated with new token.")


# ─────────────────────────────────────────────────────────────────────────────
# NEXT WEEKDAY HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _next_weekday_morning(from_dt: datetime = None) -> datetime:
    """
    Return the next Mon-Fri 09:00 IST datetime, starting from from_dt.
    If from_dt is already a weekday before 09:00, returns today at 09:00.
    """
    if from_dt is None:
        from_dt = datetime.now(IST)

    dt = from_dt.astimezone(IST)

    # If today is a weekday and we haven't passed 09:00, use today
    if dt.weekday() < 5 and dt.hour < RENEWAL_HOUR_IST:
        target = dt.replace(hour=RENEWAL_HOUR_IST, minute=0, second=0, microsecond=0)
        return target

    # Otherwise advance to the next calendar day until we hit Mon-Fri
    dt = dt + timedelta(days=1)
    while dt.weekday() >= 5:          # 5=Sat, 6=Sun
        dt = dt + timedelta(days=1)

    target = dt.replace(hour=RENEWAL_HOUR_IST, minute=0, second=0, microsecond=0)
    return target


# ─────────────────────────────────────────────────────────────────────────────
# CORE RENEWAL FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def renew_token(force: bool = False) -> dict:
    """
    Hit Dhan's /v2/RenewToken endpoint and update everything on success.
    Returns: { "success": bool, "token": str|None, "error": str|None }
    """
    # ── Always re-read .env so a manual paste is picked up immediately ──
    env_token = _read_env_token()
    if env_token and env_token != _state["current_token"]:
        logger.info("Detected updated token in .env — syncing to memory before renewal attempt.")
        _state["current_token"] = env_token
        _state["token_invalid"] = False          # fresh token: clear invalid flag
        _state["last_error"]    = None
        os.environ["ACCESS_TOKEN"] = env_token
        if _state["headers_ref"] is not None:
            _state["headers_ref"]["access-token"] = env_token

    token     = _state["current_token"]
    client_id = _state["client_id"]

    if not token or not client_id:
        msg = "Token or client_id not initialised. Cannot renew."
        logger.error(msg)
        _state["last_error"] = msg
        _save_state()
        return {"success": False, "token": None, "error": msg}

    # ── If token was previously marked invalid, only proceed if forced ──
    if _state["token_invalid"] and not force:
        msg = (
            "Token is marked invalid (last renewal got 401). "
            "Skipping automatic retry — please inject a fresh token via "
            "/api/admin/set-token or update .env and restart."
        )
        logger.warning(msg)
        return {"success": False, "token": None, "error": msg}

    logger.info(f"Attempting token renewal (force={force}) …")

    try:
        resp = requests.get(
            DHAN_RENEW_URL,
            headers={
                "access-token": token,
                "dhanClientId": client_id,
            },
            timeout=15,
        )

        logger.info(f"RenewToken HTTP {resp.status_code}")

        # ── SUCCESS ───────────────────────────────────────────────────────
        if resp.status_code == 200:
            data      = resp.json()
            new_token = (
                data.get("accessToken")
                or data.get("token")
                or data.get("access_token")
            )

            if not new_token:
                msg = f"200 OK but no token in response: {resp.text[:200]}"
                logger.error(msg)
                _state["last_error"] = msg
                _save_state()
                return {"success": False, "token": None, "error": msg}

            # ── Update in-memory state ─────────────────────────────────
            _state["current_token"] = new_token
            _state["last_error"]    = None
            _state["token_invalid"] = False
            _state["renewal_count"] += 1

            now_ist = datetime.now(IST)
            _state["last_renewed_at"] = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")

            # Next renewal: prefer next weekday 09:00, fall back to +18 h
            next_run = _next_weekday_morning(now_ist + timedelta(hours=RENEWAL_INTERVAL_HOURS))
            _state["next_renewal_at"] = next_run.strftime("%Y-%m-%d %H:%M:%S IST")

            # ── Update live HEADERS dict in app.py ─────────────────────
            if _state["headers_ref"] is not None:
                _state["headers_ref"]["access-token"] = new_token
                logger.info("In-memory HEADERS updated.")

            # ── Persist everywhere ─────────────────────────────────────
            _update_env_file(new_token)
            os.environ["ACCESS_TOKEN"] = new_token
            _save_state()

            # ── Reschedule next run ────────────────────────────────────
            _schedule_renewal(_state["scheduler_ref"], next_run_time=next_run)

            logger.info(
                f"✅ Token renewed successfully. "
                f"Renewal #{_state['renewal_count']}. "
                f"Next at {_state['next_renewal_at']}"
            )
            return {"success": True, "token": new_token, "error": None}

        # ── EXPIRED / INVALID TOKEN ───────────────────────────────────────
        elif resp.status_code == 401:
            _state["token_invalid"] = True
            msg = (
                f"RenewToken failed — HTTP 401: {resp.text[:300]}. "
                f"TOKEN IS EXPIRED/INVALID. "
                f"ACTION REQUIRED: Generate a fresh token from the Dhan portal, "
                f"then either: (a) paste it via POST /api/admin/set-token, or "
                f"(b) update .env and call GET /api/admin/reload-env-token."
            )
            logger.error(msg)
            _state["last_error"] = f"RenewToken failed — HTTP 401: {resp.text[:300]}"

            # Reschedule to next weekday morning so we don't hammer Dhan
            next_run = _next_weekday_morning()
            _state["next_renewal_at"] = next_run.strftime("%Y-%m-%d %H:%M:%S IST")
            _schedule_renewal(_state["scheduler_ref"], next_run_time=next_run)

            _save_state()
            return {"success": False, "token": None, "error": _state["last_error"]}

        # ── OTHER HTTP ERROR ──────────────────────────────────────────────
        else:
            msg = f"RenewToken failed — HTTP {resp.status_code}: {resp.text[:300]}"
            logger.error(msg)
            _state["last_error"] = msg
            _save_state()
            return {"success": False, "token": None, "error": msg}

    except requests.Timeout:
        msg = "RenewToken request timed out after 15 s."
        logger.error(msg)
        _state["last_error"] = msg
        _save_state()
        return {"success": False, "token": None, "error": msg}

    except Exception as exc:
        msg = f"Unexpected error during renewal: {exc}"
        logger.exception(msg)
        _state["last_error"] = msg
        _save_state()
        return {"success": False, "token": None, "error": msg}


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────────────────────────────────────

def _scheduled_renewal():
    """Called by APScheduler — checks .env for manual updates first."""
    logger.info("Scheduled token renewal triggered.")
    renew_token()


def _schedule_renewal(scheduler, next_run_time: datetime = None):
    """
    Register (or update) the renewal job with the given APScheduler instance.
    If next_run_time is given, the first run will be at that exact moment;
    subsequent runs follow the RENEWAL_INTERVAL_HOURS cadence.
    """
    if scheduler is None:
        return

    try:
        scheduler.remove_job("dhan_token_renewal")
    except Exception:
        pass

    if next_run_time is None:
        next_run_time = _next_weekday_morning()

    scheduler.add_job(
        _scheduled_renewal,
        "interval",
        hours=RENEWAL_INTERVAL_HOURS,
        id="dhan_token_renewal",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=next_run_time,
    )

    next_run_str = next_run_time.strftime("%Y-%m-%d %H:%M:%S IST")
    _state["next_renewal_at"] = next_run_str

    logger.info(
        f"✅ Token renewal job (re)scheduled. "
        f"Next run: {next_run_str}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# INIT — call once from app.py startup
# ─────────────────────────────────────────────────────────────────────────────

def init_token_manager(app, scheduler, headers_dict: dict):
    """
    Initialise the token manager.

    Parameters
    ----------
    app          : FastAPI instance
    scheduler    : APScheduler BackgroundScheduler (already started)
    headers_dict : The HEADERS dict in app.py — mutated in-place on renewal
    """
    _state["headers_ref"]  = headers_dict
    _state["scheduler_ref"] = scheduler
    _state["client_id"]    = headers_dict.get("client-id") or os.environ.get("CLIENT_ID", "")

    # ── Step 1: .env is the source of truth ───────────────────────────────
    env_token = _read_env_token() or headers_dict.get("access-token") or os.environ.get("ACCESS_TOKEN", "")
    _state["current_token"] = env_token
    os.environ["ACCESS_TOKEN"] = env_token
    headers_dict["access-token"] = env_token
    logger.info(f"Token loaded from .env: {env_token[:20]}…")

    # ── Step 2: Restore renewal count / timestamps only from JSON ─────────
    _load_state()

    # After _load_state the token in _state["current_token"] may have been
    # overwritten by an old saved token — re-apply the fresh .env token.
    if env_token:
        _state["current_token"]          = env_token
        _state["token_invalid"]          = False   # fresh start: trust .env
        _state["last_error"]             = None
        os.environ["ACCESS_TOKEN"]       = env_token
        headers_dict["access-token"]     = env_token

    # ── Step 3: Schedule first renewal ───────────────────────────────────
    next_run = _next_weekday_morning()
    _schedule_renewal(scheduler, next_run_time=next_run)

    logger.info(
        f"Token manager initialised. "
        f"Client ID: {_state['client_id']} | "
        f"Token (first 20 chars): {str(_state['current_token'])[:20]}… | "
        f"Renewals so far: {_state['renewal_count']} | "
        f"Next renewal: {next_run.strftime('%Y-%m-%d %H:%M:%S IST')}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI ROUTER
# ─────────────────────────────────────────────────────────────────────────────

router = APIRouter()


@router.get("/api/admin/token-status")
def token_status(request: Request):
    """Return token health info. Admin only."""
    if request.session.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token  = _state["current_token"] or ""
    masked = (token[:10] + "…" + token[-8:]) if len(token) > 20 else "not set"

    # Pull the actual next_run_time from the scheduler job for accuracy
    next_run_str = _state["next_renewal_at"] or "Unknown"
    sched = _state.get("scheduler_ref")
    if sched:
        job = sched.get_job("dhan_token_renewal")
        if job and job.next_run_time:
            next_run_str = job.next_run_time.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    return JSONResponse({
        "token_masked":    masked,
        "client_id":       _state["client_id"],
        "last_renewed_at": _state["last_renewed_at"] or "Never (original token)",
        "next_renewal_at": next_run_str,
        "renewal_count":   _state["renewal_count"],
        "last_error":      _state["last_error"],
        "token_invalid":   _state["token_invalid"],
        "status":          "error" if _state["last_error"] else "ok",
    })


@router.post("/api/admin/renew-token")
def manual_renew(request: Request):
    """Manually trigger a token renewal. Admin only."""
    if request.session.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    result = renew_token(force=True)
    return JSONResponse(result)


@router.get("/api/admin/token-logs")
def token_logs(request: Request):
    """Return last 50 lines from the renewal log. Admin only."""
    if request.session.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not LOG_FILE.exists():
        return JSONResponse({"lines": []})

    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return JSONResponse({"lines": lines[-50:]})


@router.get("/api/admin/reload-env-token")
def reload_env_token(request: Request):
    """
    Hot-reload the token from .env into memory WITHOUT restarting the server.
    Use this after manually pasting a new token into .env.
    Admin only.
    """
    if request.session.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    new_token = _read_env_token()
    if not new_token or len(new_token) < 20:
        return JSONResponse({
            "success": False,
            "error": "No valid ACCESS_TOKEN found in .env"
        })

    old_token = _state["current_token"] or ""
    if new_token == old_token:
        return JSONResponse({
            "success": True,
            "message": "Token in memory already matches .env — no change needed.",
            "token_masked": (new_token[:10] + "…" + new_token[-8:])
        })

    # Apply the new token everywhere
    _state["current_token"] = new_token
    _state["token_invalid"] = False
    _state["last_error"]    = None
    os.environ["ACCESS_TOKEN"] = new_token

    if _state["headers_ref"] is not None:
        _state["headers_ref"]["access-token"] = new_token

    # Reschedule renewal so the next run uses the fresh token
    next_run = _next_weekday_morning()
    _schedule_renewal(_state["scheduler_ref"], next_run_time=next_run)

    _save_state()

    logger.info(
        f"✅ Token hot-reloaded from .env by admin. "
        f"New token (first 20): {new_token[:20]}…"
    )

    return JSONResponse({
        "success":      True,
        "message":      "Token reloaded from .env into memory successfully.",
        "token_masked": (new_token[:10] + "…" + new_token[-8:]),
        "next_renewal": next_run.strftime("%Y-%m-%d %H:%M:%S IST"),
    })


@router.post("/api/admin/set-token")
async def set_token_manually(request: Request):
    """
    Admin emergency endpoint: paste a fresh Dhan token here when the
    auto-renewer is stuck in a 401 loop.

    Body: { "access_token": "eyJ0eX..." }
    """
    if request.session.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    data  = await request.json()
    token = (data.get("access_token") or "").strip()

    if not token or len(token) < 20:
        return JSONResponse({"success": False, "error": "Invalid token provided"})

    # Update state
    _state["current_token"] = token
    _state["last_error"]    = None
    _state["token_invalid"] = False

    now_ist = datetime.now(IST)
    _state["last_renewed_at"] = now_ist.strftime("%Y-%m-%d %H:%M:%S IST") + " (manual)"
    _state["renewal_count"] += 1

    # Sync everywhere
    if _state["headers_ref"] is not None:
        _state["headers_ref"]["access-token"] = token

    os.environ["ACCESS_TOKEN"] = token
    _update_env_file(token)

    # Reschedule with the fresh token
    next_run = _next_weekday_morning()
    _schedule_renewal(_state["scheduler_ref"], next_run_time=next_run)

    _save_state()

    logger.info(
        f"✅ Token manually set by admin via /api/admin/set-token. "
        f"Renewal #{_state['renewal_count']}."
    )
    return JSONResponse({
        "success":        True,
        "renewal_count":  _state["renewal_count"],
        "next_renewal":   next_run.strftime("%Y-%m-%d %H:%M:%S IST"),
    })
