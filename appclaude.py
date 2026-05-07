from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
import csv, os, shutil, glob, json
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi import Form

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
RUNNING_FILE = "live_running.json"


app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="elenco_secret_123")

# ── Serve static files (dashboard.html lives in ./static/) ──────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Upload storage folder ────────────────────────────────────────────────
UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploaded_csvs")
os.makedirs(UPLOADS_DIR, exist_ok=True)

ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzc2ODI1OTEwLCJpYXQiOjE3NzY3Mzk1MTAsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTAwNTg1OTc1In0.ZN8pMAp9B-85Jt6zhO84F8408R_3D2dFbq03GC21Ia5yoKMzMKF4HNkSvzw89Kg-W0A1_y-NeiKkHcLJ3eFoiQ"
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

    if LAST_DATA["time"] and (datetime.now() - LAST_DATA["time"]).seconds < 1:
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



# ═══════════════════════════════════════
# LOGIN SYSTEM
# ═══════════════════════════════════════

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "12345"

USER_USERNAME = "user"
USER_PASSWORD = "12345"


def render_login(role="admin", error=False):

    path = os.path.join(STATIC_DIR, "finlab", "login.html")

    if not os.path.exists(path):
        return """
        <h2 style='color:red;padding:30px'>
        login.html not found.<br>
        Put file here:<br>
        static/finlab/login.html
        </h2>
        """

    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    # Asset paths
    html = html.replace('href="vendor/', 'href="/static/finlab/vendor/')
    html = html.replace('src="vendor/', 'src="/static/finlab/vendor/')
    html = html.replace('href="css/', 'href="/static/finlab/css/')
    html = html.replace('src="js/', 'src="/static/finlab/js/')
    html = html.replace('href="images/', 'href="/static/finlab/images/')
    html = html.replace('src="images/', 'src="/static/finlab/images/')
    html = html.replace('url(images/', 'url(/static/finlab/images/')

    if role == "admin":
        html = html.replace("Welcome Back", "Admin Login")
        html = html.replace("Sign Me In", "Login as Admin")
        action = "/admin-login"
    else:
        html = html.replace("Welcome Back", "User Login")
        html = html.replace("Sign Me In", "Login as User")
        action = "/user-login"

    html = html.replace(
        '<form action="index.html">',
        f'<form method="post" action="{action}">'
    )

    html = html.replace(
        'type="email" class="form-control" value="hello@example.com"',
        'type="text" name="username" class="form-control" placeholder="Enter Username"'
    )

    html = html.replace(
        'id="dlab-password" class="form-control" value="123456"',
        'name="password" id="dlab-password" class="form-control" placeholder="Enter Password"'
    )

    if error:
        html = html.replace(
            '<form method="post"',
            '''
            <div style="background:#ffdddd;color:red;padding:10px;margin-bottom:15px;border-radius:8px">
            Invalid Login Credentials
            </div>
            <form method="post"
            '''
        )

    return html


# ═══════════════════════════════════════
# ROOT = MAIN ADMIN PAGE
# ═══════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if "admin" not in request.session:
        return RedirectResponse("/admin-login", status_code=302)
    
    path = os.path.join(STATIC_DIR, "admin.html")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# ───── ADMIN LOGIN ─────

@app.get("/admin-login", response_class=HTMLResponse)
def admin_login_page():
    return HTMLResponse(render_login("admin"))

@app.post("/admin-login")
async def admin_login(request: Request,
                      username: str = Form(...),
                      password: str = Form(...)):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        request.session.clear()
        request.session["admin"] = username
        return RedirectResponse("/", status_code=302)

    return HTMLResponse(render_login("admin", True))


@app.get("/recorder-page", response_class=HTMLResponse)
def recorder_page(request: Request):

    if "admin" not in request.session:
        return RedirectResponse("/recorder-login", status_code=302)

    path = os.path.join(STATIC_DIR, "simple.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ───── USER LOGIN ─────

@app.get("/user-login", response_class=HTMLResponse)
def user_login_page():
    return HTMLResponse(render_login("user"))


@app.post("/user-login")
async def user_login(request: Request,
                     username: str = Form(...),
                     password: str = Form(...)):

    if username == USER_USERNAME and password == USER_PASSWORD:
        request.session.clear()
        request.session["user"] = username
        return RedirectResponse("/dashboard", status_code=302)

    return HTMLResponse(render_login("user", True))


# ───── LOGOUT ─────

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/user-login", status_code=302)

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
def dashboard_page(request: Request):

    if "admin" not in request.session and "user" not in request.session:
        return RedirectResponse("/user-login", status_code=302)

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

    found = False

    for i in range(len(LIVE_RUNNING_RECORDS)):
        if LIVE_RUNNING_RECORDS[i].get("datetime") == ts:
            LIVE_RUNNING_RECORDS[i] = data
            found = True
            break

    if not found:
        LIVE_RUNNING_RECORDS.append(data)

    LIVE_RUNNING_RECORDS = LIVE_RUNNING_RECORDS[-500:]

    # Save to file
    with open(RUNNING_FILE, "w") as f:
        json.dump(LIVE_RUNNING_RECORDS, f)

    return JSONResponse({"status": "ok"})


@app.get("/api/get-running")
def get_running():
    global LIVE_RUNNING_RECORDS

    if os.path.exists(RUNNING_FILE):
        try:
            with open(RUNNING_FILE, "r") as f:
                LIVE_RUNNING_RECORDS = json.load(f)
        except:
            LIVE_RUNNING_RECORDS = []

    return JSONResponse({
        "rows": LIVE_RUNNING_RECORDS
    })

@app.post("/api/clear-running")
def clear_running():
    global LIVE_RUNNING_RECORDS

    LIVE_RUNNING_RECORDS = []

    if os.path.exists(RUNNING_FILE):
        os.remove(RUNNING_FILE)

    return JSONResponse({"status": "cleared"})
# ═══════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════

@app.get("/recorder-page", response_class=HTMLResponse)
def recorder_page(request: Request):

    if "admin" not in request.session:
        return RedirectResponse("/user-login", status_code=302)

    path = os.path.join(STATIC_DIR, "simple.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
    return HTMLResponse(
        "<h3 style='color:red;font-family:sans-serif;padding:20px'>"
        "simple.html not found.<br>Place it in the <b>static/</b> folder next to app.py.</h3>",
        status_code=404,
    )


@app.get("/home", response_class=HTMLResponse)
def public_home():
    path = os.path.join(STATIC_DIR, "finlab", "Frontend", "xhtml", "index.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
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