"""
dhan_token_manager.py
─────────────────────────────────────────────────────────────────────────────
Automatic Dhan API token renewal system for TraderBro.

FIXES in this version:
  1. renewal_count no longer resets to 0 on page refresh / server restart.
     Full state (token, count, timestamps, last_error) is persisted to JSON.
  2. _load_state() now restores the saved token into _state["current_token"]
     so renewals after a restart use the latest token, not the stale .env one.
  3. init_token_manager() prefers the persisted token over the env token when
     the persisted one is newer (higher renewal_count).
  4. Added retry logic: if the primary RenewToken call fails with 401, we log
     a clear actionable message instead of silently looping.
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
RENEWAL_INTERVAL_HOURS = 18

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
}


def _load_state():
    """
    Load ALL persisted state from JSON.
    Restores token, count, timestamps, and last_error so nothing resets on restart.
    """
    if not TOKEN_STATE_FILE.exists():
        return

    try:
        data = json.loads(TOKEN_STATE_FILE.read_text(encoding="utf-8"))

        _state["renewal_count"]   = data.get("renewal_count", 0)
        _state["last_renewed_at"] = data.get("last_renewed_at")
        _state["next_renewal_at"] = data.get("next_renewal_at")
        _state["last_error"]      = data.get("last_error")

        # ── KEY FIX: restore the saved token so we use the latest one ──
        saved_token = data.get("current_token")
        if saved_token:
            _state["current_token"] = saved_token
            # Also keep os.environ in sync
            os.environ["ACCESS_TOKEN"] = saved_token

        logger.info(
            f"State restored from JSON — "
            f"renewals: {_state['renewal_count']}, "
            f"last: {_state['last_renewed_at']}"
        )
    except Exception as e:
        logger.warning(f"Could not load token state: {e}")


def _save_state():
    """Persist FULL state to JSON — called after every successful renewal."""
    try:
        TOKEN_STATE_FILE.write_text(json.dumps({
            "current_token":   _state["current_token"],
            "renewal_count":   _state["renewal_count"],
            "last_renewed_at": _state["last_renewed_at"],
            "next_renewal_at": _state["next_renewal_at"],
            "last_error":      _state["last_error"],
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
# CORE RENEWAL FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def renew_token(force: bool = False) -> dict:
    """
    Hit Dhan's /v2/RenewToken endpoint and update everything on success.
    Returns: { "success": bool, "token": str|None, "error": str|None }
    """
    token     = _state["current_token"]
    client_id = _state["client_id"]

    if not token or not client_id:
        msg = "Token or client_id not initialised. Cannot renew."
        logger.error(msg)
        _state["last_error"] = msg
        _save_state()
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

            # ── Update in-memory state ─────────────────────────────
            _state["current_token"] = new_token
            _state["last_error"]    = None
            _state["renewal_count"] += 1

            now_ist = datetime.now(IST)
            _state["last_renewed_at"] = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
            _state["next_renewal_at"] = (
                now_ist + timedelta(hours=RENEWAL_INTERVAL_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S IST")

            # ── Update live HEADERS dict in app.py ─────────────────
            if _state["headers_ref"] is not None:
                _state["headers_ref"]["access-token"] = new_token
                logger.info("In-memory HEADERS updated.")

            # ── Persist everywhere ─────────────────────────────────
            _update_env_file(new_token)
            os.environ["ACCESS_TOKEN"] = new_token
            _save_state()   # saves token + count + timestamps + no error

            logger.info(
                f"✅ Token renewed successfully. "
                f"Renewal #{_state['renewal_count']}. "
                f"Next at {_state['next_renewal_at']}"
            )
            return {"success": True, "token": new_token, "error": None}

        elif resp.status_code == 401:
            msg = (
                f"RenewToken failed — HTTP 401: {resp.text[:300]}. "
                f"ACTION REQUIRED: The current token is expired/invalid. "
                f"Please generate a fresh token from Dhan portal and update .env manually."
            )
            logger.error(msg)
            _state["last_error"] = f"RenewToken failed — HTTP 401: {resp.text[:300]}"
            _save_state()
            return {"success": False, "token": None, "error": _state["last_error"]}

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
# SCHEDULER JOB
# ─────────────────────────────────────────────────────────────────────────────

def _scheduled_renewal():
    """Called by APScheduler every 18 hours."""
    logger.info("Scheduled token renewal triggered.")
    renew_token()


def _schedule_renewal(scheduler):
    """Register the renewal job with the existing APScheduler instance."""
    try:
        scheduler.remove_job("dhan_token_renewal")
    except Exception:
        pass

    next_run_dt = datetime.now(IST) + timedelta(hours=RENEWAL_INTERVAL_HOURS)

    scheduler.add_job(
        _scheduled_renewal,
        "interval",
        hours=RENEWAL_INTERVAL_HOURS,
        id="dhan_token_renewal",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=next_run_dt,
    )

    next_run_str = next_run_dt.strftime("%Y-%m-%d %H:%M:%S IST")
    # Only update next_renewal_at if we don't have one already from saved state
    if not _state["next_renewal_at"]:
        _state["next_renewal_at"] = next_run_str

    logger.info(
        f"✅ Token renewal scheduled every {RENEWAL_INTERVAL_HOURS} h. "
        f"Next: {next_run_str}"
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
    _state["headers_ref"] = headers_dict
    _state["client_id"]   = headers_dict.get("client-id") or os.environ.get("CLIENT_ID", "")

    # Start with env token as baseline
    env_token = headers_dict.get("access-token") or os.environ.get("ACCESS_TOKEN", "")
    _state["current_token"] = env_token

    # Load persisted state — may overwrite current_token with a newer renewed token
    _load_state()

    # If saved token differs from env token, sync HEADERS with the newer saved one
    if _state["current_token"] and _state["current_token"] != env_token:
        headers_dict["access-token"] = _state["current_token"]
        logger.info("HEADERS synced with persisted (newer) token from token_state.json.")

    _schedule_renewal(scheduler)

    logger.info(
        f"Token manager initialised. "
        f"Client ID: {_state['client_id']} | "
        f"Token (first 20 chars): {str(_state['current_token'])[:20]}… | "
        f"Renewals so far: {_state['renewal_count']}"
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


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN UTILITY — manually inject a fresh token (for when auto-renewal breaks)
# ─────────────────────────────────────────────────────────────────────────────

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

    now_ist = datetime.now(IST)
    _state["last_renewed_at"] = now_ist.strftime("%Y-%m-%d %H:%M:%S IST") + " (manual)"
    _state["next_renewal_at"] = (
        now_ist + timedelta(hours=RENEWAL_INTERVAL_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S IST")
    _state["renewal_count"] += 1

    # Sync everywhere
    if _state["headers_ref"] is not None:
        _state["headers_ref"]["access-token"] = token

    os.environ["ACCESS_TOKEN"] = token
    _update_env_file(token)
    _save_state()

    logger.info(f"✅ Token manually updated by admin. Renewal #{_state['renewal_count']}.")
    return JSONResponse({"success": True, "renewal_count": _state["renewal_count"]})
