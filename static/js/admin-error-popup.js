/**
 * TraderBro - Admin Error Alert Popup System
 * Automatically checks system health & API errors and displays high-priority popups ONLY to logged-in Admin users.
 */
(function () {
  'use strict';

  let isAdmin = false;
  let currentPopup = null;
  let lastErrorSignature = null;
  let dismissedSignatures = new Set();

  // Inject Popup Styles
  function injectStyles() {
    if (document.getElementById('tb-admin-error-styles')) return;
    const style = document.createElement('style');
    style.id = 'tb-admin-error-styles';
    style.textContent = `
      .tb-admin-modal-overlay {
        position: fixed;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        background: rgba(5, 8, 20, 0.85);
        backdrop-filter: blur(8px);
        z-index: 999999;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 20px;
        animation: tbFadeIn 0.3s ease-out;
      }
      @keyframes tbFadeIn {
        from { opacity: 0; transform: scale(0.96); }
        to { opacity: 1; transform: scale(1); }
      }
      .tb-admin-modal-card {
        background: #0f172a;
        border: 2px solid #ef4444;
        border-radius: 20px;
        box-shadow: 0 20px 60px rgba(239, 68, 68, 0.3);
        width: 100%;
        max-width: 580px;
        padding: 24px;
        color: #f8fafc;
        font-family: 'Poppins', system-ui, -apple-system, sans-serif;
        position: relative;
      }
      .tb-admin-modal-header {
        display: flex;
        align-items: center;
        gap: 12px;
        margin-bottom: 16px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        padding-bottom: 14px;
      }
      .tb-admin-modal-badge {
        background: rgba(239, 68, 68, 0.2);
        border: 1px solid #ef4444;
        color: #fca5a5;
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        padding: 4px 10px;
        border-radius: 20px;
        letter-spacing: 0.5px;
      }
      .tb-admin-modal-title {
        font-size: 18px;
        font-weight: 700;
        color: #ffffff;
        margin: 0;
        flex-grow: 1;
      }
      .tb-admin-modal-time {
        font-size: 11px;
        color: #94a3b8;
      }
      .tb-admin-modal-body {
        margin-bottom: 20px;
      }
      .tb-admin-error-box {
        background: rgba(15, 23, 42, 0.9);
        border: 1px dashed rgba(239, 68, 68, 0.5);
        border-radius: 12px;
        padding: 14px;
        color: #fca5a5;
        font-size: 13px;
        font-family: monospace;
        line-height: 1.5;
        word-break: break-word;
        max-height: 180px;
        overflow-y: auto;
        margin-top: 10px;
      }
      .tb-admin-modal-actions {
        display: flex;
        gap: 12px;
        justify-content: flex-end;
      }
      .tb-admin-btn {
        padding: 10px 18px;
        border-radius: 10px;
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        border: none;
        transition: all 0.2s ease;
      }
      .tb-admin-btn-primary {
        background: linear-gradient(135deg, #ef4444, #dc2626);
        color: white;
        box-shadow: 0 4px 14px rgba(239, 68, 68, 0.4);
      }
      .tb-admin-btn-primary:hover {
        opacity: 0.9;
        transform: translateY(-1px);
      }
      .tb-admin-btn-secondary {
        background: rgba(255, 255, 255, 0.1);
        color: #cbd5e1;
      }
      .tb-admin-btn-secondary:hover {
        background: rgba(255, 255, 255, 0.18);
        color: #ffffff;
      }
    `;
    document.head.appendChild(style);
  }

  // Show Error Modal
  function showErrorPopup(title, message, timestamp) {
    if (!isAdmin) return; // STRICT GUARD: Only Admin users

    const signature = `${title}:${message}`;
    if (dismissedSignatures.has(signature)) return; // User already dismissed this specific error

    injectStyles();

    if (currentPopup) {
      currentPopup.remove();
      currentPopup = null;
    }

    const overlay = document.createElement('div');
    overlay.className = 'tb-admin-modal-overlay';

    const formattedTime = timestamp || new Date().toLocaleTimeString();

    overlay.innerHTML = `
      <div class="tb-admin-modal-card">
        <div class="tb-admin-modal-header">
          <span class="tb-admin-modal-badge">🚨 Admin Alert</span>
          <h3 class="tb-admin-modal-title">${escapeHtml(title)}</h3>
          <span class="tb-admin-modal-time">⏰ ${escapeHtml(formattedTime)}</span>
        </div>
        <div class="tb-admin-modal-body">
          <div style="font-size: 13px; color: #cbd5e1;">
            The system encountered an error. As an Admin, you are notified to take action:
          </div>
          <div class="tb-admin-error-box">
            ${escapeHtml(message)}
          </div>
        </div>
        <div class="tb-admin-modal-actions">
          ${window.location.pathname.indexOf('/admin') === -1 ? `
            <button class="tb-admin-btn tb-admin-btn-primary" onclick="window.location.href='/admin'">⚙ Go to Admin Panel</button>
          ` : `
            <button class="tb-admin-btn tb-admin-btn-primary" onclick="location.reload()">🔄 Refresh Page</button>
          `}
          <button class="tb-admin-btn tb-admin-btn-secondary" id="tb-admin-dismiss-btn">Dismiss Alert</button>
        </div>
      </div>
    `;

    document.body.appendChild(overlay);
    currentPopup = overlay;

    document.getElementById('tb-admin-dismiss-btn').addEventListener('click', function () {
      dismissedSignatures.add(signature);
      overlay.remove();
      currentPopup = null;
    });
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Check System Status from Backend
  async function checkSystemStatus() {
    try {
      const res = await fetch('/api/system-status', { credentials: 'include' });
      if (!res.ok) return;
      const data = await res.json();

      isAdmin = !!data.is_admin;

      if (isAdmin && data.has_error) {
        showErrorPopup(
          data.error_title || 'System Error',
          data.error_message || 'An unspecified system error occurred.',
          data.timestamp
        );
      }
    } catch (e) {
      // Network failure to check system status
    }
  }

  // Intercept Global Fetch Calls to catch runtime API errors for Admin
  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    try {
      const response = await originalFetch.apply(this, args);
      const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url ? args[0].url : '');

      // Only check TraderBro API endpoints, skip static assets / heartbeats
      if (isAdmin && url && url.includes('/api/') && !url.includes('/api/system-status') && !url.includes('/api/heartbeat')) {
        if (response.status >= 400) {
          const clone = response.clone();
          try {
            const errJson = await clone.json();
            const msg = errJson.error || errJson.message || errJson.detail || `HTTP ${response.status} Error`;
            showErrorPopup(`API Error (${url.split('?')[0]})`, `[Status ${response.status}] ${msg}`);
          } catch (_) {
            showErrorPopup(`API Error (${url.split('?')[0]})`, `HTTP Status ${response.status}`);
          }
        }
      }
      return response;
    } catch (err) {
      const url = typeof args[0] === 'string' ? args[0] : '';
      if (isAdmin && url && url.includes('/api/')) {
        showErrorPopup('Network Request Error', `Failed to fetch ${url}: ${err.message}`);
      }
      throw err;
    }
  };

  // Initialization
  function init() {
    checkSystemStatus();
    setInterval(checkSystemStatus, 15000); // Check system status every 15 seconds
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
