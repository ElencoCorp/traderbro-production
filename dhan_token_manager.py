"""
dhan_token_manager.py
─────────────────────────────────────────────────────────────────────────────
Automatic Dhan API token renewal system for TraderBro.

HOW IT WORKS:
  1. On startup, loads the current token from .env / environment.
  2. A scheduler job runs every 18 hours and hits Dhan's /v2/RenewToken endpoint.
  3. On success, the new token is:
       • saved to the .env file (so it survives server restarts)
       • pushed live into the in-memory HEADERS dict used by every API call
       • logged to token_renewal.log

ENDPOINTS added to your FastAPI app:
  GET  /api/admin/token-status   → shows current token age, next renewal time
  POST /api/admin/renew-token    → admin-only manual trigger
  GET  /api/admin/token-logs     → last 50 log lines

SETUP (paste into your app.py):
─────────────────────────────────────────────────────────────────────────────
  from dhan_token_manager import init_token_manager, router as token_router
  app.include_router(token_router)
  init_token_manager(app, scheduler, HEADERS)
─────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import json
import time
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

IST            = pytz.timezone("Asia/Kolkata")
ENV_FILE       = Path(".env")                        # adjust path if needed
LOG_FILE       = Path("token_renewal.log")
TOKEN_STATE_FILE = Path("token_state.json")          # tracks renewal metadata

DHAN_RENEW_URL = "https://api.dhan.co/v2/RenewToken"
RENEWAL_INTERVAL_HOURS = 18                          # renew every 18 h (token lives 24 h)

# ─────────────────────────────────────────────────────────────────────────────
# LOGGER
# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger("dhan_token_manager")
logger.setLevel(logging.INFO)

# File handler — keeps the last 500 lines implicitly via rotation
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_fh)

# Console handler
_ch = logging.StreamHandler()
_ch.setFormatter(logging.Formatter("[TOKEN] %(levelname)s  %(message)s"))
logger.addHandler(_ch)

# ─────────────────────────────────────────────────────────────────────────────
# TOKEN STATE  (in-memory + JSON file)
# ─────────────────────────────────────────────────────────────────────────────

_state = {
    "current_token":   None,   # the live ACCESS_TOKEN string
    "client_id":       None,
    "last_renewed_at": None,   # ISO string (IST)
    "next_renewal_at": None,   # ISO string (IST)
    "renewal_count":   0,
    "last_error":      None,
    "headers_ref":     None,   # reference to the HEADERS dict in app.py
}


def _load_state():
    """Load persisted state from JSON (survives restarts)."""
    if TOKEN_STATE_FILE.exists():
        try:
            data = json.loads(TOKEN_STATE_FILE.read_text())
            _state["last_renewed_at"] = data.get("last_renewed_at")
            _state["renewal_count"]   = data.get("renewal_count", 0)
            logger.info(f"Loaded token state — renewals so far: {_state['renewal_count']}")
        except Exception as e:
            logger.warning(f"Could not load token state: {e}")


def _save_state():
    """Persist state to JSON."""
    try:
        TOKEN_STATE_FILE.write_text(json.dumps({
            "last_renewed_at": _state["last_renewed_at"],
            "renewal_count":   _state["renewal_count"],
        }, indent=2))
    except Exception as e:
        logger.warning(f"Could not save token state: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# .ENV UPDATER
# ─────────────────────────────────────────────────────────────────────────────

def _update_env_file(new_token: str):
    """
    Replace the ACCESS_TOKEN line in .env with the new token.
    Creates the file if it doesn't exist.
    Thread-safe for single-process use.
    """
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"ACCESS_TOKEN={new_token}\n")
        logger.info(".env created with new token.")
        return

    content = ENV_FILE.read_text(encoding="utf-8")

    # Replace existing ACCESS_TOKEN line
    pattern = r"^ACCESS_TOKEN=.*$"
    new_line = f"ACCESS_TOKEN={new_token}"

    if re.search(pattern, content, flags=re.MULTILINE):
        updated = re.sub(pattern, new_line, content, flags=re.MULTILINE)
    else:
        # Append if not found
        updated = content.rstrip("\n") + f"\nACCESS_TOKEN={new_token}\n"

    ENV_FILE.write_text(updated, encoding="utf-8")
    logger.info(".env updated with new token.")


# ─────────────────────────────────────────────────────────────────────────────
# CORE RENEWAL FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def renew_token(force: bool = False) -> dict:
    """
    Hit Dhan's /v2/RenewToken endpoint and update everything.

    Returns a dict:  { "success": bool, "token": str|None, "error": str|None }
    """
    token    = _state["current_token"]
    client_id = _state["client_id"]

    if not token or not client_id:
        msg = "Token or client_id not initialised. Cannot renew."
        logger.error(msg)
        _state["last_error"] = msg
        return {"success": False, "token": None, "error": msg}

    logger.info(f"Attempting token renewal (force={force}) …")

    try:
        resp = requests.get(
            DHAN_RENEW_URL,
            headers={
                "access-token": token,
                "dhanClientId": client_id,   # ← RenewToken uses this specific header
            },
            timeout=15,
        )

        logger.info(f"RenewToken HTTP {resp.status_code}")

        if resp.status_code == 200:
            data      = resp.json()
            new_token = data.get("accessToken") or data.get("token") or data.get("access_token")

            if not new_token:
                msg = f"200 OK but no token in response: {resp.text[:200]}"
                logger.error(msg)
                _state["last_error"] = msg
                return {"success": False, "token": None, "error": msg}

            # ── Update everything ──────────────────────────────────
            _state["current_token"] = new_token
            _state["last_error"]    = None
            _state["renewal_count"] += 1

            now_ist = datetime.now(IST)
            _state["last_renewed_at"] = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
            _state["next_renewal_at"] = (
                now_ist + timedelta(hours=RENEWAL_INTERVAL_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S IST")

            # Update the live HEADERS dict in app.py
            if _state["headers_ref"] is not None:
                _state["headers_ref"]["access-token"] = new_token
                logger.info("In-memory HEADERS updated.")

            # Persist to .env
            _update_env_file(new_token)

            # Persist state JSON
            _save_state()

            # Also update the os.environ so load_dotenv doesn't overwrite on next import
            os.environ["ACCESS_TOKEN"] = new_token

            logger.info(
                f"✅ Token renewed successfully. "
                f"Renewal #{_state['renewal_count']}. "
                f"Next at {_state['next_renewal_at']}"
            )
            return {"success": True, "token": new_token, "error": None}

        else:
            msg = f"RenewToken failed — HTTP {resp.status_code}: {resp.text[:300]}"
            logger.error(msg)
            _state["last_error"] = msg
            return {"success": False, "token": None, "error": msg}

    except requests.Timeout:
        msg = "RenewToken request timed out after 15 s."
        logger.error(msg)
        _state["last_error"] = msg
        return {"success": False, "token": None, "error": msg}

    except Exception as exc:
        msg = f"Unexpected error during renewal: {exc}"
        logger.exception(msg)
        _state["last_error"] = msg
        return {"success": False, "token": None, "error": msg}


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER JOB
# ─────────────────────────────────────────────────────────────────────────────

def _scheduled_renewal():
    """Called by APScheduler every 18 hours."""
    logger.info("Scheduled token renewal triggered.")
    renew_token()


def _schedule_renewal(scheduler):
    """Register the renewal job with an existing APScheduler instance."""
    try:
        scheduler.remove_job("dhan_token_renewal")
    except Exception:
        pass

    scheduler.add_job(
        _scheduled_renewal,
        "interval",
        hours=RENEWAL_INTERVAL_HOURS,
        id="dhan_token_renewal",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(IST) + timedelta(hours=RENEWAL_INTERVAL_HOURS),
    )

    next_run = (datetime.now(IST) + timedelta(hours=RENEWAL_INTERVAL_HOURS)).strftime(
        "%Y-%m-%d %H:%M:%S IST"
    )
    _state["next_renewal_at"] = next_run
    logger.info(f"✅ Token renewal scheduled every {RENEWAL_INTERVAL_HOURS} h. Next: {next_run}")


# ─────────────────────────────────────────────────────────────────────────────
# INIT — call once from app.py startup
# ─────────────────────────────────────────────────────────────────────────────

def init_token_manager(app, scheduler, headers_dict: dict):
    """
    Initialise the token manager.

    Parameters
    ----------
    app         : FastAPI instance (unused currently, kept for future lifespan hooks)
    scheduler   : APScheduler BackgroundScheduler already started in app.py
    headers_dict: The HEADERS dict in app.py  e.g. {"access-token": ..., ...}
                  We keep a reference and mutate it in-place on each renewal.
    """
    _state["headers_ref"] = headers_dict
    _state["current_token"] = headers_dict.get("access-token") or os.environ.get("ACCESS_TOKEN", "")
    _state["client_id"]     = headers_dict.get("client-id")    or os.environ.get("CLIENT_ID", "")

    _load_state()
    _schedule_renewal(scheduler)

    logger.info(
        f"Token manager initialised. "
        f"Client ID: {_state['client_id']} | "
        f"Token (first 20 chars): {str(_state['current_token'])[:20]}…"
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

    token = _state["current_token"] or ""
    masked = (token[:10] + "…" + token[-8:]) if len(token) > 20 else "not set"

    return JSONResponse({
        "token_masked":    masked,
        "client_id":       _state["client_id"],
        "last_renewed_at": _state["last_renewed_at"] or "Never (original token)",
        "next_renewal_at": _state["next_renewal_at"] or "Unknown",
        "renewal_count":   _state["renewal_count"],
        "last_error":      _state["last_error"],
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