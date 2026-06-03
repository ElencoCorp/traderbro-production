import os, hmac, hashlib, sqlite3, json, razorpay
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# ── Credentials (from .env via load_dotenv() in app.py) ─────────────────────
RZP_KEY_ID     = os.getenv("RAZORPAY_KEY_ID",     "")
RZP_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

# ── Lazy Razorpay client ──────────────────────────────────────────────────────
_rzp_client = None
def get_rzp_client():
    global _rzp_client
    if _rzp_client is None:
        if not RZP_KEY_ID or not RZP_KEY_SECRET:
            raise RuntimeError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in .env")
        _rzp_client = razorpay.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))
    return _rzp_client

# ── DB / IST helpers (read at call-time so import order doesn't matter) ───────
def _db():
    from app import DB_FILE
    return sqlite3.connect(DB_FILE)

def _ist():
    import pytz
    return pytz.timezone("Asia/Kolkata")

# ── Plan prices (must match PLAN_CONFIG_DATA in app.py) ──────────────────────
PLAN_PRICES = {
    "basic":     99,
    "essential": 399,
    "pro":       1499,
    "premium":   14499,
}

# ── Method display helpers ────────────────────────────────────────────────────
METHOD_ICON = {
    "upi":         "📱",
    "card":        "💳",
    "netbanking":  "🏦",
    "wallet":      "👛",
    "emi":         "🔄",
    "paylater":    "🕐",
    "unknown":     "💰",
}
METHOD_LABEL = {
    "upi":        "UPI",
    "card":       "Card",
    "netbanking": "Net Banking",
    "wallet":     "Wallet",
    "emi":        "EMI",
    "paylater":   "Pay Later",
    "unknown":    "Online",
}

# ═══════════════════════════════════════════════════════════════════════════════
router = APIRouter()


# ── Table init — called explicitly from app.py AFTER app is ready ─────────────
def init_razorpay_table():
    """
    Creates razorpay_orders table.  Safe to call multiple times.
    Adds payment_method / payment_detail columns if they don't exist yet
    (handles existing DBs from earlier version without migration).
    """
    conn = _db()
    c    = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS razorpay_orders (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        username         TEXT    NOT NULL,
        plan             TEXT    NOT NULL,
        rzp_order_id     TEXT    UNIQUE NOT NULL,
        rzp_payment_id   TEXT,
        rzp_signature    TEXT,
        amount_paise     INTEGER NOT NULL,
        currency         TEXT    DEFAULT 'INR',
        status           TEXT    DEFAULT 'created',
        payment_method   TEXT    DEFAULT '',
        payment_detail   TEXT    DEFAULT '',
        created_at       TEXT,
        verified_at      TEXT
    )
    """)

    # ── Safely add columns to existing tables (ALTER TABLE is idempotent) ──
    for col, default in [("payment_method", "''"), ("payment_detail", "''")]:
        try:
            c.execute(f"ALTER TABLE razorpay_orders ADD COLUMN {col} TEXT DEFAULT {default}")
        except Exception:
            pass   # column already exists — ignore

    conn.commit()
    conn.close()


# ── Fetch payment method from Razorpay and cache it ──────────────────────────
def _fetch_and_cache_payment_method(rzp_payment_id: str, db_row_id: int) -> dict:
    """
    Calls Razorpay /payments/{id} and extracts human-readable method + detail.
    Caches result in DB so we never call the API twice for the same payment.
    Returns {"method": "upi", "detail": "user@okaxis", "icon": "📱", "label": "UPI"}
    """
    result = {"method": "unknown", "detail": "", "icon": "💰", "label": "Online"}
    if not rzp_payment_id or not rzp_payment_id.startswith("pay_"):
        return result

    try:
        rzp     = get_rzp_client()
        payment = rzp.payment.fetch(rzp_payment_id)

        method = (payment.get("method") or "unknown").lower()
        detail = ""

        if method == "upi":
            detail = payment.get("vpa") or ""          # e.g. user@okaxis
        elif method == "card":
            card = payment.get("card") or {}
            network = card.get("network") or ""        # Visa / Mastercard / RuPay
            last4   = card.get("last4")  or "****"
            ctype   = card.get("type")   or ""         # credit / debit
            detail  = f"{network} {ctype} ····{last4}".strip()
        elif method == "netbanking":
            detail = payment.get("bank") or ""         # HDFC / SBI / ICICI
        elif method == "wallet":
            detail = payment.get("wallet") or ""       # paytm / phonepe / amazonpay
        elif method == "emi":
            card = payment.get("card") or {}
            detail = f"EMI · {card.get('network','')} ····{card.get('last4','****')}".strip(" ·")

        result = {
            "method": method,
            "detail": detail,
            "icon":   METHOD_ICON.get(method, "💰"),
            "label":  METHOD_LABEL.get(method, method.title()),
        }

        # Cache in DB
        conn = _db()
        c    = conn.cursor()
        c.execute("""
            UPDATE razorpay_orders
            SET payment_method=?, payment_detail=?
            WHERE id=?
        """, (method, detail, db_row_id))
        conn.commit()
        conn.close()

    except Exception as e:
        print(f"[Razorpay] fetch payment method failed for {rzp_payment_id}: {e}")

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# POST /api/create-order
# ═══════════════════════════════════════════════════════════════════════════════
@router.post("/api/create-order")
async def create_order(request: Request):
    """
    Creates a Razorpay order.
    Returns key_id (public) + order metadata.  KEY_SECRET never leaves backend.
    """
    username = request.session.get("user") or request.session.get("admin")
    if not username:
        return JSONResponse({"error": "Login required"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    plan = (body.get("plan") or "").lower().strip()
    if plan not in PLAN_PRICES:
        return JSONResponse({"error": "Invalid plan"}, status_code=400)

    amount_paise = PLAN_PRICES[plan] * 100
    if amount_paise < 100:
        return JSONResponse({"error": "Amount below minimum (₹1)"}, status_code=400)

    # User details for pre-fill
    conn = _db()
    c    = conn.cursor()
    c.execute("SELECT email, phone FROM users WHERE username=?", (username,))
    row  = c.fetchone()
    conn.close()
    email = (row[0] if row else "") or ""
    phone = (row[1] if row else "") or ""

    # Create Razorpay order
    try:
        rzp   = get_rzp_client()
        ts    = datetime.now(_ist()).strftime("%Y%m%d%H%M%S")
        order = rzp.order.create({
            "amount":   amount_paise,
            "currency": "INR",
            "receipt":  f"tb_{username[:8]}_{plan}_{ts}",
            "notes":    {"username": username, "plan": plan},
        })
    except Exception as e:
        return JSONResponse({"error": f"Razorpay order creation failed: {e}"}, status_code=500)

    now_iso = datetime.now(_ist()).isoformat()
    try:
        conn = _db()
        c    = conn.cursor()
        c.execute("""
            INSERT INTO razorpay_orders
                (username, plan, rzp_order_id, amount_paise, currency, status, created_at)
            VALUES (?,?,?,?,'INR','created',?)
        """, (username, plan, order["id"], amount_paise, now_iso))
        conn.commit()
        conn.close()
    except Exception as e:
        return JSONResponse({"error": f"DB save failed: {e}"}, status_code=500)

    return JSONResponse({
        "key_id":   RZP_KEY_ID,       # PUBLIC key only
        "order_id": order["id"],
        "amount":   amount_paise,
        "currency": "INR",
        "plan":     plan,
        "username": username,
        "email":    email,
        "phone":    phone,
        "name":     username,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# POST /api/verify-payment
# ═══════════════════════════════════════════════════════════════════════════════
@router.post("/api/verify-payment")
async def verify_payment(request: Request):
    """
    1. HMAC-SHA256 signature check
    2. Activate plan (with queue support)
    3. Fetch + cache payment method from Razorpay
    """
    username = request.session.get("user") or request.session.get("admin")
    if not username:
        return JSONResponse({"error": "Login required"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    rzp_payment_id = (body.get("razorpay_payment_id") or "").strip()
    rzp_order_id   = (body.get("razorpay_order_id")   or "").strip()
    rzp_signature  = (body.get("razorpay_signature")  or "").strip()

    if not all([rzp_payment_id, rzp_order_id, rzp_signature]):
        return JSONResponse({"error": "Missing payment fields"}, status_code=400)

    # Validate order belongs to this user
    conn = _db()
    c    = conn.cursor()
    c.execute("""
        SELECT id, plan, amount_paise, status
        FROM razorpay_orders
        WHERE rzp_order_id=? AND username=?
    """, (rzp_order_id, username))
    order_row = c.fetchone()
    conn.close()

    if not order_row:
        return JSONResponse({"error": "Order not found or not yours"}, status_code=403)

    # Idempotency
    if order_row[3] == "paid":
        return JSONResponse({"success": True, "already_paid": True})

    db_row_id = order_row[0]
    plan      = order_row[1]

    # ── HMAC-SHA256 verification ──────────────────────────────────────────────
    expected = hmac.new(
        RZP_KEY_SECRET.encode(),
        f"{rzp_order_id}|{rzp_payment_id}".encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, rzp_signature):
        conn = _db(); c = conn.cursor()
        c.execute("UPDATE razorpay_orders SET status='sig_mismatch' WHERE id=?", (db_row_id,))
        conn.commit(); conn.close()
        return JSONResponse({"error": "Signature mismatch — payment not verified"}, status_code=400)

    # ── Activate plan ─────────────────────────────────────────────────────────
    try:
        from app import (
            PLAN_CONFIG_DATA, get_subscription_start_date,
            get_next_trading_day_after, calculate_expiry_from_start,
            IST, DB_FILE
        )

        cfg    = PLAN_CONFIG_DATA[plan]
        t_days = cfg["trading_days"]
        price  = cfg["price"]
        now    = datetime.now(IST)

        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()

        # Find latest future expiry for queueing
        c.execute("""
            SELECT MAX(plan_expiry) FROM subscriptions
            WHERE username=? AND status IN ('active','queued')
        """, (username,))
        sub_row = c.fetchone()

        c.execute("SELECT plan_expiry FROM users WHERE username=?", (username,))
        u_row = c.fetchone()

        effective_last = None
        for es in [sub_row[0] if sub_row else None, u_row[0] if u_row else None]:
            if es:
                try:
                    edt = datetime.fromisoformat(es)
                    if edt.tzinfo is None:
                        edt = IST.localize(edt)
                    if edt > now and (effective_last is None or edt > effective_last):
                        effective_last = edt
                except Exception:
                    pass

        start_dt = get_next_trading_day_after(effective_last) if effective_last else get_subscription_start_date()
        expiry_dt, total_cal, weekends, holidays = calculate_expiry_from_start(start_dt, t_days)
        new_status = "active" if start_dt <= now else "queued"

        c.execute("""
            INSERT INTO subscriptions
                (username, plan, plan_start, plan_expiry,
                 trading_days, total_calendar_days, weekends_skipped, holidays_skipped,
                 status, price, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (username, plan, start_dt.isoformat(), expiry_dt.isoformat(),
              t_days, total_cal, weekends, holidays, new_status, price, now.isoformat()))

        if new_status == "active":
            c.execute("""
                UPDATE users SET plan=?, plan_start=?, plan_expiry=? WHERE username=?
            """, (plan, start_dt.isoformat(), expiry_dt.isoformat(), username))

        verified_at = now.isoformat()
        c.execute("""
            UPDATE razorpay_orders
            SET status='paid', rzp_payment_id=?, rzp_signature=?, verified_at=?
            WHERE id=?
        """, (rzp_payment_id, rzp_signature, verified_at, db_row_id))

        conn.commit()
        conn.close()

    except Exception as e:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": f"Plan activation failed: {e}"}, status_code=500)

    # ── Fetch + cache payment method (best-effort, non-blocking) ─────────────
    try:
        _fetch_and_cache_payment_method(rzp_payment_id, db_row_id)
    except Exception:
        pass   # don't fail the whole verify if method fetch fails

    return JSONResponse({
        "success":    True,
        "plan":       plan,
        "plan_start": start_dt.isoformat(),
        "plan_expiry": expiry_dt.isoformat(),
        "status":     new_status,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/payment-history
# ═══════════════════════════════════════════════════════════════════════════════
@router.get("/api/payment-history")
def payment_history(request: Request):
    """
    Returns the user's own payment records.
    For paid records that are missing payment_method, fetches from Razorpay
    and caches — so the first call after a payment populates the method detail.
    Always returns a valid JSON response (never hangs).
    """
    username = request.session.get("user") or request.session.get("admin")
    if not username:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        # ── Ensure table exists (guards against first-run race) ───────────────
        init_razorpay_table()

        conn = _db()
        c    = conn.cursor()
        c.execute("""
            SELECT id, rzp_order_id, rzp_payment_id, plan,
                   amount_paise, status, payment_method, payment_detail,
                   created_at, verified_at
            FROM razorpay_orders
            WHERE username=?
            ORDER BY created_at DESC
            LIMIT 50
        """, (username,))
        rows = c.fetchall()
        conn.close()

        payments = []
        for r in rows:
            (db_id, order_id, pay_id, plan, amount_paise,
             status, p_method, p_detail, created_at, verified_at) = r

            # Back-fill missing method for paid orders (one Razorpay API call each)
            if status == "paid" and pay_id and not p_method:
                info = _fetch_and_cache_payment_method(pay_id, db_id)
                p_method = info["method"]
                p_detail = info["detail"]

            method_key = (p_method or "unknown").lower()
            icon       = METHOD_ICON.get(method_key,  "💰")
            label      = METHOD_LABEL.get(method_key, method_key.title() or "Online")

            # Format dates in IST
            def fmt_ist(iso_str):
                if not iso_str:
                    return ""
                try:
                    import pytz
                    dt = datetime.fromisoformat(iso_str)
                    if dt.tzinfo is None:
                        dt = pytz.utc.localize(dt)
                    IST = pytz.timezone("Asia/Kolkata")
                    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
                except Exception:
                    return iso_str

            payments.append({
                "order_id":       order_id  or "",
                "payment_id":     pay_id    or "",
                "plan":           plan      or "",
                "amount":         (amount_paise or 0) // 100,
                "status":         status    or "unknown",
                "method":         method_key,
                "method_label":   label,
                "method_icon":    icon,
                "method_detail":  p_detail  or "",
                "created_at":     fmt_ist(created_at),
                "verified_at":    fmt_ist(verified_at),
                "created_at_iso": created_at  or "",
                "verified_at_iso": verified_at or "",
            })

        return JSONResponse({"payments": payments, "total": len(payments)})

    except Exception as e:
        import traceback; traceback.print_exc()
        # Always return valid JSON so the frontend can show an error instead of spinning
        return JSONResponse(
            {"error": f"Could not load payment history: {e}", "payments": []},
            status_code=200   # 200 so JS .json() parses correctly
        )


# ═══════════════════════════════════════════════════════════════════════════════
# GET /api/admin/payments  — all payments (admin only)
# ═══════════════════════════════════════════════════════════════════════════════
@router.get("/api/admin/payments")
def admin_payments(request: Request):
    if request.session.get("role") != "admin":
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        init_razorpay_table()
        conn = _db()
        c    = conn.cursor()
        c.execute("""
            SELECT username, rzp_order_id, rzp_payment_id, plan,
                   amount_paise, status, payment_method, payment_detail,
                   created_at, verified_at
            FROM razorpay_orders
            ORDER BY created_at DESC
            LIMIT 500
        """)
        rows = c.fetchall()
        conn.close()

        def fmt_ist(iso_str):
            if not iso_str: return ""
            try:
                import pytz
                dt = datetime.fromisoformat(iso_str)
                if dt.tzinfo is None: dt = pytz.utc.localize(dt)
                return dt.astimezone(pytz.timezone("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
            except: return iso_str

        return JSONResponse({"payments": [
            {
                "username":      r[0],
                "order_id":      r[1],
                "payment_id":    r[2] or "",
                "plan":          r[3],
                "amount":        (r[4] or 0) // 100,
                "status":        r[5],
                "method":        r[6] or "",
                "method_detail": r[7] or "",
                "created_at":    fmt_ist(r[8]),
                "verified_at":   fmt_ist(r[9]),
            }
            for r in rows
        ]})
    except Exception as e:
        return JSONResponse({"error": str(e), "payments": []})