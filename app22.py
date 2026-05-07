from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
import csv, os, shutil, glob


LAST_DATA = {
    "time": None,
    "data": None,
    "DASH_HISTORY": {
        "last_diff": 0,
        "running": 0,
        "rows": []
    }
}
LIVE_RUNNING_RECORDS = []


app = FastAPI()

# ── Serve static files (dashboard.html lives in ./static/) ──────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Upload storage folder ────────────────────────────────────────────────
UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploaded_csvs")
os.makedirs(UPLOADS_DIR, exist_ok=True)

ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzc2NjY0MTg3LCJpYXQiOjE3NzY1Nzc3ODcsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTAwNTg1OTc1In0.RScjvOcn1Ck-abzKLgYb8fNH6wqDrTqbqnt5b4uxGCSZ2mRI5VU_xhkp3yPMtM_BgB-0L95cEOGj15-Dm28Q3w"
# ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
# CLIENT_ID  = os.getenv("CLIENT_ID")
CLIENT_ID  = "1100585975"
BASE_URL   = "https://api.dhan.co/v2"
HEADERS    = {
    "Content-Type": "application/json",
    "access-token": ACCESS_TOKEN,
    "client-id":    CLIENT_ID,
}

CSV_FILE = "sensex_atm_history.csv"
CSV_COLUMNS = [
    "DateTime", "Expiry", "Strike",
    "CE_LTP", "CE_Delta", "CE_Gamma", "CE_Theta", "CE_Vega",
    "PE_LTP", "PE_Delta", "PE_Gamma", "PE_Theta", "PE_Vega",
    "Delta_Ratio", "Index_LTP",
    "Reference", "Stretched", "Difference"
]

# ── Recorder state ───────────────────────────────────────────────────────
recorder_state = {
    "running": False, "interval": 20,
    "expiry": None, "start_time": None,
    "stop_time": None, "records_saved": 0,
}
scheduler = BackgroundScheduler()
scheduler.start()


# ═══════════════════════════════════════════════════════════════════════
# CORE DATA FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def get_expiries():
    try:
        res  = requests.post(BASE_URL + "/optionchain/expirylist", headers=HEADERS,
                             json={"UnderlyingScrip": 51, "UnderlyingSeg": "IDX_I"})
        data = res.json()
        return sorted(data.get("data", [])) if data.get("status") == "success" else []
    except:
        return []


def fetch_live_option_chain(expiry):
    res  = requests.post(BASE_URL + "/optionchain", headers=HEADERS,
                         json={"UnderlyingScrip": 51, "UnderlyingSeg": "IDX_I", "Expiry": expiry})
    data = res.json()
    if data.get("status") != "success":
        raise ValueError(f"API error: {data}")
    d = data["data"]
    return float(d.get("last_price", 0)), d.get("oc", {})


def build_df_from_oc(ltp, oc, expiry, dt_label):
    rows = []
    for strike, val in oc.items():
        ce   = val.get("ce", {});  pe   = val.get("pe", {})
        ce_g = ce.get("greeks") or {}; pe_g = pe.get("greeks") or {}
        try:
            ratio = round((float(pe_g["delta"]) / float(ce_g["delta"])) * -1, 5)
        except:
            ratio = None
        rows.append({
            "DateTime": dt_label, "Expiry": expiry, "Strike": float(strike),
            "CE_LTP":    ce.get("last_price", "-"), "CE_Delta": ce_g.get("delta", "-"),
            "CE_Gamma":  ce_g.get("gamma", "-"),    "CE_Theta": ce_g.get("theta", "-"),
            "CE_Vega":   ce_g.get("vega", "-"),
            "PE_LTP":    pe.get("last_price", "-"), "PE_Delta": pe_g.get("delta", "-"),
            "PE_Gamma":  pe_g.get("gamma", "-"),    "PE_Theta": pe_g.get("theta", "-"),
            "PE_Vega":   pe_g.get("vega", "-"),
            "Delta_Ratio": ratio,
        })

    df = pd.DataFrame(rows).sort_values("Strike").reset_index(drop=True)
    if df.empty:
        return df, None

    df["diff"] = abs(df["Strike"] - ltp)
    atm_idx    = df["diff"].idxmin()
    atm_strike = df.loc[atm_idx, "Strike"]

    df = df.iloc[max(atm_idx - 10, 0): atm_idx + 11].reset_index(drop=True)
    df["diff"] = abs(df["Strike"] - ltp)
    atm_idx    = df["diff"].idxmin()

    # =========================
    # REFERENCE (ROW-WISE FIX)
    # =========================
    df["Reference"] = None

    for i in range(len(df)):
        try:
            if i == 0 or i == len(df) - 1:
                continue

            prev_val = df.loc[i - 1, "Delta_Ratio"]
            next_val = df.loc[i + 1, "Delta_Ratio"]

            if isinstance(prev_val, float) and isinstance(next_val, float):
                ref = ((prev_val + next_val) / 2) - 0.06
                df.loc[i, "Reference"] = round(ref, 5)

        except:
            continue

    # =========================
    # STRETCHED (ROW-WISE FIX)
    # =========================
    df["Stretched"] = None

    for i in range(len(df)):
        try:
            curr_dr = df.loc[i, "Delta_Ratio"]
            curr_ref = df.loc[i, "Reference"]

            if curr_ref == "0.00000" or curr_ref is None:
                continue

            curr_ref = float(curr_ref)

            if i < 2:
                continue

            prev1 = df.loc[i - 1, "Delta_Ratio"]
            prev2 = df.loc[i - 2, "Delta_Ratio"]

            try:
                curr_dr = float(curr_dr)
                prev1 = float(prev1)
                prev2 = float(prev2)
            except:
                continue

            denom = (prev1 - prev2) / 100

            if denom == 0:
                continue

            stretched_val = df.loc[i, "Strike"] - ((curr_dr - curr_ref) / denom)
            df.loc[i, "Stretched"] = f"{stretched_val:.5f}"

        except:
            continue

    df["Stretched"] = df["Stretched"].fillna("")

    def calc_diff(s):
        try:
            if s == "" or s is None:
                return ""
            return round(float(s) - ltp, 2)
        except:
            return ""

    df["Difference"] = df["Stretched"].apply(calc_diff)

    return df, atm_strike


def get_live_chain(expiry):
    global LAST_DATA

    if LAST_DATA["time"] and (datetime.now() - LAST_DATA["time"]).seconds < 5:
        return LAST_DATA["data"]

    try:
        ltp, oc  = fetch_live_option_chain(expiry)
        dt_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df, atm  = build_df_from_oc(ltp, oc, expiry, dt_label)

        LAST_DATA["time"] = datetime.now()
        LAST_DATA["data"] = (ltp, df, atm)

        return ltp, df, atm

    except Exception as e:
        print("get_live_chain error:", e)
        return LAST_DATA["data"] if LAST_DATA["data"] else (0, pd.DataFrame(), None)


def get_historical_snapshot(expiry, target_dt_str):
    if not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0:
        return None, None
    try:
        df = pd.read_csv(CSV_FILE)
        df = df[df["Expiry"].astype(str).str.strip() == expiry.strip()]
        if df.empty:
            return None, None

        def parse_dt(s):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
                try: return datetime.strptime(str(s).strip(), fmt)
                except: pass
            return None

        df["_dt"] = df["DateTime"].apply(parse_dt)
        df = df.dropna(subset=["_dt"])
        if df.empty:
            return None, None

        target_dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S"):
            try: target_dt = datetime.strptime(target_dt_str.strip(), fmt); break
            except: pass
        if target_dt is None:
            return None, None

        df["_diff"] = abs(df["_dt"] - target_dt).apply(lambda x: x.total_seconds())
        closest    = df.loc[df["_diff"].idxmin()]
        return str(closest.get("Index_LTP", "-")), closest.to_dict()
    except Exception as e:
        print("get_historical_snapshot error:", e)
        return None, None


# ═══════════════════════════════════════════════════════════════════════
# SCHEDULER / RECORDER
# ═══════════════════════════════════════════════════════════════════════

def save_atm_to_csv(expiry):
    try:
        ltp, df, atm_strike = get_live_chain(expiry)
        if df.empty or atm_strike is None:
            return
        r   = df[df["Strike"] == atm_strike].iloc[0]
        hdr = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0
        with open(CSV_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if hdr: w.writerow(CSV_COLUMNS)
            w.writerow([
                r["DateTime"], r["Expiry"], int(r["Strike"]),
                r["CE_LTP"],  r["CE_Delta"],  r["CE_Gamma"],  r["CE_Theta"],  r["CE_Vega"],
                r["PE_LTP"],  r["PE_Delta"],  r["PE_Gamma"],  r["PE_Theta"],  r["PE_Vega"],
                r["Delta_Ratio"], ltp,
                r["Reference"], r["Stretched"], r["Difference"],
                
            ])
        recorder_state["records_saved"] += 1
        print(f"[{datetime.now():%H:%M:%S}] Saved ATM Strike={int(atm_strike)} LTP={ltp}")
    except Exception as e:
        print("save_atm_to_csv error:", e)


def scheduled_job():
    now = datetime.now()
    st  = recorder_state.get("stop_time")
    if st and now >= st:
        _stop_recording(); return
    if recorder_state["running"] and recorder_state["expiry"]:
        save_atm_to_csv(recorder_state["expiry"])


def _stop_recording():
    recorder_state["running"] = False
    if scheduler.get_job("atm_rec"): scheduler.remove_job("atm_rec")
    print(f"[{datetime.now():%H:%M:%S}] Recording STOPPED.")


def _reschedule(secs):
    if scheduler.get_job("atm_rec"): scheduler.remove_job("atm_rec")
    scheduler.add_job(scheduled_job, "interval", seconds=secs,
                      id="atm_rec", replace_existing=True)


# ═══════════════════════════════════════════════════════════════════════
# API — RECORDER
# ═══════════════════════════════════════════════════════════════════════

@app.post("/recorder/start")
async def start_recorder(req: Request):
    b        = await req.json()
    expiry   = b.get("expiry")
    interval = int(b.get("interval", 20))
    stop_str = b.get("stop_time", "")
    if not expiry:
        return JSONResponse({"status": "error", "message": "Expiry required"})
    recorder_state.update({
        "running": True, "expiry": expiry, "interval": interval,
        "start_time": datetime.now(), "records_saved": 0, "stop_time": None,
    })
    if stop_str:
        try:
            fmt = "%Y-%m-%d %H:%M:%S" if stop_str.count(":") == 2 else "%Y-%m-%d %H:%M"
            recorder_state["stop_time"] = datetime.strptime(
                f"{datetime.now().date()} {stop_str}", fmt)
        except: pass
    _reschedule(interval)
    save_atm_to_csv(expiry)
    return JSONResponse({"status": "started", "interval": interval, "expiry": expiry})


@app.post("/recorder/stop")
def stop_recorder():
    _stop_recording()
    return JSONResponse({"status": "stopped", "records_saved": recorder_state["records_saved"]})


@app.get("/recorder/status")
def recorder_status():
    return JSONResponse({
        "running":       recorder_state["running"],
        "expiry":        recorder_state["expiry"],
        "interval":      recorder_state["interval"],
        "records_saved": recorder_state["records_saved"],
        "start_time":    recorder_state["start_time"].strftime("%d-%m-%Y %H:%M:%S") if recorder_state["start_time"] else None,
        "stop_time":     recorder_state["stop_time"].strftime("%H:%M:%S") if recorder_state["stop_time"] else None,
    })


@app.get("/download/csv")
def download_csv():
    if os.path.exists(CSV_FILE):
        return FileResponse(CSV_FILE, media_type="text/csv", filename="sensex_atm_history.csv")
    return JSONResponse({"error": "No CSV yet."})

@app.get("/api/downloads")
def api_downloads():
    files = sorted(glob.glob(os.path.join(UPLOADS_DIR, "*.csv")), reverse=True)

    result = []
    for f in files:
        name = os.path.basename(f)

        result.append({
            "name": name,
            "url": f"/user/download-csv/{name}",
            "date": name.replace(".csv", "")
        })

    return JSONResponse(result)


@app.post("/recorder/clear")
def clear_csv():
    if os.path.exists(CSV_FILE): os.remove(CSV_FILE)
    recorder_state["records_saved"] = 0
    return JSONResponse({"status": "cleared"})


@app.get("/simple", response_class=HTMLResponse)
def simple_page():
    path = os.path.join(STATIC_DIR, "simple.html")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
    
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    path = os.path.join(STATIC_DIR, "dashboard.html")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# ═══════════════════════════════════════════════════════════════════════
# API — LIVE DATA
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/expiries")
def api_expiries():
    return JSONResponse(get_expiries())


@app.get("/api/live-data")
def api_live_data():
    global LIVE_RUNNING_RECORDS

    rows = LIVE_RUNNING_RECORDS[-51:]   # 1 top + 50 list rows

    return JSONResponse({
        "rows": rows
    })
# ═══════════════════════════════════════════════════════════════════════
# API — FULL CHAIN (all columns, all rows) — used by admin AJAX refresh
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/full-chain")
def api_full_chain(expiry: str = ""):
    """Return all 21 rows with every column for the admin table AJAX refresh."""
    if not expiry:
        expiries = get_expiries()
        expiry   = expiries[0] if expiries else ""
    if not expiry:
        return JSONResponse({"error": "No expiry available"})

    ltp, df, atm = get_live_chain(expiry)

    if df.empty:
        return JSONResponse({"error": "No data", "ltp": ltp})

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "datetime":    str(r["DateTime"]),
            "expiry":      str(r["Expiry"]),
            "strike":      int(r["Strike"]),
            "ce_ltp":      r["CE_LTP"],
            "ce_delta":    r["CE_Delta"],
            "ce_gamma":    r["CE_Gamma"],
            "ce_theta":    r["CE_Theta"],
            "ce_vega":     r["CE_Vega"],
            "pe_ltp":      r["PE_LTP"],
            "pe_delta":    r["PE_Delta"],
            "pe_gamma":    r["PE_Gamma"],
            "pe_theta":    r["PE_Theta"],
            "pe_vega":     r["PE_Vega"],
            "delta_ratio": r["Delta_Ratio"],
            "index_ltp":   ltp,
            "reference":   r["Reference"],
            "stretched":   r["Stretched"],
            "difference":  r["Difference"],
            "is_atm":      bool(r["Strike"] == atm),
        })

    return JSONResponse({
        "ltp":       ltp,
        "atm":       int(atm) if atm is not None else None,
        "expiry":    expiry,
        "timestamp": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        "rows":      rows,
    })


@app.get("/api/simple-data")
def api_simple_data(expiry: str = ""):
    try:
        expiries = get_expiries()

        if not expiry:
            expiry = expiries[0] if expiries else ""

        if not expiry:
            return JSONResponse({"error": "No expiry"})

        ltp, df, atm = get_live_chain(expiry)

        if df.empty or atm is None:
            return JSONResponse({"error": "No live data"})

        atm_row = df[df["Strike"] == atm]

        if atm_row.empty:
            return JSONResponse({"error": "ATM not found"})

        r = atm_row.iloc[0]

        return JSONResponse({
            "datetime": str(r["DateTime"]),
            "difference": r["Difference"]   # ✅ only valid column
        })

    except Exception as e:
        print("ERROR:", e)
        return JSONResponse({"error": str(e)})

# ═══════════════════════════════════════════════════════════════════════
# API — CSV UPLOAD / DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════

@app.post("/admin/upload-csv")
async def upload_csv(file: UploadFile = File(...), label: str = Form("")):
    safe = label.replace("/", "-").replace(" ", "_") or datetime.now().strftime("%Y-%m-%d")
    dest = os.path.join(UPLOADS_DIR, f"{safe}.csv")
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return JSONResponse({"status": "uploaded", "file": f"{safe}.csv"})


@app.delete("/admin/delete-csv/{filename}")
def delete_uploaded(filename: str):
    p = os.path.join(UPLOADS_DIR, filename)
    if os.path.exists(p):
        os.remove(p)
        return JSONResponse({"status": "deleted"})
    return JSONResponse({"status": "not_found"}, status_code=404)


@app.get("/admin/list-csvs")
def list_csvs():
    files = sorted(glob.glob(os.path.join(UPLOADS_DIR, "*.csv")), reverse=True)
    return JSONResponse([
        {"name": os.path.basename(f), "size": os.path.getsize(f)}
        for f in files
    ])


@app.get("/user/download-csv/{filename}")
def user_download(filename: str):
    p = os.path.join(UPLOADS_DIR, filename)
    if os.path.exists(p):
        return FileResponse(p, media_type="text/csv", filename=filename)
    return JSONResponse({"error": "File not found"}, status_code=404)

@app.post("/api/save-running")
async def save_running(req: Request):
    global LIVE_RUNNING_RECORDS

    data = await req.json()

    ts = data.get("datetime")

    # if same timestamp exists -> replace row
    found = False

    for i in range(len(LIVE_RUNNING_RECORDS)):
        if LIVE_RUNNING_RECORDS[i].get("datetime") == ts:
            LIVE_RUNNING_RECORDS[i] = data
            found = True
            break

    if not found:
        LIVE_RUNNING_RECORDS.append(data)

    # keep latest 51
    LIVE_RUNNING_RECORDS = LIVE_RUNNING_RECORDS[-51:]

    return JSONResponse({"status": "ok"})
# ═══════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════

@app.get("/user", response_class=HTMLResponse)
def user_dashboard():
    path = os.path.join(STATIC_DIR, "simple.html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(
        "<h3 style='color:red;font-family:sans-serif;padding:20px'>"
        "simple.html not found.<br>Place it in the <b>static/</b> folder next to app.py.</h3>",
        status_code=404,
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Admin panel — option chain table + recorder. Table refreshes via AJAX every 5s."""
    expiries = get_expiries()
    if not expiries:
        return HTMLResponse("<h3 style='color:red'>API ERROR: Could not fetch expiries</h3>")

    selected_expiry = request.query_params.get("expiry") or expiries[0]
    if selected_expiry not in expiries:
        selected_expiry = expiries[0]

    manual_date = request.query_params.get("manual_date", "")
    manual_time = request.query_params.get("manual_time", "")
    use_manual  = bool(manual_date and manual_time)

    hist_row = hist_ltp = hist_error = hist_actual_dt = None

    if use_manual:
        target_str = f"{manual_date} {manual_time}"
        hist_ltp, hist_row = get_historical_snapshot(selected_expiry, target_str)
        if hist_row is None:
            hist_error = (
                f"No saved data found for expiry <b>{selected_expiry}</b> "
                f"around <b>{manual_date} {manual_time}</b>.<br>"
                f"Record live data first using the Recorder below, then replay here."
            )
        else:
            hist_actual_dt = str(hist_row.get("DateTime", ""))

    if not use_manual:
        ltp, df, atm = get_live_chain(selected_expiry)
    else:
        ltp = hist_ltp or "—"; df = pd.DataFrame(); atm = None

    opts = "".join(
        f'<option value="{e}" {"selected" if e == selected_expiry else ""}>{e}</option>'
        for e in expiries
    )

    if use_manual and hist_row and not hist_error:
        table_rows = _single_csv_row_html(hist_row, hist_ltp)
        data_note  = f'<span class="badge badge-hist">📂 HISTORICAL — Nearest: {hist_actual_dt}</span>'
    elif not use_manual and not df.empty:
        table_rows = _build_live_table_rows(df, atm, ltp)
        data_note  = '<span class="badge badge-live">🔴 LIVE — Auto-refresh 5s</span>'
    else:
        table_rows = ""
        data_note  = '<span class="badge badge-live">🔴 LIVE</span>'

    mode_info = f'<div class="err-box">⚠️ {hist_error}</div>' if hist_error else ""
    ltp_disp  = hist_ltp if (use_manual and hist_ltp) else ltp

    # Is this a live (non-manual) view? Controls whether JS auto-refresh runs.
    is_live_js = "true" if not use_manual else "false"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Sensex Option Chain — Admin</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0b0f1a;color:#e0e0e0;font-family:Arial,sans-serif;padding:12px;font-size:13px}}
  h2{{margin-bottom:10px;font-size:17px;color:#fff}}
  .badge{{display:inline-block;padding:4px 12px;border-radius:4px;font-weight:bold;font-size:13px}}
  .badge-live{{background:#00c853;color:#000}}.badge-hist{{background:#7b1fa2;color:#fff}}
  .err-box{{background:#4a1010;border:1px solid #c62828;border-radius:6px;padding:10px 14px;margin:8px 0;color:#ffcdd2;font-size:13px;line-height:1.6}}
  .info-box{{background:#0d2137;border:1px solid #1565c0;border-radius:6px;padding:8px 14px;margin:6px 0;color:#90caf9;font-size:12px}}
  .ctrl-bar{{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin-bottom:10px}}
  .ctrl-bar label{{font-size:11px;color:#90a4ae;display:block;margin-bottom:3px}}
  select,input[type=number],input[type=time],input[type=date]{{background:#1e2a3a;color:#fff;border:1px solid #455a64;padding:6px 9px;border-radius:4px;font-size:13px}}
  .btn{{padding:7px 16px;border:none;border-radius:4px;cursor:pointer;font-size:13px;font-weight:bold;white-space:nowrap}}
  .btn:hover{{opacity:.85}}
  .btn-apply{{background:#2979ff;color:#fff}}.btn-live{{background:#c62828;color:#fff}}
  .btn-start{{background:#00c853;color:#000}}.btn-stop{{background:#e53935;color:#fff}}
  .btn-dl{{background:#1565c0;color:#fff}}.btn-clr{{background:#e65100;color:#fff}}
  .btn-user{{background:#6a1b9a;color:#fff}}
  .rec-panel{{background:#0d1b2a;border:1px solid #2979ff;border-radius:8px;padding:12px 16px;margin-bottom:12px}}
  .rec-panel h3{{color:#64b5f6;font-size:13px;margin-bottom:10px}}
  .rec-row{{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end}}
  .field{{display:flex;flex-direction:column;gap:3px}}
  .field label{{font-size:11px;color:#90a4ae}}
  #status-box{{margin-top:10px;background:#060e1a;border:1px solid #1a2a3a;border-radius:5px;padding:7px 12px;font-size:12px;color:#ccc}}
  .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:middle}}
  .dot-on{{background:#00c853;box-shadow:0 0 6px #00c853}}.dot-off{{background:#e53935}}
  .upload-panel{{background:#0d1b2a;border:1px solid #7b1fa2;border-radius:8px;padding:12px 16px;margin-bottom:12px}}
  .upload-panel h3{{color:#ce93d8;font-size:13px;margin-bottom:10px}}
  .file-list{{margin-top:8px;display:flex;flex-direction:column;gap:4px;max-height:160px;overflow-y:auto}}
  .file-item{{display:flex;justify-content:space-between;align-items:center;background:#111827;padding:5px 10px;border-radius:4px;font-size:12px;color:#ce93d8}}
  .tbl-wrap{{overflow-x:auto;margin-top:8px}}
  table{{border-collapse:collapse;font-size:12px;min-width:100%}}
  th,td{{border:1px solid #1e2d3d;padding:5px 8px;text-align:center;white-space:nowrap}}
  th{{background:#102030;color:#90caf9;position:sticky;top:0;z-index:1}}
  tr:hover td{{background:#0d2137}}
  p{{margin:6px 0}}
  a{{text-decoration:none}}
  .topbar{{display:flex;align-items:center;gap:10px;margin-bottom:12px}}
  #refresh-indicator{{display:inline-block;margin-left:12px;font-size:11px;color:#546e7a;vertical-align:middle}}
  .spin{{animation:spin 1s linear infinite;display:inline-block}}
  @keyframes spin{{from{{transform:rotate(0deg)}}to{{transform:rotate(360deg)}}}}
</style>
</head>
<body>

<div class="topbar">
  <h2>📊 SENSEX OPTION CHAIN — Single Recorded PANEL</h2>
  <a href="/user" style="margin-left:auto">
    <button class="btn btn-user">👤 Admin Dashboard →</button>
  </a>
</div>

<div class="topbar">
  <a href="/dashboard" style="margin-left:auto">
    <button class="btn btn-user">👤 User Dashboard →</button>
  </a>
</div>

<!-- CONTROLS -->
<div class="ctrl-bar">
  <div><label>Expiry</label>
    <select id="master-expiry" onchange="onExpiryChange()">{opts}</select></div>
   <div><label>Manual Date</label>
    <input type="date" id="inp-date" value="{manual_date}"></div>
  <div><label>Manual Time (HH:MM:SS)</label>
    <input type="time" id="inp-time" value="{manual_time}" step="1"></div>
  <button class="btn btn-apply" onclick="applyView()">▶ Apply</button>
  <button class="btn btn-live"  onclick="goLive()">✕ Go Live</button>
</div>

<div id="data-note">{data_note}</div>
<span id="refresh-indicator"></span>
{mode_info}
<p style="margin:6px 0" id="ltp-line">
  LTP: <b id="ltp-val" style="color:#00e5ff;font-size:14px">{ltp_disp}</b>
  &nbsp;|&nbsp; Expiry: <b style="color:#ffeb3b">{selected_expiry}</b>
  {"&nbsp;|&nbsp; <span style='color:#ce93d8'>Nearest saved snapshot</span>" if use_manual and hist_row else ""}
</p>

{"<div class='info-box'>ℹ️ <b>How historical view works:</b> Start the Recorder to save ATM snapshots, then select a past date &amp; time to replay.</div>" if use_manual and not hist_row else ""}

<!-- RECORDER -->
<!-- <div class="rec-panel">
  <h3>📁 ATM Data Recorder → CSV</h3>
  <div class="rec-row">
    <div class="field"><label>Interval (sec)</label>
      <input type="number" id="rec-interval" value="20" min="5" max="600" step="5" style="width:80px"></div>
    <div class="field"><label>Scheduled Start (HH:MM:SS)</label>
      <input type="time" id="rec-start-time" step="1" style="width:140px"></div>
    <div class="field"><label>Auto-Stop (HH:MM:SS)</label>
      <input type="time" id="rec-stop-time" step="1" style="width:140px"></div>
    <div style="display:flex;gap:7px;align-items:flex-end;flex-wrap:wrap">
      <button class="btn btn-start" onclick="startRecorder()">▶ Start Recording</button>
      <button class="btn btn-stop"  onclick="stopRecorder()">⏹ Stop</button>
      <a href="/download/csv"><button class="btn btn-dl" type="button">⬇ Download CSV</button></a>
      <button class="btn btn-clr"   onclick="clearCSV()">🗑 Clear CSV</button>
    </div>
  </div>
  <div id="status-box">
    <span class="dot dot-off" id="sdot"></span>
    <span id="stext">Not Recording</span>
  </div>
</div>-->

<!-- CSV UPLOAD (admin → users) -->
<div class="upload-panel">
  <h3>📤 Upload CSV for Users (Date-wise)</h3>
  <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end">
    <div class="field"><label>Date Label (YYYY-MM-DD)</label>
      <input type="date" id="upload-label" style="width:160px"></div>
    <div class="field"><label>CSV File</label>
      <input type="file" id="upload-file" accept=".csv"
             style="background:#1e2a3a;color:#fff;border:1px solid #455a64;padding:5px;border-radius:4px"></div>
    <button class="btn btn-start" onclick="uploadCSV()">⬆ Upload</button>
  </div>
  <div class="file-list" id="file-list"><i style="color:#666">Loading...</i></div>
</div>

<!-- OPTION CHAIN TABLE -->
<div class="tbl-wrap">
<table>
<thead><tr>
  <th>Date Time</th><th>Expiry</th>
  <th>CE LTP</th><th>Δ</th><th>Γ</th><th>Θ</th><th>V</th>
  <th>STRIKE</th>
  <th>PE LTP</th><th>Δ</th><th>Γ</th><th>Θ</th><th>V</th>
  <th>Delta Ratio</th><th>Index LTP</th>
  <th>Reference</th><th>Stretched</th><th>Difference</th>

</tr></thead>
<tbody id="tbl-body">{table_rows}</tbody>
</table>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────
let isLive        = {is_live_js};
let currentExpiry = "{selected_expiry}";
let refreshTimer  = null;

// ── Helpers ────────────────────────────────────────────────────────────
function fmt(v) {{
  if (v === null || v === undefined || v === "") return "";
  if (typeof v === "number") return v;
  return v;
}}

function runningStyle(v) {{
  if (v === null || v === "" || v === undefined) return "";
  const n = parseFloat(v);
  if (isNaN(n)) return "";
  if (n > 0) return "background:#1b5e20;color:white;";
  if (n < 0) return "background:#b71c1c;color:white;";
  return "";
}}

// ── Build table HTML from JSON rows ───────────────────────────────────
function buildTableHTML(rows) {{
  let html = "";
  rows.forEach(r => {{
    const atmStyle = r.is_atm
      ? "background:yellow;color:black;font-weight:bold;"
      : "";
    const runStyle = runningStyle(r.running);
    html += `<tr style="${{atmStyle}}">
      <td>${{fmt(r.datetime)}}</td>
      <td>${{fmt(r.expiry)}}</td>
      <td>${{fmt(r.ce_ltp)}}</td>
      <td>${{fmt(r.ce_delta)}}</td>
      <td>${{fmt(r.ce_gamma)}}</td>
      <td>${{fmt(r.ce_theta)}}</td>
      <td>${{fmt(r.ce_vega)}}</td>
      <td><b>${{r.strike}}</b></td>
      <td>${{fmt(r.pe_ltp)}}</td>
      <td>${{fmt(r.pe_delta)}}</td>
      <td>${{fmt(r.pe_gamma)}}</td>
      <td>${{fmt(r.pe_theta)}}</td>
      <td>${{fmt(r.pe_vega)}}</td>
      <td>${{fmt(r.delta_ratio)}}</td>
      <td>${{fmt(r.index_ltp)}}</td>
      <td>${{fmt(r.reference)}}</td>
      <td>${{fmt(r.stretched)}}</td>
      <td>${{fmt(r.difference)}}</td>

    </tr>`;
  }});
  return html;
}}

// ── AJAX fetch and update table (NO page reload) ───────────────────────
async function refreshTable() {{
  if (!isLive) return;

  const ind = document.getElementById("refresh-indicator");
  ind.innerHTML = '<span class="spin">⟳</span> Refreshing...';

  try {{
    const res  = await fetch(`/api/full-chain?expiry=${{encodeURIComponent(currentExpiry)}}`);
    const data = await res.json();

    if (data.error) {{
      ind.textContent = "⚠ " + data.error;
      return;
    }}

    // Update table body
    document.getElementById("tbl-body").innerHTML = buildTableHTML(data.rows);

    // Update LTP display
    document.getElementById("ltp-val").textContent = data.ltp;

    // Update timestamp in indicator
    ind.textContent = "✓ Updated " + data.timestamp;

  }} catch (e) {{
    ind.textContent = "⚠ Fetch error: " + e.message;
  }}
}}

// ── Start / stop auto-refresh ──────────────────────────────────────────
function startAutoRefresh() {{
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshTable, 5000);   // every 5 seconds
  refreshTable();   // immediate first call
}}

function stopAutoRefresh() {{
  if (refreshTimer) {{ clearInterval(refreshTimer); refreshTimer = null; }}
  document.getElementById("refresh-indicator").textContent = "";
}}

// ── Expiry / view controls ─────────────────────────────────────────────
function onExpiryChange() {{
  currentExpiry = document.getElementById("master-expiry").value;
  if (isLive) refreshTable();
}}

function applyView() {{
  const d = document.getElementById("inp-date").value;
  const t = document.getElementById("inp-time").value;
  if (d && t) {{
    isLive = false;
    stopAutoRefresh();
    const exp = document.getElementById("master-expiry").value;
    window.location.href = `/?expiry=${{encodeURIComponent(exp)}}&manual_date=${{d}}&manual_time=${{encodeURIComponent(t)}}`;
  }} else {{
    // No manual date/time — just switch expiry live
    isLive = true;
    currentExpiry = document.getElementById("master-expiry").value;
    startAutoRefresh();
  }}
}}

function goLive() {{
  isLive = true;
  document.getElementById("inp-date").value = "";
  document.getElementById("inp-time").value = "";
  currentExpiry = document.getElementById("master-expiry").value;
  document.getElementById("data-note").innerHTML =
    '<span class="badge badge-live">🔴 LIVE — Auto-refresh 5s</span>';
  startAutoRefresh();
}}

// ── Recorder controls ──────────────────────────────────────────────────
async function startRecorder() {{
  const expiry   = document.getElementById("master-expiry").value;
  const interval = parseInt(document.getElementById("rec-interval").value) || 20;
  const stopT    = document.getElementById("rec-stop-time").value;
  const body     = {{ expiry, interval, stop_time: stopT }};
  const res  = await fetch("/recorder/start", {{method:"POST", headers:{{"Content-Type":"application/json"}}, body:JSON.stringify(body)}});
  const data = await res.json();
  if (data.status === "started") {{
    document.getElementById("sdot").className  = "dot dot-on";
    document.getElementById("stext").textContent = `Recording — Expiry: ${{data.expiry}} | Interval: ${{data.interval}}s`;
  }}
}}

async function stopRecorder() {{
  const res  = await fetch("/recorder/stop", {{method:"POST"}});
  const data = await res.json();
  document.getElementById("sdot").className  = "dot dot-off";
  document.getElementById("stext").textContent = `Stopped — ${{data.records_saved}} records saved`;
}}

async function clearCSV() {{
  if (!confirm("Clear all recorded CSV data?")) return;
  await fetch("/recorder/clear", {{method:"POST"}});
  alert("CSV cleared.");
}}

// ── Recorder status poll ───────────────────────────────────────────────
async function pollRecorderStatus() {{
  try {{
    const res  = await fetch("/recorder/status");
    const data = await res.json();
    const dot  = document.getElementById("sdot");
    const txt  = document.getElementById("stext");
    if (data.running) {{
      dot.className = "dot dot-on";
      txt.textContent = `Recording — Expiry: ${{data.expiry}} | Interval: ${{data.interval}}s | Saved: ${{data.records_saved}}`;
    }} else {{
      dot.className = "dot dot-off";
      if (txt.textContent === "Not Recording") {{/* leave default */}}
    }}
  }} catch {{}}
}}
setInterval(pollRecorderStatus, 5000);
pollRecorderStatus();

// ── File list ──────────────────────────────────────────────────────────
async function loadFileList() {{
  try {{
    const res   = await fetch("/admin/list-csvs");
    const files = await res.json();
    const el    = document.getElementById("file-list");
    if (!files.length) {{ el.innerHTML = '<i style="color:#666">No uploaded files yet.</i>'; return; }}
    el.innerHTML = files.map(f =>
      `<div class="file-item">
        <span>${{f.name}} <span style="color:#546e7a">(${{ (f.size/1024).toFixed(1) }}KB)</span></span>
        <span style="display:flex;gap:6px">
          <a href="/user/download-csv/${{f.name}}"><button class="btn btn-dl" style="padding:3px 10px;font-size:11px">⬇</button></a>
          <button class="btn btn-clr" style="padding:3px 10px;font-size:11px" onclick="deleteFile('${{f.name}}')">🗑</button>
        </span>
      </div>`
    ).join("");
  }} catch {{}}
}}

async function deleteFile(name) {{
  if (!confirm(`Delete ${{name}}?`)) return;
  await fetch(`/admin/delete-csv/${{name}}`, {{method:"DELETE"}});
  loadFileList();
}}

async function uploadCSV() {{
  const label = document.getElementById("upload-label").value;
  const file  = document.getElementById("upload-file").files[0];
  if (!file) {{ alert("Select a CSV file first."); return; }}
  const fd = new FormData();
  fd.append("file", file);
  fd.append("label", label);
  const res  = await fetch("/admin/upload-csv", {{method:"POST", body:fd}});
  const data = await res.json();
  if (data.status === "uploaded") {{ alert(`Uploaded: ${{data.file}}`); loadFileList(); }}
  else alert("Upload failed.");
}}

// ── Init ───────────────────────────────────────────────────────────────
loadFileList();
if (isLive) startAutoRefresh();
</script>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════
# TABLE HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _build_live_table_rows(df, atm, ltp):
    html = ""
    for _, r in df.iterrows():
        is_atm = r["Strike"] == atm
        rs = "background:yellow;color:black;font-weight:bold;" if is_atm else ""
    
        html += f"""<tr style="{rs}">
          <td>{r['DateTime']}</td><td>{r['Expiry']}</td>
          <td>{r['CE_LTP']}</td><td>{r['CE_Delta']}</td><td>{r['CE_Gamma']}</td>
          <td>{r['CE_Theta']}</td><td>{r['CE_Vega']}</td>
          <td><b>{int(r['Strike'])}</b></td>
          <td>{r['PE_LTP']}</td><td>{r['PE_Delta']}</td><td>{r['PE_Gamma']}</td>
          <td>{r['PE_Theta']}</td><td>{r['PE_Vega']}</td>
          <td>{r['Delta_Ratio']}</td><td>{ltp}</td>
          <td>{r['Reference']}</td><td>{r['Stretched']}</td><td>{r['Difference']}</td>
          
        </tr>"""
    return html


def _single_csv_row_html(r, ltp_disp):
    def g(k): return r.get(k, "-")
    return f"""<tr style="background:yellow;color:black;font-weight:bold;">
      <td>{g('DateTime')}</td><td>{g('Expiry')}</td>
      <td>{g('CE_LTP')}</td><td>{g('CE_Delta')}</td><td>{g('CE_Gamma')}</td>
      <td>{g('CE_Theta')}</td><td>{g('CE_Vega')}</td>
      <td><b>{g('Strike')}</b></td>
      <td>{g('PE_LTP')}</td><td>{g('PE_Delta')}</td><td>{g('PE_Gamma')}</td>
      <td>{g('PE_Theta')}</td><td>{g('PE_Vega')}</td>
      <td>{g('Delta_Ratio')}</td><td>{ltp_disp}</td>
      <td>{g('Reference')}</td><td>{g('Stretched')}</td><td>{g('Difference')}</td>
     
    </tr>"""