import os
import time
import hmac
import hashlib
import math
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, render_template_string, jsonify, request, session, redirect
from apscheduler.schedulers.background import BackgroundScheduler
from threading import Lock

load_dotenv()
API_KEY    = os.getenv("DELTA_API_KEY")
API_SECRET = os.getenv("DELTA_API_SECRET")
BASE_URL   = "https://api.delta.exchange"

# ── Currency & Config ───────────────────────────────────────────────────
USD_TO_INR     = 94.33
INTERVAL_FILE  = "crypto_config.json"
RUNNING_FILE   = "crypto_running.json"
ACTIVE_TIMEOUT = 35

# ── Global State ────────────────────────────────────────────────────────
LIVE_RUNNING_RECORDS: list = []
LAST_CHAIN_CACHE = {"data": None, "time": None}
ACTIVE_USERS: dict = {}
_lock = Lock()

app = Flask(__name__)
app.secret_key = "CryptoNexus@2026#Secure$Flask"
scheduler = BackgroundScheduler()
scheduler.start()

# ══════════════════════════════════════════════════════════════════════
# INTERVAL HELPERS
# ══════════════════════════════════════════════════════════════════════

def get_interval() -> int:
    try:
        with open(INTERVAL_FILE, "r") as f:
            return int(json.load(f).get("interval", 15))
    except:
        return 15

def set_interval(val: int):
    with open(INTERVAL_FILE, "w") as f:
        json.dump({"interval": val}, f)

# ══════════════════════════════════════════════════════════════════════
# DELTA EXCHANGE AUTH
# ══════════════════════════════════════════════════════════════════════

def get_auth_headers(method: str, path: str, query_string: str = "", payload: str = "") -> dict:
    """
    Delta Exchange signature format:
      signature = HMAC-SHA256(secret, method + timestamp + path + query_string + payload)
    query_string must NOT include the leading '?'
    """
    if not API_KEY or not API_SECRET:
        raise ValueError("DELTA_API_KEY or DELTA_API_SECRET missing in .env")
    timestamp = str(int(time.time()))
    # Strip leading '?' if caller passes it — signature must not include it
    qs_for_sig = query_string.lstrip("?")
    signature_data = method + timestamp + path + qs_for_sig + payload
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        signature_data.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return {
        "api-key":   API_KEY,
        "signature": signature,
        "timestamp": timestamp,
        "Accept":    "application/json",
        "Content-Type": "application/json",
    }

# ══════════════════════════════════════════════════════════════════════
# FETCH RAW CHAIN FROM DELTA EXCHANGE
# ══════════════════════════════════════════════════════════════════════

def fetch_raw_chain():
    """
    Returns (parsed_rows: list, index_inr: float).
    Handles all known Delta Exchange API quirks.
    """
    path         = "/v2/tickers"
    # query string WITHOUT leading '?' for the URL and signature
    qs_params    = "contract_types=call_options,put_options&underlying_asset_symbols=BTC"
    url          = f"{BASE_URL}{path}?{qs_params}"

    try:
        headers  = get_auth_headers("GET", path, qs_params)
        response = requests.get(url, headers=headers, timeout=12)
        print(f"[DELTA] tickers status: {response.status_code}")

        if response.status_code != 200:
            print(f"[DELTA] error body: {response.text[:300]}")
            return [], 0.0

        body = response.json()
        # Delta returns: {success: true, result: [...], meta: {...}}
        if not body.get("success", False):
            print(f"[DELTA] API success=false: {body}")
            return [], 0.0

        data = body.get("result") or []
        if not data:
            print("[DELTA] Empty result array")
            return [], 0.0

    except Exception as e:
        print(f"[DELTA] fetch exception: {e}")
        return [], 0.0

    def safe_float(v):
        """Convert value to float, handling None, 'null', NaN, inf."""
        try:
            if v is None or v == "null" or v == "":
                return None
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except:
            return None

    parsed = []
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    index_usd = 0.0

    for item in data:
        symbol = item.get("symbol", "")
        parts  = symbol.split("-")

        # Delta BTC option symbol: "C-BTC-105000-270625"
        # parts[0] = C/P, parts[1] = BTC, parts[2] = strike, parts[3] = expiry DDMMYY
        if len(parts) != 4:
            continue

        contract_type = parts[0]  # 'C' or 'P'
        if contract_type not in ("C", "P"):
            continue

        try:
            strike = int(float(parts[2]))
        except:
            continue

        try:
            expiry = datetime.strptime(parts[3], "%d%m%y").strftime("%Y-%m-%d")
        except:
            expiry = parts[3]

        # underlying_price — try multiple field names Delta uses
        up = safe_float(item.get("underlying_price")) or \
             safe_float(item.get("spot_price"))        or \
             safe_float(item.get("index_price"))       or 0.0

        if up > 0 and index_usd == 0.0:
            index_usd = up

        mark = safe_float(item.get("mark_price")) or 0.0

        # greeks — null for deep OTM, handle gracefully
        greeks = item.get("greeks") or {}
        if isinstance(greeks, str):
            try:
                greeks = json.loads(greeks)
            except:
                greeks = {}

        parsed.append({
            "DateTime":  current_time,
            "Type":      contract_type,
            "Expiry":    expiry,
            "Strike":    strike,
            "IndexUSD":  up,
            "PriceUSD":  mark,
            "Delta":     safe_float(greeks.get("delta")),
            "Gamma":     safe_float(greeks.get("gamma")),
            "Theta":     safe_float(greeks.get("theta")),
            "Vega":      safe_float(greeks.get("vega")),
        })

    # If underlying_price was 0 everywhere, try to get from a spot ticker
    if index_usd == 0.0:
        try:
            r2 = requests.get(
                f"{BASE_URL}/v2/tickers/BTCUSD",
                headers=get_auth_headers("GET", "/v2/tickers/BTCUSD"),
                timeout=8
            )
            if r2.status_code == 200:
                b2 = r2.json()
                spot = b2.get("result", {})
                index_usd = safe_float(spot.get("spot_price")) or \
                            safe_float(spot.get("mark_price")) or 0.0
                print(f"[DELTA] spot fallback: {index_usd}")
        except:
            pass

    print(f"[DELTA] parsed {len(parsed)} rows, index_usd={index_usd:.2f}")
    return parsed, index_usd * USD_TO_INR


# ══════════════════════════════════════════════════════════════════════
# BUILD CHAIN WITH FORMULAS (mirrors dhan build_df_from_oc exactly)
# ══════════════════════════════════════════════════════════════════════

def build_chain_df(raw_rows: list, index_inr: float) -> pd.DataFrame:
    if not raw_rows:
        return pd.DataFrame()

    df = pd.DataFrame(raw_rows)

    calls = df[df["Type"] == "C"].copy()
    puts  = df[df["Type"] == "P"].copy()

    if calls.empty or puts.empty:
        print(f"[BUILD] calls={len(calls)} puts={len(puts)} — skipping")
        return pd.DataFrame()

    calls = calls.rename(columns={
        "PriceUSD": "CE_LTP", "Delta": "CE_Delta",
        "Gamma": "CE_Gamma", "Theta": "CE_Theta", "Vega": "CE_Vega",
    })
    puts = puts.rename(columns={
        "PriceUSD": "PE_LTP", "Delta": "PE_Delta",
        "Gamma": "PE_Gamma", "Theta": "PE_Theta", "Vega": "PE_Vega",
    })

    chain = pd.merge(
        calls[["DateTime", "Expiry", "Strike", "IndexUSD",
               "CE_LTP", "CE_Delta", "CE_Gamma", "CE_Theta", "CE_Vega"]],
        puts[["Expiry", "Strike",
              "PE_LTP", "PE_Delta", "PE_Gamma", "PE_Theta", "PE_Vega"]],
        on=["Expiry", "Strike"], how="outer"
    ).sort_values("Strike").reset_index(drop=True)

    # Fill NaN for LTP columns only (not greeks — keep None for formula guards)
    for col in ["CE_LTP", "PE_LTP"]:
        chain[col] = chain[col].fillna(0)

    # Convert prices USD → INR
    for col in ["CE_LTP", "PE_LTP"]:
        chain[col] = chain[col].apply(lambda v: round(float(v) * USD_TO_INR, 2))

    if chain.empty:
        return chain

    # ── ATM window ±10 strikes ────────────────────────────────────────
    index_usd = index_inr / USD_TO_INR
    chain["_diff"] = abs(chain["Strike"] - index_usd)
    atm_idx = chain["_diff"].idxmin()
    chain = chain.iloc[max(atm_idx - 5, 0): atm_idx + 6].reset_index(drop=True)
    chain["_diff"] = abs(chain["Strike"] - index_usd)
    atm_idx = chain["_diff"].idxmin()

    # ── Delta Ratio ───────────────────────────────────────────────────
    def safe_ratio(row):
        try:
            ce = row["CE_Delta"]
            pe = row["PE_Delta"]
            if ce is None or pe is None:
                return None
            ce, pe = float(ce), float(pe)
            if ce == 0 or math.isnan(ce) or math.isinf(ce):
                return None
            r = (pe / ce) * -1
            return None if (math.isnan(r) or math.isinf(r)) else round(r, 5)
        except:
            return None

    chain["Delta_Ratio"] = chain.apply(safe_ratio, axis=1)

    # ── Reference ────────────────────────────────────────────────────
    chain["Reference"] = None
    for i in range(1, len(chain) - 1):
        try:
            prev = chain.loc[i - 1, "Delta_Ratio"]
            nxt  = chain.loc[i + 1, "Delta_Ratio"]
            if prev is not None and nxt is not None:
                ref = ((float(prev) + float(nxt)) / 2) - 0.06
                if not (math.isnan(ref) or math.isinf(ref)):
                    chain.loc[i, "Reference"] = round(ref, 5)
        except:
            continue

    # ── Stretched ────────────────────────────────────────────────────
    chain["Stretched"] = None
    for i in range(2, len(chain)):
        try:
            curr_dr  = chain.loc[i,     "Delta_Ratio"]
            curr_ref = chain.loc[i,     "Reference"]
            prev1    = chain.loc[i - 1, "Delta_Ratio"]
            prev2    = chain.loc[i - 2, "Delta_Ratio"]
            if any(v is None for v in [curr_dr, curr_ref, prev1, prev2]):
                continue
            curr_dr  = float(curr_dr)
            curr_ref = float(curr_ref)
            prev1    = float(prev1)
            prev2    = float(prev2)
            denom = (prev1 - prev2) / 100
            if denom == 0:
                continue
            # Strike is in USD → convert result to INR
            stretched_usd = chain.loc[i, "Strike"] - ((curr_dr - curr_ref) / denom)
            stretched_inr = stretched_usd * USD_TO_INR
            if math.isnan(stretched_inr) or math.isinf(stretched_inr):
                continue
            chain.loc[i, "Stretched"] = round(stretched_inr, 5)
        except:
            continue

    # ── Difference ───────────────────────────────────────────────────
    def calc_diff(s):
        try:
            if s is None:
                return None
            f = float(s)
            return None if (math.isnan(f) or math.isinf(f)) else round(f - index_inr, 2)
        except:
            return None

    chain["Difference"] = chain["Stretched"].apply(calc_diff)
    chain["Index_LTP"]  = index_inr
    chain["ATM_Strike"] = int(chain.loc[atm_idx, "Strike"])

    chain.drop(columns=["_diff", "IndexUSD"], inplace=True, errors="ignore")
    return chain


def get_live_chain(force_live: bool = False):
    """Returns (chain_df, index_inr, atm_strike) with caching."""
    global LAST_CHAIN_CACHE
    now       = datetime.now()
    interval  = get_interval()
    cached    = LAST_CHAIN_CACHE.get("data")
    cached_t  = LAST_CHAIN_CACHE.get("time")

    if (not force_live and cached is not None and cached_t is not None
            and (now - cached_t).total_seconds() < interval):
        return cached

    try:
        raw_rows, index_inr = fetch_raw_chain()
        if not raw_rows or index_inr == 0:
            return cached if cached else (pd.DataFrame(), 0.0, None)

        chain = build_chain_df(raw_rows, index_inr)
        if chain is None or chain.empty:
            return cached if cached else (pd.DataFrame(), index_inr, None)

        # Determine ATM from ATM_Strike column
        atm_strike = int(chain["ATM_Strike"].iloc[0]) if "ATM_Strike" in chain.columns else None
        chain.drop(columns=["ATM_Strike"], inplace=True, errors="ignore")

        result = (chain, index_inr, atm_strike)
        LAST_CHAIN_CACHE["data"] = result
        LAST_CHAIN_CACHE["time"] = now
        return result

    except Exception as e:
        print(f"[get_live_chain] error: {e}")
        import traceback; traceback.print_exc()
        return cached if cached else (pd.DataFrame(), 0.0, None)


# ══════════════════════════════════════════════════════════════════════
# AUTO RECORDER  (mirrors dhan auto_market_recorder exactly)
# ══════════════════════════════════════════════════════════════════════

def crypto_auto_recorder():
    global LIVE_RUNNING_RECORDS
    try:
        chain, index_inr, atm_strike = get_live_chain(force_live=True)
        if chain is None or chain.empty or atm_strike is None:
            print("[RECORDER] No chain data — skipping")
            return

        atm_row = chain[chain["Strike"] == atm_strike]
        if atm_row.empty:
            print(f"[RECORDER] ATM strike {atm_strike} not found in chain")
            return

        r = atm_row.iloc[0]
        current_diff = r.get("Difference") if hasattr(r, 'get') else r["Difference"]
        if current_diff is None or (isinstance(current_diff, float) and math.isnan(current_diff)):
            current_diff = 0.0

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        row = {
            "datetime":    now_str,
            "expiry":      str(r.get("Expiry", "") if hasattr(r, 'get') else r["Expiry"]),
            "ce_ltp":      round(float(r["CE_LTP"] or 0), 2),
            "ce_delta":    _safe_val(r["CE_Delta"]),
            "ce_gamma":    _safe_val(r["CE_Gamma"]),
            "ce_theta":    _safe_val(r["CE_Theta"]),
            "ce_vega":     _safe_val(r["CE_Vega"]),
            "strike":      int(r["Strike"]),
            "pe_ltp":      round(float(r["PE_LTP"] or 0), 2),
            "pe_delta":    _safe_val(r["PE_Delta"]),
            "pe_gamma":    _safe_val(r["PE_Gamma"]),
            "pe_theta":    _safe_val(r["PE_Theta"]),
            "pe_vega":     _safe_val(r["PE_Vega"]),
            "delta_ratio": _safe_val(r["Delta_Ratio"]),
            "index_ltp":   round(float(index_inr), 2),
            "reference":   _safe_val(r["Reference"]),
            "stretched":   _safe_val(r["Stretched"]),
            "difference":  round(float(current_diff), 2),
            "diff_prev":   0.0,
            "running":     0.0,
        }

        with _lock:
            if len(LIVE_RUNNING_RECORDS) > 0:
                prev = LIVE_RUNNING_RECORDS[-1]
                if (prev["datetime"] == row["datetime"]
                        and prev["difference"] == row["difference"]):
                    print("⚠️ Duplicate row — skipped")
                    return
                diff_change      = float(current_diff) - float(prev["difference"])
                row["diff_prev"] = round(diff_change, 2)
                row["running"]   = round(float(prev.get("running", 0)) + diff_change, 2)

            LIVE_RUNNING_RECORDS.append(row)
            LIVE_RUNNING_RECORDS = LIVE_RUNNING_RECORDS[-2000:]

        with open(RUNNING_FILE, "w") as f:
            json.dump(LIVE_RUNNING_RECORDS, f)

        print(f"✅ SAVED {now_str}  ATM={atm_strike}  ₹{index_inr:.0f}  Running={row['running']:.2f}")

    except Exception as e:
        print(f"[RECORDER] ERROR: {e}")
        import traceback; traceback.print_exc()


def _safe_val(v):
    """Convert pandas/numpy value to plain Python float or None."""
    try:
        if v is None:
            return None
        import numpy as np
        if isinstance(v, (float, int)):
            return None if (math.isnan(v) or math.isinf(v)) else round(float(v), 6)
        if isinstance(v, np.floating):
            return None if (math.isnan(float(v)) or math.isinf(float(v))) else round(float(v), 6)
        return float(v)
    except:
        return None


def restart_recorder_job():
    try:
        scheduler.remove_job("crypto_recorder_job")
    except:
        pass
    interval = get_interval()
    scheduler.add_job(
        crypto_auto_recorder,
        "interval",
        seconds=interval,
        id="crypto_recorder_job",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    print(f"✅ CRYPTO RECORDER ({interval}s interval)")

restart_recorder_job()


def daily_cleanup():
    global LIVE_RUNNING_RECORDS
    now = datetime.now()
    if not (0 <= now.hour < 2):
        return
    with _lock:
        LIVE_RUNNING_RECORDS = []
    if os.path.exists(RUNNING_FILE):
        os.remove(RUNNING_FILE)
    print(f"🧹 DAILY CLEANUP {now.strftime('%Y-%m-%d %H:%M:%S')}")

scheduler.add_job(daily_cleanup, "cron", hour=0, minute=1,
                  id="crypto_cleanup", replace_existing=True,
                  max_instances=1, coalesce=True)


def load_existing_records():
    global LIVE_RUNNING_RECORDS
    if os.path.exists(RUNNING_FILE):
        try:
            with open(RUNNING_FILE, "r") as f:
                LIVE_RUNNING_RECORDS = json.load(f)
            print(f"✅ Reloaded {len(LIVE_RUNNING_RECORDS)} records")
        except:
            LIVE_RUNNING_RECORDS = []

load_existing_records()


# ══════════════════════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    data = request.get_json(silent=True) or {}
    page = data.get("page", "unknown")
    user = session.get("user", f"guest_{request.remote_addr}")
    ACTIVE_USERS[f"{user}::{page}"] = {"username": user, "page": page, "ts": time.time()}
    return jsonify({"ok": True})


@app.route("/api/active-users", methods=["GET"])
def active_users_api():
    now   = time.time()
    stale = [k for k, v in ACTIVE_USERS.items() if now - v["ts"] > ACTIVE_TIMEOUT]
    for k in stale:
        del ACTIVE_USERS[k]
    recorder_users = {v["username"] for v in ACTIVE_USERS.values() if v["page"] == "recorder"}
    return jsonify({"total": len(recorder_users)})


@app.route("/api/set-interval", methods=["POST"])
def api_set_interval():
    data     = request.get_json(silent=True) or {}
    interval = max(1, int(data.get("interval", 15)))
    set_interval(interval)
    restart_recorder_job()
    return jsonify({"success": True, "interval": interval})


@app.route("/api/get-interval", methods=["GET"])
def api_get_interval():
    return jsonify({"interval": get_interval()})


@app.route("/api/chain", methods=["GET"])
def api_chain():
    chain, index_inr, atm_strike = get_live_chain()

    if chain is None or chain.empty:
        return jsonify({"error": "No data", "ltp_inr": 0, "chain": [], "atm_strike": 0})

    def safe(v):
        try:
            if v is None:
                return None
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except:
            return None

    rows = []
    for _, r in chain.iterrows():
        rows.append({
            "DateTime": str(r.get("DateTime", "")),
            "Expiry":   str(r.get("Expiry", "")),
            "CE":       safe(r.get("CE_LTP")),
            "CEΔ":      safe(r.get("CE_Delta")),
            "CEΓ":      safe(r.get("CE_Gamma")),
            "CEΘ":      safe(r.get("CE_Theta")),
            "CEV":      safe(r.get("CE_Vega")),
            "Strike":   int(r["Strike"]),
            "PE":       safe(r.get("PE_LTP")),
            "PEΔ":      safe(r.get("PE_Delta")),
            "PEΓ":      safe(r.get("PE_Gamma")),
            "PEΘ":      safe(r.get("PE_Theta")),
            "PEV":      safe(r.get("PE_Vega")),
            "Ratio":    safe(r.get("Delta_Ratio")),
            "Index":    round(float(index_inr), 2),
            "Ref":      safe(r.get("Reference")),
            "Stretch":  safe(r.get("Stretched")),
            "Diff":     safe(r.get("Difference")),
            "is_atm":   int(r["Strike"]) == atm_strike,
        })

    return jsonify({
        "chain":     rows,
        "atm_strike": atm_strike,
        "ltp_inr":   round(float(index_inr), 2),
        "timestamp": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
    })


@app.route("/api/get-running", methods=["GET"])
def api_get_running():
    try:
        if os.path.exists(RUNNING_FILE):
            with open(RUNNING_FILE, "r") as f:
                rows = json.load(f)
            return jsonify({"rows": rows[-2000:]})
        return jsonify({"rows": []})
    except Exception as e:
        return jsonify({"rows": [], "error": str(e)})


@app.route("/api/live-data", methods=["GET"])
def api_live_data():
    return api_get_running()


@app.route("/api/clear-running", methods=["POST"])
def api_clear_running():
    global LIVE_RUNNING_RECORDS
    with _lock:
        LIVE_RUNNING_RECORDS = []
    if os.path.exists(RUNNING_FILE):
        os.remove(RUNNING_FILE)
    return jsonify({"status": "cleared"})


@app.route("/api/expiries", methods=["GET"])
def api_expiries():
    chain, _, _ = get_live_chain()
    if chain is None or chain.empty:
        return jsonify([])
    expiries = sorted(chain["Expiry"].dropna().unique().tolist())
    return jsonify(expiries)


# ── Debug endpoint — shows raw API response for troubleshooting ──────
@app.route("/api/debug-raw", methods=["GET"])
def api_debug_raw():
    """Call this in browser to see exactly what Delta Exchange returns."""
    path      = "/v2/tickers"
    qs_params = "contract_types=call_options,put_options&underlying_asset_symbols=BTC"
    url       = f"{BASE_URL}{path}?{qs_params}"
    try:
        headers  = get_auth_headers("GET", path, qs_params)
        response = requests.get(url, headers=headers, timeout=12)
        body     = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
        result   = body.get("result", []) if isinstance(body, dict) else []
        sample   = result[:3] if result else []
        return jsonify({
            "status_code":    response.status_code,
            "success":        body.get("success") if isinstance(body, dict) else None,
            "total_items":    len(result),
            "sample_items":   sample,
            "auth_key_prefix": API_KEY[:6] + "..." if API_KEY else "MISSING",
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ══════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def index():
    return render_template_string(ADMIN_TEMPLATE)

@app.route("/recorder", methods=["GET"])
def recorder_page():
    return redirect("/simple.html")

@app.route("/simple.html", methods=["GET"])
def simple_page():
    try:
        with open("simple.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error loading simple.html: {str(e)}", 404

@app.route("/dashboard.html", methods=["GET"])
def dashboard_page():
    try:
        with open("dashboard.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error loading dashboard.html: {str(e)}", 404


# ══════════════════════════════════════════════════════════════════════
# ADMIN PAGE TEMPLATE
# ══════════════════════════════════════════════════════════════════════

ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Crypto-Nexus-Engine — Admin</title>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --call: #34d399; --put: #f87171; --strike: #60a5fa;
      --atm: #fbbf24;  --dim: #94a3b8; --main: #f8fafc;
    }
    *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      background: #030712; color: var(--main);
      padding: 24px; min-height: 100vh; overflow-x: hidden;
      font-variant-numeric: tabular-nums;
    }
    .orb { position:fixed; border-radius:50%; z-index:-1; filter:blur(100px); pointer-events:none; }
    .orb-1 { top:-10%; left:-5%; width:50vw; height:50vw; background:radial-gradient(circle,rgba(59,130,246,0.4) 0%,transparent 60%); animation:drift 14s ease-in-out infinite alternate; }
    .orb-2 { bottom:-20%; right:-10%; width:60vw; height:60vw; background:radial-gradient(circle,rgba(139,92,246,0.3) 0%,transparent 60%); filter:blur(120px); animation:drift 18s ease-in-out infinite alternate-reverse; }
    .orb-3 { top:40%; left:30%; width:40vw; height:40vw; background:radial-gradient(circle,rgba(16,185,129,0.2) 0%,transparent 60%); animation:drift 22s ease-in-out infinite alternate; }
    @keyframes drift { 0%{transform:translate(0,0) scale(1);} 100%{transform:translate(-40px,40px) scale(1.1);} }

    .glass { background:rgba(10,15,30,0.25); backdrop-filter:blur(35px); -webkit-backdrop-filter:blur(35px); border:1px solid rgba(255,255,255,0.09); border-top-color:rgba(255,255,255,0.14); border-radius:22px; box-shadow:0 20px 45px rgba(0,0,0,0.3); }

    .brand { text-align:center; margin-bottom:24px; font-size:28px; font-weight:800; letter-spacing:-1px; }
    .brand span { font-size:14px; font-weight:500; color:var(--strike); margin-left:8px; }

    .nav-strip { display:flex; justify-content:center; gap:12px; margin-bottom:24px; }
    .nav-btn { padding:10px 22px; border-radius:12px; font-weight:700; font-size:13px; cursor:pointer; border:1px solid rgba(255,255,255,0.08); transition:.25s; text-decoration:none; display:inline-flex; align-items:center; gap:8px; }
    .nav-active { background:rgba(59,130,246,0.2); color:#60a5fa; border-color:rgba(96,165,250,0.3); }
    .nav-rec { background:rgba(16,185,129,0.1); color:#34d399; }
    .nav-rec:hover { background:rgba(16,185,129,0.25); }
    .nav-debug { background:rgba(251,191,36,0.1); color:#fbbf24; font-size:11px; }

    .ctrl { padding:22px 24px; margin-bottom:20px; }
    .ctrl-row { display:flex; gap:14px; align-items:flex-end; flex-wrap:wrap; }
    .fgroup { display:flex; flex-direction:column; }
    .fgroup label { font-size:11px; font-weight:700; color:var(--dim); text-transform:uppercase; letter-spacing:1px; margin-bottom:8px; }
    select, input[type=number] {
      background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.09);
      color:#fff; padding:10px 14px; border-radius:11px; font-family:'Plus Jakarta Sans',sans-serif;
      font-size:13px; font-weight:600; outline:none; min-width:140px; transition:.25s;
    }
    select:focus, input[type=number]:focus { border-color:var(--strike); }
    select option { background:#0f172a; }
    .btn { padding:10px 22px; border-radius:11px; font-family:'Plus Jakarta Sans',sans-serif; font-weight:700; font-size:13px; cursor:pointer; border:1px solid rgba(255,255,255,0.06); transition:.25s; }
    .btn:hover { transform:translateY(-1px); }
    .btn-blue { background:rgba(59,130,246,0.15); color:#60a5fa; }
    .btn-blue:hover { background:rgba(59,130,246,0.3); border-color:#60a5fa; }
    .btn-red  { background:rgba(239,68,68,0.1); color:#f87171; }
    .btn-red:hover  { background:rgba(239,68,68,0.25); border-color:#f87171; }
    .btn-green { background:rgba(16,185,129,0.1); color:#34d399; }
    .btn-green:hover { background:rgba(16,185,129,0.25); border-color:#34d399; }

    .status-row { margin-top:20px; padding-top:16px; border-top:1px solid rgba(255,255,255,0.05); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px; }
    .sl { display:flex; align-items:center; gap:12px; }
    .badge-live { background:rgba(52,211,153,0.1); color:var(--call); border:1px solid rgba(52,211,153,0.25); padding:5px 14px; border-radius:20px; font-size:11px; font-weight:800; text-transform:uppercase; letter-spacing:1px; }
    .pill { display:inline-flex; align-items:center; gap:6px; padding:5px 13px; border-radius:20px; font-size:11px; font-weight:800; background:rgba(251,191,36,0.08); border:1px solid rgba(251,191,36,0.2); color:#fbbf24; }
    .dot { width:7px; height:7px; border-radius:50%; background:#fbbf24; animation:pulse 1.5s infinite; }
    @keyframes pulse { 0%,100%{opacity:1;transform:scale(1);} 50%{opacity:.4;transform:scale(0.75);} }
    .sr { display:flex; gap:32px; }
    .lbox span { font-size:10px; font-weight:700; color:var(--dim); text-transform:uppercase; letter-spacing:1px; }
    .lbox h3 { margin:4px 0 0; font-size:22px; font-weight:800; }

    .tbl-wrap { overflow-x:auto; border-radius:20px; }
    table { width:100%; border-collapse:separate; border-spacing:0; text-align:center; font-size:12px; font-family:'JetBrains Mono',monospace; background:transparent; min-width:1200px; }
    th { padding:18px 8px; font-family:'Plus Jakarta Sans',sans-serif; font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; border-bottom:1px solid rgba(255,255,255,0.09); background:rgba(0,0,0,0.15); white-space:nowrap; position:sticky; top:0; z-index:5; backdrop-filter:blur(10px); }
    .thg { color:var(--call); } .thr { color:var(--put); } .thb { color:var(--strike); } .thd { color:var(--dim); }
    td { padding:15px 8px; border-bottom:1px solid rgba(255,255,255,0.025); color:#cbd5e1; white-space:nowrap; background:transparent; transition:.15s; }
    tr:hover td { background:rgba(255,255,255,0.02); color:#fff; }
    .ce  { color:var(--call); font-weight:700; font-size:13px; }
    .pe  { color:var(--put);  font-weight:700; font-size:13px; }
    .sk  { font-weight:800; font-size:13px; }
    .si  { background:rgba(96,165,250,0.07); color:var(--strike); border:1px solid rgba(96,165,250,0.18); padding:6px 14px; border-radius:9px; display:inline-block; }
    .atm-row td { background:linear-gradient(90deg,rgba(251,191,36,0.03),rgba(251,191,36,0.1),rgba(251,191,36,0.03)) !important; border-top:1px solid rgba(251,191,36,0.15); border-bottom:1px solid rgba(251,191,36,0.15); color:#fef3c7; }
    .atm-row .si { background:rgba(251,191,36,0.1); color:var(--atm); border-color:rgba(251,191,36,0.3); box-shadow:0 0 16px rgba(251,191,36,0.15); }
    .vp { color:#34d399; } .vn { color:#f87171; } .vz { color:#94a3b8; }

    #err-banner { display:none; background:rgba(239,68,68,0.12); border:1px solid rgba(239,68,68,0.3); border-radius:12px; padding:14px 20px; margin-bottom:16px; color:#fca5a5; font-size:13px; }
  </style>
</head>
<body>
  <div class="orb orb-1"></div>
  <div class="orb orb-2"></div>
  <div class="orb orb-3"></div>

  <div class="brand">Crypto-Nexus-Engine <span>by Traderbro</span></div>

  <div class="nav-strip">
    <span class="nav-btn nav-active">⚙ Option Chain</span>
    <a href="/simple.html" class="nav-btn nav-rec">📊 Recorder</a>
    <a href="/dashboard.html" class="nav-btn nav-rec" style="background:rgba(96,165,250,0.1); color:#60a5fa;">📈 Dashboard</a>
    <a href="/api/debug-raw" target="_blank" class="nav-btn nav-debug">🔍 Debug API</a>
  </div>

  <div id="err-banner"></div>

  <div class="glass ctrl" style="margin-bottom:18px;">
    <div class="ctrl-row">
      <div class="fgroup">
        <label>Expiry</label>
        <select id="sel-expiry" onchange="renderTable()"><option value="">All</option></select>
      </div>
      <div class="fgroup">
        <label>Interval (s)</label>
        <input type="number" id="inp-interval" value="15" min="1" style="min-width:80px;">
      </div>
      <button class="btn btn-blue"  onclick="applyInterval()">⚡ Apply</button>
      <button class="btn btn-red"   onclick="clearRecorder()">🗑 Clear Recorder</button>
      <a href="/simple.html" class="btn btn-green">📊 Open Recorder</a>
    </div>
    <div class="status-row">
      <div class="sl">
        <span class="badge-live" id="live-badge">● LIVE</span>
        <span id="last-updated" style="color:var(--dim); font-size:12px;">Connecting…</span>
        <span class="pill"><span class="dot"></span><span id="active-count">0</span> online</span>
      </div>
      <div class="sr">
        <div class="lbox"><span>Expiry</span><h3 id="expiry-lbl" style="color:#a78bfa; font-size:15px;">—</h3></div>
        <div class="lbox"><span>LTP</span><h3 id="ltp-val" style="color:#60a5fa;">—</h3></div>
        <div class="lbox"><span>Strike Price</span><h3 id="strike-val" style="color:#fbbf24;">—</h3></div>
      </div>
    </div>
  </div>

  <div class="glass tbl-wrap">
    <table>
      <thead>
        <tr>
          <th class="thd">DateTime</th>
          <th class="thd">Expiry</th>
          <th class="thg">CE ₹</th>
          <th class="thg">Δ</th><th class="thg">Γ</th><th class="thg">Θ</th><th class="thg">V</th>
          <th class="thb">Strike</th>
          <th class="thr">PE ₹</th>
          <th class="thr">Δ</th><th class="thr">Γ</th><th class="thr">Θ</th><th class="thr">V</th>
          <th class="thd">Ratio</th>
          <th class="thd">Index ₹</th>
          <th class="thd">Reference</th>
          <th class="thd">Stretched</th>
          <th class="thd">Difference</th>
        </tr>
      </thead>
      <tbody id="tbl-body">
        <tr><td colspan="18" style="padding:50px; color:#94a3b8;">Connecting to Delta Exchange…</td></tr>
      </tbody>
    </table>
  </div>

<script>
let rows = [], atmStrike = 0, ltpInr = 0, refreshId = null, intervalSec = 15;

const fmt = (v, d=2) => (v===null||v===undefined) ? "—" : isNaN(parseFloat(v)) ? "—" : parseFloat(v).toFixed(d);
const vcls = v => { const n=parseFloat(v); return isNaN(n) ? "vz" : n>0 ? "vp" : n<0 ? "vn" : "vz"; };

function showError(msg) {
  const b = document.getElementById("err-banner");
  b.style.display = "block";
  b.innerHTML = `⚠️ ${msg} — <a href="/api/debug-raw" target="_blank" style="color:#fbbf24;">Check Debug API</a>`;
}
function hideError() { document.getElementById("err-banner").style.display = "none"; }

async function loadInterval() {
  try {
    const d = await (await fetch("/api/get-interval")).json();
    if (d.interval) { intervalSec = d.interval; document.getElementById("inp-interval").value = d.interval; }
  } catch(e) {}
}

async function applyInterval() {
  const v = parseInt(document.getElementById("inp-interval").value);
  if (!v || v < 1) return;
  await fetch("/api/set-interval",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({interval:v})});
  intervalSec = v; scheduleNext();
}

async function clearRecorder() {
  if (!confirm("Clear all recorder data?")) return;
  await fetch("/api/clear-running",{method:"POST"});
}

function populateExpiries() {
  const sel = document.getElementById("sel-expiry");
  const cur = sel.value;
  const exps = [...new Set(rows.map(r=>r.Expiry))].filter(Boolean).sort();
  sel.innerHTML = '<option value="">All</option>' + exps.map(e=>`<option value="${e}">${e}</option>`).join("");
  if (cur && exps.includes(cur)) sel.value = cur;
  document.getElementById("expiry-lbl").innerText = sel.value || exps[0] || "—";
}

function renderTable() {
  const sel = document.getElementById("sel-expiry").value;
  const filtered = sel ? rows.filter(r => r.Expiry === sel) : rows;
  const tbody = document.getElementById("tbl-body");
  tbody.innerHTML = "";
  filtered.forEach(r => {
    const tr = document.createElement("tr");
    if (r.is_atm) tr.className = "atm-row";
    tr.innerHTML = `
      <td style="font-size:10px;color:#94a3b8;">${r.DateTime||"—"}</td>
      <td style="font-size:10px;color:#a78bfa;">${r.Expiry||"—"}</td>
      <td class="ce">${fmt(r.CE)}</td>
      <td>${fmt(r["CEΔ"],4)}</td><td>${fmt(r["CEΓ"],5)}</td><td>${fmt(r["CEΘ"],2)}</td><td>${fmt(r["CEV"],2)}</td>
      <td class="sk"><div class="si">${r.Strike}</div></td>
      <td class="pe">${fmt(r.PE)}</td>
      <td>${fmt(r["PEΔ"],4)}</td><td>${fmt(r["PEΓ"],5)}</td><td>${fmt(r["PEΘ"],2)}</td><td>${fmt(r["PEV"],2)}</td>
      <td class="${vcls(r.Ratio)}">${fmt(r.Ratio,5)}</td>
      <td style="color:#60a5fa;">${fmt(r.Index,2)}</td>
      <td>${fmt(r.Ref,5)}</td>
      <td class="${vcls(r.Stretch)}">${fmt(r.Stretch,5)}</td>
      <td class="${vcls(r.Diff)}">${fmt(r.Diff,2)}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function fetchChain() {
  try {
    const data = await (await fetch("/api/chain")).json();
    if (data.error || !data.chain || !data.chain.length) {
      showError(data.error || "No data returned from Delta Exchange");
      return;
    }
    hideError();
    rows = data.chain; atmStrike = data.atm_strike; ltpInr = data.ltp_inr;
    populateExpiries(); renderTable();
    document.getElementById("ltp-val").innerText = ltpInr > 0 ? `₹${ltpInr.toLocaleString("en-IN",{maximumFractionDigits:0})}` : "—";
    document.getElementById("strike-val").innerText = atmStrike ? atmStrike : "—";
    document.getElementById("last-updated").innerText = "Updated " + new Date().toLocaleTimeString();
  } catch(e) { showError(e.message); }
}

async function fetchUsers() {
  try { document.getElementById("active-count").innerText = ((await (await fetch("/api/active-users")).json()).total)||0; } catch(e){}
}

function scheduleNext() { if(refreshId) clearTimeout(refreshId); refreshId = setTimeout(loop, intervalSec*1000); }
async function loop() { await fetchChain(); scheduleNext(); }

async function hb() { await fetch("/api/heartbeat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({page:"admin"})}).catch(()=>{}); }

window.addEventListener("load", async () => {
  await loadInterval(); loop(); fetchUsers();
  setInterval(fetchUsers, 20000); hb(); setInterval(hb, 20000);
});
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════
# RECORDER PAGE TEMPLATE
# ══════════════════════════════════════════════════════════════════════

# RECORDER_TEMPLATE = """
# <!DOCTYPE html>
# <html lang="en">
# <head>
#   <meta charset="UTF-8">
#   <meta name="viewport" content="width=device-width, initial-scale=1.0">
#   <title>Crypto-Nexus — Live Recorder</title>
#   <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
#   <script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
#   <style>
#     :root { --call:#34d399; --put:#f87171; --strike:#60a5fa; --amber:#fbbf24; --dim:#94a3b8; }
#     *, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
#     body { font-family:'Plus Jakarta Sans',sans-serif; background:#030712; color:#f8fafc; min-height:100vh; padding:22px; overflow-x:hidden; position:relative; }
#     .orb { position:fixed; border-radius:50%; z-index:-1; filter:blur(100px); pointer-events:none; }
#     .orb-1 { top:-8%; left:-4%; width:55vw; height:55vw; background:radial-gradient(circle,rgba(59,130,246,0.35) 0%,transparent 65%); animation:drift 16s ease-in-out infinite alternate; }
#     .orb-2 { bottom:-18%; right:-8%; width:60vw; height:60vw; background:radial-gradient(circle,rgba(139,92,246,0.28) 0%,transparent 65%); animation:drift 20s ease-in-out infinite alternate-reverse; }
#     .orb-3 { top:35%; left:25%; width:45vw; height:45vw; background:radial-gradient(circle,rgba(16,185,129,0.18) 0%,transparent 65%); animation:drift 24s ease-in-out infinite alternate; }
#     @keyframes drift { 0%{transform:translate(0,0) scale(1);} 100%{transform:translate(35px,-35px) scale(1.08);} }
#     .glass { background:rgba(10,15,30,0.22); backdrop-filter:blur(38px); -webkit-backdrop-filter:blur(38px); border:1px solid rgba(255,255,255,0.09); border-top-color:rgba(255,255,255,0.14); border-left-color:rgba(255,255,255,0.12); border-radius:22px; box-shadow:0 20px 50px rgba(0,0,0,0.35); }

#     .topbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:22px; flex-wrap:wrap; gap:14px; }
#     .brand { font-size:22px; font-weight:800; letter-spacing:-.5px; }
#     .brand small { font-size:12px; font-weight:500; color:var(--dim); margin-left:8px; }
#     .nav-btn { padding:10px 20px; border-radius:11px; font-weight:700; font-size:13px; border:1px solid rgba(255,255,255,0.08); text-decoration:none; color:#60a5fa; background:rgba(59,130,246,0.12); transition:.25s; }
#     .nav-btn:hover { background:rgba(59,130,246,0.25); }

#     .ctrl-panel { padding:18px 22px; margin-bottom:16px; display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
#     input[type=number] { width:82px; padding:10px 12px; border:1px solid rgba(255,255,255,0.08); border-radius:10px; background:rgba(255,255,255,0.04); color:#fff; font-size:14px; font-weight:700; outline:none; text-align:center; font-family:'JetBrains Mono',monospace; }
#     .btn { padding:9px 18px; border-radius:10px; font-family:'Plus Jakarta Sans',sans-serif; font-weight:700; font-size:13px; cursor:pointer; border:1px solid rgba(255,255,255,0.06); transition:.25s; }
#     .btn:hover { transform:translateY(-2px); }
#     .btn-blue  { background:rgba(59,130,246,0.15); color:#60a5fa; }
#     .btn-teal  { background:rgba(16,185,129,0.1);  color:#34d399; }
#     .btn-red   { background:rgba(239,68,68,0.1);   color:#f87171; }

#     .ribbon { padding:13px 20px; margin-bottom:16px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }
#     .badge { padding:5px 13px; border-radius:20px; font-size:11px; font-weight:800; text-transform:uppercase; letter-spacing:1px; border:1px solid; }
#     .badge-live { background:rgba(52,211,153,0.09); color:#34d399; border-color:rgba(52,211,153,0.25); }
#     .pill { display:inline-flex; align-items:center; gap:6px; padding:5px 13px; border-radius:20px; font-size:11px; font-weight:800; }
#     .pill-amber { background:rgba(251,191,36,0.08); border:1px solid rgba(251,191,36,0.2); color:#fbbf24; }
#     .pill-green { background:rgba(52,211,153,0.08); border:1px solid rgba(52,211,153,0.2); color:#34d399; }
#     .pill-red   { background:rgba(248,113,113,0.08); border:1px solid rgba(248,113,113,0.2); color:#f87171; }
#     .dot { width:7px; height:7px; border-radius:50%; animation:blink 1.4s infinite; }
#     .dot-a { background:#fbbf24; } .dot-g { background:#34d399; } .dot-r { background:#f87171; }
#     @keyframes blink { 0%,100%{opacity:1;} 50%{opacity:.3;} }
#     .meta-row { display:flex; gap:24px; align-items:center; flex-wrap:wrap; }
#     .mi { font-size:12px; color:var(--dim); }
#     .mi b { color:#fff; font-family:'JetBrains Mono',monospace; }

#     .tbl-wrap { overflow:auto; border-radius:18px; max-height:calc(100vh - 310px); }
#     table { width:100%; border-collapse:collapse; min-width:1500px; }
#     thead th { position:sticky; top:0; z-index:5; background:rgba(0,0,0,0.18); backdrop-filter:blur(12px); color:#cbd5e1; font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:1.4px; padding:15px 8px; border-bottom:1px solid rgba(255,255,255,0.09); white-space:nowrap; font-family:'Plus Jakarta Sans',sans-serif; }
#     .thc { color:var(--call); } .thp { color:var(--put); } .ths { color:var(--strike); }
#     tbody td { padding:12px 8px; text-align:center; border-bottom:1px solid rgba(255,255,255,0.03); font-size:12px; font-family:'JetBrains Mono',monospace; white-space:nowrap; background:transparent; transition:.15s; }
#     tbody tr:hover td { background:rgba(255,255,255,0.025); color:#fff; }
#     tbody tr:nth-child(even) td { background:rgba(255,255,255,0.01); }
#     .cpos { background:rgba(0,41,31,0.6) !important; color:#34d399; font-weight:700; }
#     .cneg { background:rgba(45,0,16,0.6) !important; color:#f87171; font-weight:700; }
#     .czro { color:var(--dim); }
#     .cpnd { color:var(--dim); font-style:italic; }
#     .cdp  { color:#34d399; } .cdn { color:#f87171; }
#     .csk  { color:var(--strike); font-weight:800; font-size:13px; }
#     .cdt  { color:#fb923c; font-size:11px; }
#     .cidx { color:#60a5fa; }
#     ::-webkit-scrollbar { width:6px; height:6px; }
#     ::-webkit-scrollbar-thumb { background:#1e3a5f; border-radius:20px; }
#   </style>
# </head>
# <body>
#   <div class="orb orb-1"></div>
#   <div class="orb orb-2"></div>
#   <div class="orb orb-3"></div>

#   <div class="topbar">
#     <div class="brand">📊 Live Recorder <small>Crypto-Nexus · Delta Exchange</small></div>
#     <a href="/" class="nav-btn">⚙ Admin</a>
#   </div>

#   <div class="glass ctrl-panel">
#     <div style="font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:1px;">Interval (s)</div>
#     <input type="number" id="inp-interval" value="15" min="1">
#     <button class="btn btn-blue" onclick="applyInterval()">⚡ Apply</button>
#     <button class="btn btn-teal" onclick="downloadExcel()">⬇ Excel</button>
#     <button class="btn btn-red"  onclick="clearData()">🗑 Clear</button>
#     <div class="pill pill-amber" style="margin-left:auto;">
#       <span class="dot dot-a"></span><span id="active-count">0</span> online
#     </div>
#   </div>

#   <div class="glass ribbon">
#     <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
#       <span class="badge badge-live">● LIVE</span>
#       <span style="font-size:12px;color:var(--dim);" id="last-updated">Connecting…</span>
#     </div>
#     <div class="meta-row">
#       <div class="mi">Rows <b id="row-count">0</b></div>
#       <div class="mi">Strike Price <b id="last-strike">—</b></div>
#       <div class="mi">LTP <b id="last-index">—</b></div>
#       <div class="mi" id="run-pill">
#         <span class="pill pill-green"><span class="dot dot-g"></span>Running <b>0.00</b></span>
#       </div>
#     </div>
#   </div>

#   <div class="glass" style="padding:0; overflow:hidden;">
#     <div class="tbl-wrap">
#       <table>
#         <thead>
#           <tr>
#             <th>DateTime</th><th>Expiry</th>
#             <th class="thc">CE ₹</th><th class="thc">Δ</th><th class="thc">Γ</th><th class="thc">Θ</th><th class="thc">V</th>
#             <th class="ths">Strike</th>
#             <th class="thp">PE ₹</th><th class="thp">Δ</th><th class="thp">Γ</th><th class="thp">Θ</th><th class="thp">V</th>
#             <th>Ratio</th><th class="ths">Index ₹</th><th>Reference</th><th>Stretched</th><th>Difference</th>
#             <th>Diff Prev</th><th>Running</th>
#           </tr>
#         </thead>
#         <tbody id="tbl-body"></tbody>
#       </table>
#     </div>
#   </div>

# <script>
# let records = [], intervalSec = 15, loopId = null;

# const fmt = (v, d=2) => (v===null||v===undefined||v==="") ? "—" : isNaN(parseFloat(v)) ? "—" : parseFloat(v).toFixed(d);
# const dc  = v => { const n=parseFloat(v); return isNaN(n)?"":n>0?"cdp":n<0?"cdn":""; };

# function buildRow(r, isPending) {
#   const rv  = parseFloat(r.running||0);
#   const rc  = isPending ? "cpnd" : rv>0 ? "cpos" : rv<0 ? "cneg" : "czro";
#   return `
#     <td class="cdt">${r.datetime||"—"}</td>
#     <td style="color:#a78bfa;font-size:11px;">${r.expiry||"—"}</td>
#     <td style="color:#34d399;font-weight:700;">${fmt(r.ce_ltp)}</td>
#     <td>${fmt(r.ce_delta,4)}</td><td>${fmt(r.ce_gamma,5)}</td><td>${fmt(r.ce_theta,2)}</td><td>${fmt(r.ce_vega,2)}</td>
#     <td class="csk">${r.strike||"—"}</td>
#     <td style="color:#f87171;font-weight:700;">${fmt(r.pe_ltp)}</td>
#     <td>${fmt(r.pe_delta,4)}</td><td>${fmt(r.pe_gamma,5)}</td><td>${fmt(r.pe_theta,2)}</td><td>${fmt(r.pe_vega,2)}</td>
#     <td class="${dc(r.delta_ratio)}">${fmt(r.delta_ratio,5)}</td>
#     <td class="cidx">${fmt(r.index_ltp,2)}</td>
#     <td>${fmt(r.reference,5)}</td>
#     <td class="${dc(r.stretched)}">${fmt(r.stretched,5)}</td>
#     <td class="${dc(r.difference)}">${fmt(r.difference,2)}</td>
#     <td class="${isPending?"cpnd":dc(r.diff_prev)}">${isPending?"—":fmt(r.diff_prev,2)}</td>
#     <td class="${rc}">${isPending?"—":rv.toFixed(2)}</td>
#   `;
# }

# function renderRecords() {
#   const tbody = document.getElementById("tbl-body");
#   if (records.length > 1) {
#     const prev = tbody.querySelector(`tr[data-idx="${records.length-2}"]`);
#     if (prev) {
#       prev.innerHTML = buildRow(records[records.length-2], false);
#       const tr = document.createElement("tr");
#       tr.setAttribute("data-idx", String(records.length-1));
#       tr.innerHTML = buildRow(records[records.length-1], true);
#       tbody.appendChild(tr);
#       updateMeta(); return;
#     }
#   }
#   tbody.innerHTML = "";
#   records.forEach((r,idx) => {
#     const tr = document.createElement("tr");
#     tr.setAttribute("data-idx", String(idx));
#     tr.innerHTML = buildRow(r, idx===records.length-1);
#     tbody.appendChild(tr);
#   });
#   updateMeta();
# }

# function updateMeta() {
#   document.getElementById("row-count").innerText = records.length;
#   if (!records.length) return;
#   const last = records[records.length-1];
#   document.getElementById("last-strike").innerText = last.strike||"—";
#   document.getElementById("last-index").innerText  = last.index_ltp ? `₹${parseFloat(last.index_ltp).toLocaleString("en-IN",{maximumFractionDigits:0})}` : "—";
#   const rv   = parseFloat(records.length>=2 ? records[records.length-2].running : 0);
#   const pcls = rv>0?"pill-green":rv<0?"pill-red":"pill-amber";
#   const dcls = rv>0?"dot-g":rv<0?"dot-r":"dot-a";
#   document.getElementById("run-pill").innerHTML = `<span class="pill ${pcls}"><span class="dot ${dcls}"></span>Running <b>${rv>0?"+":""}${rv.toFixed(2)}</b></span>`;
# }

# async function fetchData() {
#   try {
#     const data = await (await fetch("/api/get-running?t="+Date.now())).json();
#     if (!data.rows || !data.rows.length) return;
#     if (data.rows.length !== records.length) {
#       records = data.rows;
#       renderRecords();
#       document.getElementById("last-updated").innerText = "Updated "+new Date().toLocaleTimeString();
#     }
#   } catch(e) { console.error(e); }
# }

# async function loadInterval() {
#   try { const d=await(await fetch("/api/get-interval")).json(); if(d.interval){intervalSec=d.interval;document.getElementById("inp-interval").value=d.interval;} } catch(e){}
# }
# async function applyInterval() {
#   const v=parseInt(document.getElementById("inp-interval").value);
#   if(!v||v<1) return;
#   await fetch("/api/set-interval",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({interval:v})});
#   intervalSec=v; scheduleNext();
# }
# async function clearData() {
#   if(!confirm("Delete all recorded data?")) return;
#   await fetch("/api/clear-running",{method:"POST"});
#   records=[]; document.getElementById("tbl-body").innerHTML=""; updateMeta();
# }

# function downloadExcel() {
#   if (!records.length) { alert("No data."); return; }
#   const now=new Date(), ds=now.toISOString().slice(0,10), dn=now.toLocaleDateString("en-IN",{weekday:"long"});
#   const hdrs=["DateTime","Expiry","CE ₹","CE Δ","CE Γ","CE Θ","CE V","Strike","PE ₹","PE Δ","PE Γ","PE Θ","PE V","Delta Ratio","Index ₹","Reference","Stretched","Difference","Diff Prev","Running"];
#   const fn=v=>{if(v===null||v===undefined||v==="")return"";const n=parseFloat(v);return isNaN(n)?"":n;};
#   const dataRows=records.map((r,i)=>{const p=i===records.length-1;return[r.datetime??"",r.expiry??"",fn(r.ce_ltp),fn(r.ce_delta),fn(r.ce_gamma),fn(r.ce_theta),fn(r.ce_vega),r.strike??"",fn(r.pe_ltp),fn(r.pe_delta),fn(r.pe_gamma),fn(r.pe_theta),fn(r.pe_vega),fn(r.delta_ratio),fn(r.index_ltp),fn(r.reference),fn(r.stretched),fn(r.difference),p?"":fn(r.diff_prev),p?"":fn(r.running)];});
#   const lastS=records.length>=2?records[records.length-2]:null;
#   const fr=lastS?Number(lastS.running||0):0;
#   const wsData=[[`Crypto-Nexus-Engine — Live Recorder | ${ds} (${dn})`],["Delta Exchange BTC Options | ₹INR | USD×94.33"],[],hdrs,...dataRows,[],[`Final Running: ${fr>=0?"+":""}${fr.toFixed(2)}`]];
#   const wb=XLSX.utils.book_new(),ws=XLSX.utils.aoa_to_sheet(wsData);
#   ws["!cols"]=[{wch:22},{wch:12},{wch:10},{wch:10},{wch:10},{wch:10},{wch:9},{wch:9},{wch:10},{wch:10},{wch:10},{wch:10},{wch:9},{wch:12},{wch:12},{wch:12},{wch:13},{wch:11},{wch:10},{wch:11}];
#   ws["!merges"]=[{s:{r:0,c:0},e:{r:0,c:19}},{s:{r:1,c:0},e:{r:1,c:19}},{s:{r:wsData.length-1,c:0},e:{r:wsData.length-1,c:9}}];
#   XLSX.utils.book_append_sheet(wb,ws,ds);
#   XLSX.writeFile(wb,`CryptoNexus_${ds}.xlsx`,{bookType:"xlsx",cellStyles:true});
# }

# async function fetchUsers() { try{document.getElementById("active-count").innerText=((await(await fetch("/api/active-users")).json()).total)||0;}catch(e){} }
# function scheduleNext() { if(loopId) clearTimeout(loopId); loopId=setTimeout(loop,intervalSec*1000); }
# async function loop() { await fetchData(); scheduleNext(); }
# async function hb() { await fetch("/api/heartbeat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({page:"recorder"})}).catch(()=>{}); }

# window.addEventListener("load", async () => {
#   await loadInterval(); loop(); fetchUsers();
#   setInterval(fetchUsers,20000); hb(); setInterval(hb,20000);
# });
# </script>
# </body>
# </html>
# """

if __name__ == "__main__":
    print("🚀 Crypto-Nexus-Engine")
    print("   Admin  → http://127.0.0.1:5000/")
    print("   Recorder → http://127.0.0.1:5000/recorder")
    print("   Debug API → http://127.0.0.1:5000/api/debug-raw")
    app.run(debug=True, port=5000, threaded=True)
