"""
api/telegram.py — Varam-Dynamics Telegram webhook (Phase 1, Vercel Python BETA)
==============================================================================
PHASE 1 SCOPE ONLY — prove the webhook plumbing. This endpoint:
  • verifies the Telegram secret header (X-Telegram-Bot-Api-Secret-Token)
  • answers /whoami (replies the caller's chat/user id — used later for invite-only)
  • answers a Help/TEST button tap INSTANTLY (clears the spinner)
  • exposes a GET health check

IT DOES NOT (by design, Phase 1):
  • log trades, open paper positions, or read/write any repo state
  • import the existing bot/scanner/tracker code (fully self-contained, stdlib only)

Secrets are read from environment variables and NEVER printed.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import json
import os
import urllib.request


# ── Telegram API (stdlib only; no 'requests' dependency) ────────────────────

def _tg(method: str, payload: dict) -> dict | None:
    """Best-effort Telegram call. Returns None if no token (e.g. local test)
    or on any error. Never prints the token."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        # sanitise: make sure a token can never leak via an error string
        print(f"[webhook] {method} failed: {str(e).replace(token, '***')}")
        return None


def _verify_secret(secret_header: str | None) -> bool:
    """True only if WEBHOOK_SECRET is configured AND matches the header."""
    expected = os.environ.get("WEBHOOK_SECRET", "")
    return bool(expected) and secret_header == expected


# ── Routing (pure-ish; directly unit-testable) ──────────────────────────────

def _route(update: dict) -> str:
    """Handle one Telegram update. Returns a short tag describing what we did
    (useful for tests/logs). Performs only safe, stateless Telegram replies."""
    # ---- text commands ----
    msg = update.get("message") or update.get("edited_message")
    if isinstance(msg, dict):
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        frm  = msg.get("from") or {}
        chat_id = chat.get("id")
        if text.startswith("/whoami"):
            _tg("sendMessage", {
                "chat_id": chat_id,
                "text": ("👤 <b>whoami</b>\n"
                         f"chat_id: <code>{chat_id}</code>\n"
                         f"user_id: <code>{frm.get('id')}</code>\n"
                         f"username: @{frm.get('username')}"),
                "parse_mode": "HTML",
            })
            return "whoami"
        if text.startswith("/help") or text.startswith("/start"):
            _tg("sendMessage", {
                "chat_id": chat_id,
                "text": ("🧪 Varam-Dynamics webhook (Phase 1 test) is online.\n"
                         "Commands: /whoami\n"
                         "This is a system test endpoint — not a trading service."),
            })
            return "help"
        return "ignored_message"

    # ---- button taps ----
    cb = update.get("callback_query")
    if isinstance(cb, dict):
        cb_id = cb.get("id")
        data  = cb.get("data", "")
        # Always clear the spinner instantly — this is the core proof-of-life.
        _tg("answerCallbackQuery",
            {"callback_query_id": cb_id, "text": "✅ Webhook received your tap"})
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        if data in ("HELP", "TEST", "PING") and chat_id is not None:
            _tg("sendMessage", {
                "chat_id": chat_id,
                "text": "🧪 Webhook test OK — your button tap was handled instantly.",
            })
            return "callback_help"
        return "callback_ack"

    return "ignored"


def process_request(body_bytes: bytes, secret_header: str | None) -> tuple[int, dict]:
    """Verify secret, parse, route. Returns (http_status, json_body)."""
    if not _verify_secret(secret_header):
        return 401, {"ok": False, "error": "unauthorized"}
    try:
        update = json.loads(body_bytes.decode() or "{}")
    except Exception:
        return 400, {"ok": False, "error": "bad json"}
    if not isinstance(update, dict):
        return 400, {"ok": False, "error": "bad update"}
    action = _route(update)
    return 200, {"ok": True, "action": action}


# ── Vercel Python handler (BaseHTTPRequestHandler) ──────────────────────────

class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # health check — no secrets exposed
        self._send(200, {"ok": True, "service": "varam-dynamics-webhook", "phase": 1})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        status, resp = process_request(body, secret)
        self._send(status, resp)

    def log_message(self, *args):  # silence default request logging
        return
