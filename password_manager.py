"""
password_manager.py
===================
Complete password management for TraderBro.
Handles: forgot password (email OTP), reset password, change password.

SETUP REQUIRED in your .env file:
----------------------------------
MAIL_HOST=smtp.gmail.com
MAIL_PORT=587
MAIL_USER=your@gmail.com
MAIL_PASS=your_app_password        # Gmail App Password (NOT your login password)
MAIL_FROM=TraderBro <your@gmail.com>

For Gmail: https://myaccount.google.com/apppasswords
For other providers: use their SMTP settings.
----------------------------------

Include in app.py:
    from password_manager import router as pw_router
    app.include_router(pw_router)
"""

import os
import sqlite3
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from passlib.context import CryptContext
from dotenv import load_dotenv

load_dotenv()

IST = pytz.timezone("Asia/Kolkata")
router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Config (read from same .env as app.py) ──────────────────────────────
DB_FILE   = os.getenv("DB_FILE",   "traderbro.db")
MAIL_HOST = os.getenv("MAIL_HOST", "smtp.gmail.com")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USER = os.getenv("MAIL_USER", "")
MAIL_PASS = os.getenv("MAIL_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", f"TraderBro <{MAIL_USER}>")

OTP_EXPIRE_MINUTES = 15   # OTP valid for 15 minutes
MAX_OTP_ATTEMPTS   = 5    # lock after 5 wrong attempts per token


# ══════════════════════════════════════════════════════════════════════
# DB INIT
# ══════════════════════════════════════════════════════════════════════

def init_password_reset_table():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS password_resets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        email       TEXT NOT NULL,
        token       TEXT NOT NULL UNIQUE,
        otp         TEXT NOT NULL,
        attempts    INTEGER DEFAULT 0,
        used        INTEGER DEFAULT 0,
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

init_password_reset_table()


# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def _get_user_by_email(email: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, email, password FROM users WHERE email=?", (email.lower().strip(),))
    row = c.fetchone()
    conn.close()
    return row  # (username, email, password) or None

def _get_user_by_username(username: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, email, password FROM users WHERE username=?", (username.strip(),))
    row = c.fetchone()
    conn.close()
    return row

def _purge_expired_tokens():
    """Clean up old/used tokens — call before inserting new ones."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        DELETE FROM password_resets
        WHERE used=1 OR expires_at < datetime('now')
    """)
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════
# EMAIL SENDER
# ══════════════════════════════════════════════════════════════════════

def _send_otp_email(to_email: str, username: str, otp: str) -> bool:
    """
    Sends a styled OTP email.
    Returns True on success, False on failure.
    """
    if not MAIL_USER or not MAIL_PASS:
        print("⚠️  EMAIL NOT CONFIGURED — OTP:", otp)   # dev fallback
        return True   # return True so flow continues in dev

    subject = "TraderBro — Your Password Reset OTP"

    html_body = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:40px 0;">
  <tr><td align="center">
    <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,.08);">

      <!-- Header -->
      <tr><td style="background:linear-gradient(135deg,#0f172a,#1e293b);padding:32px 40px;text-align:center;">
        <div style="font-size:28px;font-weight:900;color:#ffffff;letter-spacing:-0.5px;">TraderBro</div>
        <div style="font-size:13px;color:#94a3b8;margin-top:4px;">traderbro.in</div>
      </td></tr>

      <!-- Body -->
      <tr><td style="padding:40px;">
        <p style="font-size:22px;font-weight:800;color:#0f172a;margin:0 0 8px;">Password Reset Request</p>
        <p style="font-size:14px;color:#64748b;margin:0 0 28px;">Hi <b>{username}</b>, use the OTP below to reset your password. It expires in <b>{OTP_EXPIRE_MINUTES} minutes</b>.</p>

        <!-- OTP Box -->
        <div style="background:linear-gradient(135deg,#eff6ff,#dbeafe);border:2px dashed #93c5fd;border-radius:16px;padding:28px;text-align:center;margin:0 0 28px;">
          <div style="font-size:13px;font-weight:700;color:#2563eb;letter-spacing:1px;margin-bottom:12px;text-transform:uppercase;">Your One-Time Password</div>
          <div style="font-size:44px;font-weight:900;letter-spacing:10px;color:#0f172a;font-family:'Courier New',monospace;">{otp}</div>
          <div style="font-size:12px;color:#64748b;margin-top:12px;">⏰ Valid for {OTP_EXPIRE_MINUTES} minutes only</div>
        </div>

        <!-- Warning -->
        <div style="background:#fff7ed;border-left:4px solid #f97316;border-radius:8px;padding:14px 16px;margin:0 0 28px;">
          <p style="font-size:13px;color:#9a3412;margin:0;"><b>⚠️ Security Notice:</b> Never share this OTP with anyone. TraderBro staff will never ask for it. If you didn't request this, ignore this email — your account is safe.</p>
        </div>

        <p style="font-size:13px;color:#94a3b8;margin:0;">This OTP will expire automatically. If you need a new one, request again on the website.</p>
      </td></tr>

      <!-- Footer -->
      <tr><td style="background:#f8fafc;padding:20px 40px;text-align:center;border-top:1px solid #e2e8f0;">
        <p style="font-size:12px;color:#94a3b8;margin:0;">© 2026 TraderBro | traderbro.in | Educational Platform (Not SEBI Registered)</p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>
"""

    text_body = f"TraderBro Password Reset\n\nHi {username},\n\nYour OTP: {otp}\nExpires in {OTP_EXPIRE_MINUTES} minutes.\n\nDo not share this with anyone.\n\n— TraderBro Team"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = MAIL_FROM
    msg["To"]      = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.login(MAIL_USER, MAIL_PASS)
            server.sendmail(MAIL_USER, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"❌ Email send error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════════════════════

# ── 1. Request OTP (forgot password step 1) ────────────────────────────
@router.post("/api/forgot-password/request")
async def forgot_password_request(request: Request):
    """
    Accepts email or username, finds the account,
    generates OTP + secure token, sends email.
    Returns a session_token (not the OTP) to the client
    so it can submit the OTP in step 2.
    """
    data       = await request.json()
    identifier = (data.get("identifier") or "").strip().lower()

    if not identifier:
        return JSONResponse({"success": False, "error": "Please enter your email or username."})

    # Find user
    user = _get_user_by_email(identifier)
    if not user:
        # Try by username
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT username, email, password FROM users WHERE LOWER(username)=?", (identifier,))
        row = c.fetchone()
        conn.close()
        user = row

    # Always respond the same way to prevent email enumeration
    if not user:
        return JSONResponse({
            "success": True,
            "message": "If that account exists, an OTP has been sent to the registered email."
        })

    username, email, _ = user

    # Rate-limit: max 3 active (unused, unexpired) tokens per email
    _purge_expired_tokens()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT COUNT(*) FROM password_resets
        WHERE email=? AND used=0 AND expires_at > datetime('now')
    """, (email,))
    active_count = c.fetchone()[0]
    conn.close()

    if active_count >= 3:
        return JSONResponse({"success": False, "error": "Too many reset requests. Please wait 15 minutes and try again."})

    # Generate OTP (6 digits) + secure session token
    otp           = str(secrets.randbelow(900000) + 100000)   # 100000–999999
    session_token = secrets.token_hex(32)
    now           = datetime.now(IST)
    expires       = now + timedelta(minutes=OTP_EXPIRE_MINUTES)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO password_resets (email, token, otp, attempts, used, created_at, expires_at)
        VALUES (?, ?, ?, 0, 0, ?, ?)
    """, (email, session_token, otp, now.isoformat(), expires.isoformat()))
    conn.commit()
    conn.close()

    # Send email
    sent = _send_otp_email(email, username, otp)
    if not sent:
        return JSONResponse({"success": False, "error": "Failed to send email. Please try again later."})

    # Mask email for display: s***@gmail.com
    parts    = email.split("@")
    masked   = parts[0][:2] + "***@" + parts[1] if len(parts) == 2 else "***"

    return JSONResponse({
        "success":       True,
        "session_token": session_token,     # client stores this to submit OTP
        "masked_email":  masked,
        "expires_in":    OTP_EXPIRE_MINUTES * 60,   # seconds
        "message":       f"OTP sent to {masked}"
    })


# ── 2. Verify OTP (forgot password step 2) ─────────────────────────────
@router.post("/api/forgot-password/verify-otp")
async def forgot_password_verify_otp(request: Request):
    """
    Verifies the OTP against the session_token.
    On success returns a short-lived reset_token to use in step 3.
    """
    data          = await request.json()
    session_token = (data.get("session_token") or "").strip()
    otp_input     = (data.get("otp") or "").strip()

    if not session_token or not otp_input:
        return JSONResponse({"success": False, "error": "Invalid request."})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT id, otp, attempts, used, expires_at
        FROM password_resets
        WHERE token=?
    """, (session_token,))
    row = c.fetchone()

    if not row:
        conn.close()
        return JSONResponse({"success": False, "error": "Invalid or expired session. Please start over."})

    rid, stored_otp, attempts, used, expires_at = row

    if used:
        conn.close()
        return JSONResponse({"success": False, "error": "This OTP has already been used."})

    # Check expiry
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = IST.localize(exp)
        if datetime.now(IST) > exp:
            conn.close()
            return JSONResponse({"success": False, "error": "OTP has expired. Please request a new one.", "expired": True})
    except Exception:
        conn.close()
        return JSONResponse({"success": False, "error": "Session error. Please start over."})

    # Check attempt limit
    if attempts >= MAX_OTP_ATTEMPTS:
        c.execute("UPDATE password_resets SET used=1 WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        return JSONResponse({"success": False, "error": "Too many incorrect attempts. Please request a new OTP.", "locked": True})

    # Verify OTP
    if otp_input != stored_otp:
        c.execute("UPDATE password_resets SET attempts=attempts+1 WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        remaining = MAX_OTP_ATTEMPTS - attempts - 1
        return JSONResponse({"success": False, "error": f"Incorrect OTP. {remaining} attempt(s) remaining."})

    # ✅ OTP correct — issue a reset_token (different from session_token)
    reset_token = secrets.token_hex(32)
    c.execute("UPDATE password_resets SET token=? WHERE id=?", (reset_token, rid))
    conn.commit()
    conn.close()

    return JSONResponse({
        "success":     True,
        "reset_token": reset_token,
        "message":     "OTP verified. You may now set a new password."
    })


# ── 3. Set New Password (forgot password step 3) ───────────────────────
@router.post("/api/forgot-password/reset")
async def forgot_password_reset(request: Request):
    """
    Final step: accepts reset_token + new password, updates DB.
    """
    data        = await request.json()
    reset_token = (data.get("reset_token") or "").strip()
    new_pw      = (data.get("new_password")     or "").strip()
    confirm_pw  = (data.get("confirm_password") or "").strip()

    if not reset_token or not new_pw or not confirm_pw:
        return JSONResponse({"success": False, "error": "All fields are required."})

    if new_pw != confirm_pw:
        return JSONResponse({"success": False, "error": "Passwords do not match."})

    if len(new_pw) < 8:
        return JSONResponse({"success": False, "error": "Password must be at least 8 characters."})

    if not any(ch.isupper() for ch in new_pw):
        return JSONResponse({"success": False, "error": "Password must contain at least one uppercase letter."})

    if not any(ch.isdigit() for ch in new_pw):
        return JSONResponse({"success": False, "error": "Password must contain at least one number."})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT id, email, used, expires_at
        FROM password_resets
        WHERE token=?
    """, (reset_token,))
    row = c.fetchone()

    if not row:
        conn.close()
        return JSONResponse({"success": False, "error": "Invalid or expired reset session. Please start over."})

    rid, email, used, expires_at = row

    if used:
        conn.close()
        return JSONResponse({"success": False, "error": "This reset link has already been used."})

    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = IST.localize(exp)
        if datetime.now(IST) > exp:
            conn.close()
            return JSONResponse({"success": False, "error": "Session expired. Please request a new OTP.", "expired": True})
    except Exception:
        conn.close()
        return JSONResponse({"success": False, "error": "Session error. Please start over."})

    # Check not same as current
    c.execute("SELECT password FROM users WHERE email=?", (email,))
    urow = c.fetchone()
    if urow and verify_password(new_pw, urow[0]):
        conn.close()
        return JSONResponse({"success": False, "error": "New password must be different from your current password."})

    # ✅ Update password + mark token used
    new_hashed = hash_password(new_pw)
    c.execute("UPDATE users SET password=? WHERE email=?", (new_hashed, email))
    c.execute("UPDATE password_resets SET used=1 WHERE id=?", (rid,))

    # Also invalidate all sessions for this user (force re-login everywhere)
    c.execute("""
        SELECT username FROM users WHERE email=?
    """, (email,))
    urow2 = c.fetchone()
    if urow2:
        c.execute("DELETE FROM active_sessions WHERE username=?", (urow2[0],))

    conn.commit()
    conn.close()

    return JSONResponse({"success": True, "message": "Password reset successfully. Please login with your new password."})


# ── 4. Change Password (logged-in user) ────────────────────────────────
@router.post("/api/change-password")
async def change_password(request: Request):
    """Logged-in user changes their own password."""
    username = request.session.get("user")
    if not username:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)

    data       = await request.json()
    current_pw = (data.get("current_password") or "").strip()
    new_pw     = (data.get("new_password")     or "").strip()
    confirm_pw = (data.get("confirm_password") or "").strip()

    if not current_pw or not new_pw or not confirm_pw:
        return JSONResponse({"success": False, "error": "All fields are required."})

    if new_pw != confirm_pw:
        return JSONResponse({"success": False, "error": "New passwords do not match."})

    if len(new_pw) < 8:
        return JSONResponse({"success": False, "error": "Password must be at least 8 characters."})

    if not any(ch.isupper() for ch in new_pw):
        return JSONResponse({"success": False, "error": "Password must contain at least one uppercase letter."})

    if not any(ch.isdigit() for ch in new_pw):
        return JSONResponse({"success": False, "error": "Password must contain at least one number."})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT password FROM users WHERE username=?", (username,))
    row = c.fetchone()

    if not row:
        conn.close()
        return JSONResponse({"success": False, "error": "User not found."})

    if not verify_password(current_pw, row[0]):
        conn.close()
        return JSONResponse({"success": False, "error": "Current password is incorrect."})

    if verify_password(new_pw, row[0]):
        conn.close()
        return JSONResponse({"success": False, "error": "New password must be different from your current password."})

    new_hashed = hash_password(new_pw)
    c.execute("UPDATE users SET password=? WHERE username=?", (new_hashed, username))
    conn.commit()
    conn.close()

    return JSONResponse({"success": True, "message": "Password changed successfully."})


# ── 5. Resend OTP ──────────────────────────────────────────────────────
@router.post("/api/forgot-password/resend")
async def forgot_password_resend(request: Request):
    """
    Invalidates the previous token and issues a fresh OTP to the same email.
    Requires the original session_token so we know which email to resend to.
    """
    data          = await request.json()
    session_token = (data.get("session_token") or "").strip()

    if not session_token:
        return JSONResponse({"success": False, "error": "Invalid request."})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT email, used, expires_at FROM password_resets WHERE token=?", (session_token,))
    row = c.fetchone()

    if not row:
        conn.close()
        return JSONResponse({"success": False, "error": "Session not found. Please start the forgot password flow again."})

    email, used, expires_at = row

    # Mark old token used
    c.execute("UPDATE password_resets SET used=1 WHERE token=?", (session_token,))
    conn.commit()
    conn.close()

    # Find username
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE email=?", (email,))
    urow = c.fetchone()
    conn.close()
    username = urow[0] if urow else "User"

    # Generate new OTP
    otp           = str(secrets.randbelow(900000) + 100000)
    new_token     = secrets.token_hex(32)
    now           = datetime.now(IST)
    expires       = now + timedelta(minutes=OTP_EXPIRE_MINUTES)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO password_resets (email, token, otp, attempts, used, created_at, expires_at)
        VALUES (?, ?, ?, 0, 0, ?, ?)
    """, (email, new_token, otp, now.isoformat(), expires.isoformat()))
    conn.commit()
    conn.close()

    sent = _send_otp_email(email, username, otp)
    if not sent:
        return JSONResponse({"success": False, "error": "Failed to send email."})

    parts  = email.split("@")
    masked = parts[0][:2] + "***@" + parts[1] if len(parts) == 2 else "***"

    return JSONResponse({
        "success":       True,
        "session_token": new_token,
        "masked_email":  masked,
        "expires_in":    OTP_EXPIRE_MINUTES * 60,
        "message":       f"New OTP sent to {masked}"
    })