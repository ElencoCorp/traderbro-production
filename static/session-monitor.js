/**
 * TraderBro — Single-Device Session Monitor
 * ==========================================
 * Detects when the same account logs in on another device and
 * immediately shows a full-screen overlay, then redirects to home ("/").
 *
 * Works on ALL pages including home. Safe for guests (no-op if not logged in).
 */

(function () {
  "use strict";

  // ── Config ──────────────────────────────────────────────────────────
  const CHECK_INTERVAL_MS = 3000;   // poll every 3 s — fast detection
  const STARTUP_DELAY_MS  = 1000;   // first check after 1 s (page already loaded)
  const REDIRECT_DELAY_MS = 2000;   // show overlay 2 s then go home
  const REDIRECT_URL      = "/";

  // Pages where a non-logged-in response is expected and harmless.
  // NOTE: "/" is NOT here — logged-in users on home must be monitored too.
  const GUEST_OK_PATHS = [
    "/user-login",
    "/register",
    "/admin-login",
    "/about-us",
    "/contact-us",
    "/trading-plan",
    "/webinars",
    "/terms-and-conditions",
    "/privacy-policy",
    "/disclaimer",
    "/refund-policy",
    "/forgot-password",
  ];

  // ── State ────────────────────────────────────────────────────────────
  let overlayShown = false;
  let intervalId   = null;
  let lastKnownLoggedIn = null;  // track whether we WERE logged in

  // ── Helpers ──────────────────────────────────────────────────────────

  function isGuestOkPage() {
    const path = window.location.pathname;
    return GUEST_OK_PATHS.some(
      (p) => path === p || path.startsWith(p + "?")
    );
  }

  function showSessionEndedOverlay() {
    if (overlayShown) return;
    overlayShown = true;

    if (intervalId) clearInterval(intervalId);

    // Replace history entry so back button can't return
    window.history.replaceState(null, "", window.location.href);

    const overlay = document.createElement("div");
    overlay.id = "tb-session-ended-overlay";
    overlay.style.cssText = [
      "position:fixed",
      "inset:0",
      "background:#050814",
      "display:flex",
      "align-items:center",
      "justify-content:center",
      "z-index:2147483647",
      "flex-direction:column",
      "font-family:Poppins,sans-serif",
      "color:#e8eaf0",
      "text-align:center",
      "padding:2rem",
    ].join(";");

    overlay.innerHTML = `
      <div style="
        background:rgba(255,255,255,0.04);
        border:1px solid rgba(235,66,1,0.3);
        border-radius:16px;
        padding:2.5rem 3rem;
        max-width:420px;
        width:100%;
      ">
        <div style="font-size:3rem;margin-bottom:1rem;">🔒</div>
        <h2 style="
          font-size:1.4rem;
          font-weight:700;
          margin:0 0 0.5rem;
          color:#eb4201;
        ">Session Ended</h2>
        <p style="
          font-size:0.95rem;
          color:#9e9eb8;
          margin:0 0 1.5rem;
          line-height:1.6;
        ">
          Your account was logged in on another device.<br>
          Redirecting you to the home page…
        </p>
        <div style="
          width:100%;
          height:4px;
          background:rgba(255,255,255,0.08);
          border-radius:4px;
          overflow:hidden;
        ">
          <div id="tb-progress-bar" style="
            height:100%;
            width:0%;
            background:linear-gradient(90deg,#eb4201,#ff6935);
            border-radius:4px;
            transition:width ${REDIRECT_DELAY_MS}ms linear;
          "></div>
        </div>
      </div>
    `;

    // Wipe page DOM so no sensitive data is visible
    document.body.innerHTML = "";
    document.body.appendChild(overlay);

    window.onbeforeunload = null;

    // Kick off progress bar animation
    requestAnimationFrame(() => {
      const bar = document.getElementById("tb-progress-bar");
      if (bar) bar.style.width = "100%";
    });

    // Redirect — use replace() so back button cannot return
    setTimeout(() => {
      window.location.replace(REDIRECT_URL);
    }, REDIRECT_DELAY_MS);
  }

  // ── Main check ───────────────────────────────────────────────────────

  async function checkSession() {
    if (overlayShown) return;

    // Deliberate logout by this tab — don't false-positive
    if (sessionStorage.getItem("loggedOut")) return;

    try {
      const res = await fetch("/api/session-check", {
        credentials: "include",
        cache: "no-store",
      });

      if (!res.ok) return; // server unreachable — don't kick

      const data = await res.json();

      // ── Admin sessions are sacred — never touch them ──
      if (data.role === "admin") {
        lastKnownLoggedIn = true;
        return;
      }

      // ── Detect transition: was logged in, now not ──
      const isLoggedIn = data.logged_in === true && data.valid === true;

      if (lastKnownLoggedIn === true && !isLoggedIn) {
        // We WERE logged in and now we're not → another device took over
        showSessionEndedOverlay();
        return;
      }

      if (lastKnownLoggedIn === null && !isLoggedIn) {
        // First check and not logged in — guest on guest-ok page is fine
        if (isGuestOkPage()) {
          lastKnownLoggedIn = false;
          return;
        }
        // Not logged in on a protected page — also kick
        // (handles direct URL access to /dashboard etc with no session)
        lastKnownLoggedIn = false;
        return;
      }

      // Update state
      lastKnownLoggedIn = isLoggedIn;

    } catch (e) {
      // Network blip — don't kick
      console.debug("[SessionMonitor] check error:", e.message);
    }
  }

  // ── Boot ─────────────────────────────────────────────────────────────

  function start() {
    setTimeout(() => {
      checkSession();
      intervalId = setInterval(checkSession, CHECK_INTERVAL_MS);
    }, STARTUP_DELAY_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }

  // ── bfcache: back button after redirect ──────────────────────────────
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      overlayShown = false;
      lastKnownLoggedIn = null; // re-evaluate from scratch
      checkSession();
    }
  });

})();
