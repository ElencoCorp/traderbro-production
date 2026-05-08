from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import pandas as pd
from datetime import datetime
import csv, os, shutil, glob, json
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
import os
import math
import sqlite3
from passlib.context import CryptContext
from dotenv import load_dotenv
load_dotenv()
CONFIG_FILE = "config.json"
import json

def get_interval():

    try:

        with open(CONFIG_FILE, "r") as f:

            data = json.load(f)

            return int(data.get("interval", 15))

    except:

        return 15


def set_interval(val):

    with open(CONFIG_FILE, "w") as f:

        json.dump({
            "interval": val
        }, f)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password):
    return pwd_context.hash(password)

def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)

DB_FILE = "traderbro.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        password TEXT,
        phone TEXT,
        role TEXT DEFAULT 'user',
        consent BOOLEAN,
        plan TEXT DEFAULT 'free',
        plan_start TEXT,
        plan_expiry TEXT
    )
    """)

    # Create default admin if not exists
    c.execute("SELECT * FROM users WHERE username='admin'")
    if not c.fetchone():
        admin_password = hash_password("admin123")
        c.execute("""
        INSERT INTO users (username, email, password, role, consent, plan)
        VALUES (?, ?, ?, ?, ?, ?)
        """, ("admin", "admin@example.com", admin_password, "admin", True, "pro"))

    conn.commit()
    conn.close()
init_db()

def create_admin():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE role='admin'")
    admin = c.fetchone()

    if not admin:
        hashed_pw = hash_password("admin123")

        c.execute("""
        INSERT INTO users (username, email, password, role)
        VALUES (?, ?, ?, ?)
        """, ("admin", "admin@traderbro.in", hashed_pw, "admin"))

        conn.commit()

    conn.close()

create_admin()


# GLOBALs
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
LOGIN_ATTEMPTS = {}


app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key="TraderBro@2026#Secure$FastAPI",
    https_only=True,  
    same_site="lax"
)

# ── Serve static files (dashboard.html lives in ./static/) ──────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Upload storage folder ────────────────────────────────────────────────
UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploaded_csvs")
os.makedirs(UPLOADS_DIR, exist_ok=True)

# CLIENT_ID  = "1100585975"
# ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwicGFydG5lcklkIjoiIiwiZXhwIjoxNzc4MjIxNzMxLCJpYXQiOjE3NzgxMzUzMzEsInRva2VuQ29uc3VtZXJUeXBlIjoiU0VMRiIsIndlYmhvb2tVcmwiOiIiLCJkaGFuQ2xpZW50SWQiOiIxMTAwNTg1OTc1In0.o96GqSjFN2Q7kYzzFnzN80KFZk7ju6xDOu9xy1jPQTKDqfi9gEu0AKGLiiJo_niknfMixKMQX3-7yVdOxlkRCg"
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
CLIENT_ID  = os.getenv("CLIENT_ID")
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

# VPS AUTO RECORDER
AUTO_RUNNING = False

def auto_market_recorder():

    global AUTO_RUNNING

    now = datetime.now()

    hour = now.hour
    minute = now.minute

    current = (hour * 60) + minute

    start_time = (9 * 60) + 16
    end_time = (15 * 60) + 30

    # START
    if start_time <= current <= end_time:

        if not AUTO_RUNNING:

            print("🚀 MARKET RECORDER STARTED")

            AUTO_RUNNING = True

        try:

            expiry_list = get_expiries()

            if not expiry_list:
                return

            expiry = expiry_list[0]

            ltp, df, atm = get_live_chain(expiry)

            if df.empty or atm is None:
                return

            atm_row = df[df["Strike"] == atm]

            if atm_row.empty:
                return

            r = atm_row.iloc[0]

            global LIVE_RUNNING_RECORDS

            current_diff = r["Difference"]

            if current_diff is None:
                current_diff = 0

            row = {
                "datetime": str(r["DateTime"]),
                "expiry": str(r["Expiry"]),
                "ce_ltp": r["CE_LTP"],
                "ce_delta": r["CE_Delta"],
                "ce_gamma": r["CE_Gamma"],
                "ce_theta": r["CE_Theta"],
                "ce_vega": r["CE_Vega"],
                "strike": int(r["Strike"]),
                "pe_ltp": r["PE_LTP"],
                "pe_delta": r["PE_Delta"],
                "pe_gamma": r["PE_Gamma"],
                "pe_theta": r["PE_Theta"],
                "pe_vega": r["PE_Vega"],
                "delta_ratio": r["Delta_Ratio"],
                "index_ltp": ltp,
                "reference": r["Reference"],
                "stretched": r["Stretched"],
                "difference": current_diff,
                "diff_prev": 0,
                "running": 0
            }

            # PREVIOUS ROW LOGIC
            if len(LIVE_RUNNING_RECORDS) > 0:

                prev = LIVE_RUNNING_RECORDS[-1]

                prev_diff = current_diff - prev["difference"]

                prev["diff_prev"] = round(prev_diff, 2)

                prev["running"] = round(
                    prev.get("running", 0) + prev_diff,
                    2
                )

            LIVE_RUNNING_RECORDS.append(row)

            LIVE_RUNNING_RECORDS = LIVE_RUNNING_RECORDS[-500:]

            with open(RUNNING_FILE, "w") as f:
                json.dump(LIVE_RUNNING_RECORDS, f)

            print("✅ SAVED:", row["datetime"])

        except Exception as e:
            print("AUTO RECORDER ERROR:", e)

    else:

        if AUTO_RUNNING:
            print("⛔ MARKET RECORDER STOPPED")

        AUTO_RUNNING = False

# login

def load_login_template(role="admin", error=False):
    path = os.path.join(STATIC_DIR, "finlab", "login.html")

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
        html = html.replace(
            "Log in to your admin dashboard with your credentials",
            "Secure admin access to TraderBro control panel"
        )
        html = html.replace(
            "The Evolution of <span>Finlab</span>",
            "Welcome to <span>TraderBro</span>"
        )
        action = "/admin-login"
    else:
        action = "/user-login"

    html = html.replace(
        '<form method="POST" action="/user-login">',
        f'<form method="POST" action="{action}">'
    )

    html = html.replace(
        'name="identifier"',
        'name="username"'
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

@app.get("/api/me")
def get_current_user(request: Request):

    if "user" not in request.session:
        return {"username": None}

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        SELECT username, email, phone, plan, plan_start, plan_expiry
        FROM users
        WHERE username=?
    """, (request.session["user"],))

    user = c.fetchone()

    conn.close()

    if user:
        return {
            "username": user[0],
            "email": user[1],
            "phone": user[2],
            "plan": user[3],
            "plan_start": user[4],
            "plan_expiry": user[5]
        }

    return {"username": None}



@app.post("/update-profile")
async def update_profile(request: Request):

    if "user" not in request.session:
        return JSONResponse({
            "success": False,
            "message": "Unauthorized"
        })

    data = await request.json()

    username = data.get("username")
    phone = data.get("phone")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        UPDATE users
        SET username=?,
            phone=?
        WHERE username=?
    """, (
        username,
        phone,
        request.session["user"]
    ))

    conn.commit()
    conn.close()

    # UPDATE SESSION
    request.session["user"] = username

    return JSONResponse({
        "success": True
    })

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")

@app.get("/admin-login", response_class=HTMLResponse)
def admin_login_page():
    return HTMLResponse(load_login_template("admin"))

@app.post("/admin-login")
async def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE username=? AND role='admin'", (username,))
    admin = c.fetchone()

    conn.close()

    if admin and verify_password(password, admin[3]):

        request.session["admin"] = username
        request.session["role"] = "admin"

        return RedirectResponse(url="/admin", status_code=303)

    return HTMLResponse(load_login_template("admin", True))

@app.get("/admin", response_class=HTMLResponse)
def admin_panel(request: Request):

    if request.session.get("role") != "admin":
        return RedirectResponse("/admin-login", status_code=302)

    path = os.path.join(STATIC_DIR, "admin.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# USER LOGIN PAGE
@app.get("/user-login", response_class=HTMLResponse)
def login_page():
    try:
        path = os.path.join(
            STATIC_DIR,
            "finlab",
            "login.html"   # or login.html if that’s your file
        )

        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    except Exception as e:
        return HTMLResponse(f"LOGIN PAGE ERROR: {str(e)}", status_code=500)

@app.post("/user-login")
async def login(request: Request):

    form = await request.form()

    identifier = form.get("identifier")
    password = form.get("password")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute(
        """
        SELECT username, password
        FROM users
        WHERE username=? OR email=?
        """,
        (identifier, identifier)
    )

    user = c.fetchone()

    conn.close()

    # INVALID LOGIN
    if not user:
        return RedirectResponse(
            url="/user-login?error=invalid",
            status_code=303
        )

    # PASSWORD CHECK
    if not verify_password(password, user[1]):

        return RedirectResponse(
            url="/user-login?error=invalid",
            status_code=303
        )

    # SUCCESS LOGIN
    request.session["user"] = user[0]

    return RedirectResponse(
        url="/dashboard",
        status_code=303
    )

@app.post("/register-user")
async def register_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    phone: str = Form(...),
    consent: str = Form(...)
):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # ✅ FIX HERE (INSIDE FUNCTION BODY)
        consent_value = True if consent == "1" else False

        # EMAIL CHECK
        c.execute("SELECT * FROM users WHERE email=?", (email,))
        if c.fetchone():
            return RedirectResponse("/register?error=email", status_code=303)

        # USERNAME CHECK
        c.execute("SELECT * FROM users WHERE username=?", (username,))
        if c.fetchone():
            return RedirectResponse("/register?error=username", status_code=303)

        # PASSWORD VALIDATION
        if len(password) < 8 or not any(c.isupper() for c in password) or not any(c.isdigit() for c in password):
            return RedirectResponse("/register?error=password", status_code=303)

        hashed_pw = hash_password(password)

        c.execute("""
        INSERT INTO users (username, email, password, phone, consent)
        VALUES (?, ?, ?, ?, ?)
        """, (username, email, hashed_pw, phone, consent_value))

        conn.commit()
        conn.close()

        # ✅ AUTO LOGIN (SESSION CREATE)
        request.session.clear()
        request.session["user"] = username
        request.session["role"] = "user"

        return RedirectResponse("/", status_code=303)

    except Exception as e:
        print("REGISTER ERROR:", e)
        return HTMLResponse("Internal Server Error", status_code=500)

# Dashboard
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):

    if "user" not in request.session and "admin" not in request.session:
        return RedirectResponse("/user-login", status_code=302)

    # ADMIN ACCESS
    if "admin" in request.session:

        path = os.path.join(STATIC_DIR, "dashboard.html")

        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    username = request.session["user"]

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        SELECT plan, plan_expiry
        FROM users
        WHERE username=?
    """, (username,))

    user = c.fetchone()

    conn.close()

    if not user:
        return RedirectResponse("/user-login")

    plan = user[0]
    expiry = user[1]

    # NO PLAN
    if not plan or plan == "free":
        return RedirectResponse("/trading-plan")

    # EXPIRY CHECK
    if expiry:

        expiry_date = datetime.fromisoformat(expiry)

        if datetime.now() > expiry_date:
            return RedirectResponse("/trading-plan")

    path = os.path.join(STATIC_DIR, "dashboard.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# My Account
@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    # ✅ Check login
    if "user" not in request.session and "admin" not in request.session:
        return RedirectResponse("/user-login", status_code=302)

    path = os.path.join(STATIC_DIR, "finlab", "user.html")

    with open(path, "r", encoding="utf-8") as f:
        return f.read()

#Checkout
@app.get("/checkout", response_class=HTMLResponse)
def checkout_page(request: Request):

    if "user" not in request.session:
        return RedirectResponse("/user-login")

    path = os.path.join(STATIC_DIR, "finlab", "ecom-checkout.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/activate-plan")
async def activate_plan(request: Request):

    if "user" not in request.session:
        return JSONResponse({"success": False})

    data = await request.json()

    plan = data.get("plan")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    from datetime import datetime, timedelta

    plan_days = {
        "basic": 1,
        "essential": 5,
        "pro": 22,
        "premium": 250
    }

    days = plan_days.get(plan, 30)

    start_date = get_subscription_start_date()

    expiry = start_date

    added = 1

    while added < days:

        expiry += timedelta(days=1)

        # Skip weekends
        if expiry.weekday() >= 5:
            continue

        added += 1

    # MARKET END TIME → 12 PM
    expiry = expiry.replace(
        hour=15,
        minute=30,
        second=0,
        microsecond=0
    )
    c.execute("""
        UPDATE users
        SET plan=?,
            plan_start=?,
            plan_expiry=?
        WHERE username=?
    """, (
        plan,
        start_date.isoformat(),
        expiry.isoformat(),
        request.session["user"]
    ))

    conn.commit()
    conn.close()

    return JSONResponse({
        "success": True
    })


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

# Workign Build_df_from_oc
# def build_df_from_oc(ltp, oc, expiry, dt_label):
#     rows = []
#     for strike, val in oc.items():
#         ce   = val.get("ce", {});  pe   = val.get("pe", {})
#         ce_g = ce.get("greeks") or {}; pe_g = pe.get("greeks") or {}
#         try:
#             ratio = round((float(pe_g["delta"]) / float(ce_g["delta"])) * -1, 5)
#         except:
#             ratio = None
#         rows.append({
#             "DateTime": dt_label, "Expiry": expiry, "Strike": float(strike),
#             "CE_LTP":    ce.get("last_price", "-"), "CE_Delta": ce_g.get("delta", "-"),
#             "CE_Gamma":  ce_g.get("gamma", "-"),    "CE_Theta": ce_g.get("theta", "-"),
#             "CE_Vega":   ce_g.get("vega", "-"),
#             "PE_LTP":    pe.get("last_price", "-"), "PE_Delta": pe_g.get("delta", "-"),
#             "PE_Gamma":  pe_g.get("gamma", "-"),    "PE_Theta": pe_g.get("theta", "-"),
#             "PE_Vega":   pe_g.get("vega", "-"),
#             "Delta_Ratio": ratio,
#         })

#     df = pd.DataFrame(rows).sort_values("Strike").reset_index(drop=True)
#     if df.empty:
#         return df, None

#     df["diff"] = abs(df["Strike"] - ltp)
#     atm_idx    = df["diff"].idxmin()
#     atm_strike = df.loc[atm_idx, "Strike"]

#     df = df.iloc[max(atm_idx - 10, 0): atm_idx + 11].reset_index(drop=True)
#     df["diff"] = abs(df["Strike"] - ltp)
#     atm_idx    = df["diff"].idxmin()

#     # =========================
#     # REFERENCE (ROW-WISE FIX)
#     # =========================
#     df["Reference"] = None

#     for i in range(len(df)):
#         try:
#             if i == 0 or i == len(df) - 1:
#                 continue

#             prev_val = df.loc[i - 1, "Delta_Ratio"]
#             next_val = df.loc[i + 1, "Delta_Ratio"]

#             if isinstance(prev_val, float) and isinstance(next_val, float):
#                 ref = ((prev_val + next_val) / 2) - 0.06
#                 df.loc[i, "Reference"] = round(ref, 5)

#         except:
#             continue

#     # =========================
#     # STRETCHED (ROW-WISE FIX)
#     # =========================
#     df["Stretched"] = None

#     for i in range(len(df)):
#         try:
#             curr_dr = df.loc[i, "Delta_Ratio"]
#             curr_ref = df.loc[i, "Reference"]

#             if curr_ref == "0.00000" or curr_ref is None:
#                 continue

#             curr_ref = float(curr_ref)

#             if i < 2:
#                 continue

#             prev1 = df.loc[i - 1, "Delta_Ratio"]
#             prev2 = df.loc[i - 2, "Delta_Ratio"]

#             try:
#                 curr_dr = float(curr_dr)
#                 prev1 = float(prev1)
#                 prev2 = float(prev2)
#             except:
#                 continue

#             denom = (prev1 - prev2) / 100

#             if denom == 0:
#                 continue

#             stretched_val = df.loc[i, "Strike"] - ((curr_dr - curr_ref) / denom)
#             df.loc[i, "Stretched"] = f"{stretched_val:.5f}"

#         except:
#             continue

#     df["Stretched"] = df["Stretched"].fillna("")

#     def calc_diff(s):
#         try:
#             if s == "" or s is None:
#                 return ""
#             return round(float(s) - ltp, 2)
#         except:
#             return ""

#     df["Difference"] = df["Stretched"].apply(calc_diff)

#     return df, atm_strike

def build_df_from_oc(ltp, oc, expiry, dt_label):
    rows = []
    for strike, val in oc.items():
        ce  = val.get("ce", {})
        pe  = val.get("pe", {})
        ce_g = ce.get("greeks") or {}
        pe_g = pe.get("greeks") or {}

        def to_float(v):
            try:
                f = float(v)
                if math.isnan(f) or math.isinf(f):
                    return None
                return f
            except:
                return None

        ce_delta = to_float(ce_g.get("delta"))
        pe_delta = to_float(pe_g.get("delta"))

        # Delta ratio — only compute if both deltas are valid and non-zero
        try:
            # if ce_delta and pe_delta and ce_delta != 0:
            if ce_delta is not None and pe_delta is not None and ce_delta != 0:
                ratio = round((pe_delta / ce_delta) * -1, 5)
                if math.isnan(ratio) or math.isinf(ratio):
                    ratio = None
            else:
                ratio = None
        except:
            ratio = None

        rows.append({
            "DateTime":    dt_label,
            "Expiry":      expiry,
            "Strike":      float(strike),
            "CE_LTP":      to_float(ce.get("last_price")) or 0,
            "CE_Delta":    ce_delta,
            "CE_Gamma":    to_float(ce_g.get("gamma")),
            "CE_Theta":    to_float(ce_g.get("theta")),
            "CE_Vega":     to_float(ce_g.get("vega")),
            "PE_LTP":      to_float(pe.get("last_price")) or 0,
            "PE_Delta":    pe_delta,
            "PE_Gamma":    to_float(pe_g.get("gamma")),
            "PE_Theta":    to_float(pe_g.get("theta")),
            "PE_Vega":     to_float(pe_g.get("vega")),
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

    # ── REFERENCE ──────────────────────────────────────────────────────
    df["Reference"] = None
    for i in range(1, len(df) - 1):
        try:
            prev_val = df.loc[i - 1, "Delta_Ratio"]
            next_val = df.loc[i + 1, "Delta_Ratio"]
            if prev_val is not None and next_val is not None:
                ref = ((float(prev_val) + float(next_val)) / 2) - 0.06
                if not math.isnan(ref) and not math.isinf(ref):
                    df.loc[i, "Reference"] = round(ref, 5)
        except:
            continue

    # ── STRETCHED ──────────────────────────────────────────────────────
    df["Stretched"] = None
    for i in range(2, len(df)):
        try:
            curr_dr  = df.loc[i,     "Delta_Ratio"]
            curr_ref = df.loc[i,     "Reference"]
            prev1    = df.loc[i - 1, "Delta_Ratio"]
            prev2    = df.loc[i - 2, "Delta_Ratio"]

            # Skip if any required value is missing
            if any(v is None for v in [curr_dr, curr_ref, prev1, prev2]):
                continue

            curr_dr  = float(curr_dr)
            curr_ref = float(curr_ref)
            prev1    = float(prev1)
            prev2    = float(prev2)

            denom = (prev1 - prev2) / 100
            if denom == 0:
                continue

            stretched_val = df.loc[i, "Strike"] - ((curr_dr - curr_ref) / denom)

            if math.isnan(stretched_val) or math.isinf(stretched_val):
                continue

            df.loc[i, "Stretched"] = round(stretched_val, 5)
        except:
            continue

    # ── DIFFERENCE ─────────────────────────────────────────────────────
    def calc_diff(s):
        try:
            if s is None:
                return None
            f = float(s)
            if math.isnan(f) or math.isinf(f):
                return None
            return round(f - ltp, 2)
        except:
            return None

    df["Difference"] = df["Stretched"].apply(calc_diff)

    return df, atm_strike

CACHE_SECONDS = 15  # minimum gap between real API calls

def is_market_open():

    now = datetime.now()

    current_minutes = (
        now.hour * 60
    ) + now.minute

    start_minutes = (9 * 60) + 16
    end_minutes   = (15 * 60) + 30

    return (
        current_minutes >= start_minutes
        and
        current_minutes <= end_minutes
    )


def get_live_chain(expiry):
    print("🔥 FETCHING NEW DATA FROM API")
    global LAST_DATA

    # MARKET CLOSED
    if not is_market_open():

        cached = LAST_DATA.get("data")

        if cached:
            return cached

        return (0, pd.DataFrame(), None)

    now = datetime.now()
    cached = LAST_DATA.get("data")
    cached_time = LAST_DATA.get("time")

    # Return cached data if it's fresh enough
    if cached and cached_time:
        if (now - cached_time).total_seconds() < 2:
            return cached

    try:
        ltp, oc  = fetch_live_option_chain(expiry)
        dt_label = now.strftime("%Y-%m-%d %H:%M:%S")
        df, atm  = build_df_from_oc(ltp, oc, expiry, dt_label)

        LAST_DATA["time"] = now
        LAST_DATA["data"] = (ltp, df, atm)

        return ltp, df, atm

    except Exception as e:
        print("🔥 FULL ERROR:", str(e))
        import traceback
        traceback.print_exc()

        # Return stale cache rather than empty on rate-limit error
        if cached:
            print("⚠️ Returning stale cached data due to error")
            return cached

        return (0, pd.DataFrame(), None)


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


def restart_market_job():

    try:
        scheduler.remove_job("market_auto_job")

    except:
        pass

    # scheduler.add_job(
    #     auto_market_recorder,
    #     "interval",
    #     seconds=max(get_interval(), 5),
    #     id="market_auto_job",
    #     replace_existing=True
    # )
# restart_market_job()
    scheduler.add_job(
        auto_market_recorder,
        "interval",
        seconds=max(get_interval(), 5),
        id="market_auto_job",
        replace_existing=True
    )

    print("✅ VPS MARKET WORKER STARTED")

restart_market_job()    

# Interval
@app.get("/api/get-interval")
def api_get_interval():

    return {
        "interval": get_interval()
    }


@app.post("/api/set-interval")
async def api_set_interval(request: Request):

    # ADMIN ONLY
    if "admin" not in request.session:

        return JSONResponse({
            "error": "Unauthorized"
        }, status_code=401)

    data = await request.json()

    interval = int(data.get("interval", 15))

    # SAFETY
    if interval < 1:
        interval = 1

    set_interval(interval)
    restart_market_job()

    return {
        "success": True,
        "interval": interval
    }


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
def simple_page(request: Request):

    # 🔐 Require login
    if "user" not in request.session:
        return RedirectResponse("/user-login")

    path = os.path.join(STATIC_DIR, "simple.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
    
# @app.get("/dashboard", response_class=HTMLResponse)
# def dashboard_page():
#     path = os.path.join(STATIC_DIR, "dashboard.html")
#     with open(path, "r", encoding="utf-8") as f:
#         return HTMLResponse(f.read())


#Html file frontend finlab

# ═══════════════════════════════════════════════════════════════════════
# API — LIVE DATA
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/expiries")
def api_expiries():
    return JSONResponse(get_expiries())


@app.get("/api/live-data")
def live_data():

    try:

        if os.path.exists(RUNNING_FILE):

            with open(RUNNING_FILE, "r") as f:

                rows = json.load(f)

                return {
                    "rows": rows[-100:]
                }

        return {
            "rows": []
        }

    except Exception as e:

        return {
            "rows": [],
            "error": str(e)
        }

# ═══════════════════════════════════════════════════════════════════════
# API — FULL CHAIN (all columns, all rows) — used by admin AJAX refresh
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/full-chain")
def api_full_chain(expiry: str = ""):
        # MARKET CLOSED
    # if not is_market_open():

    #     return JSONResponse({
    #         "market_closed": True,
    #         "message": "Values are visible only during market hours (9:16 AM to 12:00 PM)"
    #     })
    """Return all 21 rows with every column for the admin table AJAX refresh."""
    if not expiry:
        expiries = get_expiries()
        expiry   = expiries[0] if expiries else ""
    if not expiry:
        return JSONResponse({"error": "No expiry available"})

    ltp, df, atm = get_live_chain(expiry)

    if df.empty:
        return JSONResponse({"error": "No data", "ltp": ltp})

    def safe(val):
        """Convert NaN/inf/None to None for JSON safety."""
        try:
            if val is None:
                return None
            f = float(val)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        except:
            return str(val) if val not in [None, "", "-"] else None

    # ✅ rows loop is NOW outside safe() — correct indentation
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "datetime":    str(r["DateTime"]),
            "expiry":      str(r["Expiry"]),
            "strike":      int(r["Strike"]),
            "ce_ltp":      safe(r["CE_LTP"]),
            "ce_delta":    safe(r["CE_Delta"]),
            "ce_gamma":    safe(r["CE_Gamma"]),
            "ce_theta":    safe(r["CE_Theta"]),
            "ce_vega":     safe(r["CE_Vega"]),
            "pe_ltp":      safe(r["PE_LTP"]),
            "pe_delta":    safe(r["PE_Delta"]),
            "pe_gamma":    safe(r["PE_Gamma"]),
            "pe_theta":    safe(r["PE_Theta"]),
            "pe_vega":     safe(r["PE_Vega"]),
            "delta_ratio": safe(r["Delta_Ratio"]),
            "index_ltp":   safe(ltp),
            "reference":   safe(r["Reference"]),
            "stretched":   str(r["Stretched"]) if r["Stretched"] not in [None, ""] else None,
            "difference":  safe(r["Difference"]),
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

        # if not is_market_open():

        #     return JSONResponse({
        #         "error": "Market closed"
        #     })

        expiries = get_expiries()

        if not expiry:
            expiry = expiries[0] if expiries else ""

        if not expiry:
            return JSONResponse({
                "error": "No expiry"
            })

        ltp, df, atm = get_live_chain(expiry)
        print(df.head())
        print("ATM =", atm)
        print("LTP =", ltp)

        if df.empty or atm is None:
            return JSONResponse({
                "error": "No live data"
            })

        atm_row = df[df["Strike"] == atm]

        if atm_row.empty:
            return JSONResponse({
                "error": "ATM not found"
            })

        r = atm_row.iloc[0]

        return JSONResponse({

            "datetime": str(r["DateTime"]),
            "expiry": str(r["Expiry"]),

            "ce_ltp": r["CE_LTP"],
            "ce_delta": r["CE_Delta"],
            "ce_gamma": r["CE_Gamma"],
            "ce_theta": r["CE_Theta"],
            "ce_vega": r["CE_Vega"],

            "strike": int(r["Strike"]),

            "pe_ltp": r["PE_LTP"],
            "pe_delta": r["PE_Delta"],
            "pe_gamma": r["PE_Gamma"],
            "pe_theta": r["PE_Theta"],
            "pe_vega": r["PE_Vega"],

            "delta_ratio": r["Delta_Ratio"],
            "index_ltp": ltp,

            "reference": r["Reference"],
            "stretched": r["Stretched"],

            "difference": r["Difference"]

        })

    except Exception as e:

        print("ERROR:", e)

        return JSONResponse({
            "error": str(e)
        })


# ═══════════════════════════════════════════════════════════════════════
# API — CSV UPLOAD / DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════

# @app.post("/admin/upload-csv")
# async def upload_csv(file: UploadFile = File(...), label: str = Form("")):
#     safe = label.replace("/", "-").replace(" ", "_") or datetime.now().strftime("%Y-%m-%d")
#     dest = os.path.join(UPLOADS_DIR, f"{safe}.csv")
#     with open(dest, "wb") as f:
#         shutil.copyfileobj(file.file, f)
#     return JSONResponse({"status": "uploaded", "file": f"{safe}.csv"})


# @app.delete("/admin/delete-csv/{filename}")
# def delete_uploaded(filename: str):
#     p = os.path.join(UPLOADS_DIR, filename)
#     if os.path.exists(p):
#         os.remove(p)
#         return JSONResponse({"status": "deleted"})
#     return JSONResponse({"status": "not_found"}, status_code=404)


# @app.get("/admin/list-csvs")
# def list_csvs():
#     files = sorted(glob.glob(os.path.join(UPLOADS_DIR, "*.csv")), reverse=True)
#     return JSONResponse([
#         {"name": os.path.basename(f), "size": os.path.getsize(f)}
#         for f in files
#     ])


@app.get("/user/download-csv/{filename}")
def user_download(filename: str):
    p = os.path.join(UPLOADS_DIR, filename)
    if os.path.exists(p):
        return FileResponse(p, media_type="text/csv", filename=filename)
    return JSONResponse({"error": "File not found"}, status_code=404)

@app.post("/api/save-running")
async def save_running(request: Request):

    try:

        data = await request.json()

        rows = []

        if os.path.exists(RUNNING_FILE):

            with open(RUNNING_FILE, "r") as f:

                rows = json.load(f)

        # PREVENT DUPLICATE TIMESTAMP
        if len(rows) > 0:

            last = rows[-1]

            if last["datetime"] == data["datetime"]:
                return {"success": True}

        rows.append(data)

        # LIMIT
        rows = rows[-500:]

        with open(RUNNING_FILE, "w") as f:

            json.dump(rows, f)

        return {
            "success": True
        }

    except Exception as e:

        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)


from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

def get_subscription_start_date():

    now = datetime.now(IST)

    current_minutes = (now.hour * 60) + now.minute

    # MARKET CLOSE = 12 PM
    market_close = (15 * 60) + 30

    # BEFORE OR DURING MARKET
    if current_minutes <= market_close:

        start_date = now

    else:

        # AFTER MARKET CLOSE → NEXT TRADING DAY
        start_date = now + timedelta(days=1)

        # SKIP WEEKENDS
        while start_date.weekday() >= 5:
            start_date += timedelta(days=1)

    # FORCE MARKET START TIME
    start_date = start_date.replace(
        hour=9,
        minute=16,
        second=0,
        microsecond=0
    )

    return start_date

@app.get("/api/get-running")
def get_running():

    try:

        if os.path.exists(RUNNING_FILE):

            with open(RUNNING_FILE, "r") as f:

                rows = json.load(f)

                return {
                    "rows": rows[-500:]
                }

        return {
            "rows": []
        }

    except Exception as e:

        return {
            "rows": [],
            "error": str(e)
        }


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
def home_page():
    try:
        path = os.path.join(
            STATIC_DIR,
            "finlab",
            "Frontend",
            "xhtml",
            "index.html"
        )

        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    except Exception as e:
        return HTMLResponse(f"ERROR: {str(e)}", status_code=500)


# About us page
@app.get("/about-us", response_class=HTMLResponse)
def about_page():
    path = os.path.join(STATIC_DIR, "finlab", "Frontend", "xhtml", "about-us.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
    
# Trading plan page
@app.get("/trading-plan", response_class=HTMLResponse)
def trading_plan_page(request: Request):
    path = os.path.join(STATIC_DIR, "finlab", "Frontend", "xhtml", "trading-plan.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# Page-Register Page
@app.get("/register", response_class=HTMLResponse)
def register_page():
    path = os.path.join(STATIC_DIR, "finlab", "page-register.html")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ═══════════════════════════════════════════════════════════════════════
# TABLE HELPERS
# ══════════════════════════════════

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
