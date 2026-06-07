"""
tools/set_webhook.py — register / remove / inspect the Telegram webhook
=======================================================================
MANUAL helper. Run locally ONLY when you intend to switch modes.
It does NOT run in CI and is not called by any workflow.

Usage:
    python tools/set_webhook.py set       # point Telegram at the webhook
    python tools/set_webhook.py delete    # remove webhook (re-enables polling)
    python tools/set_webhook.py info      # show current webhook status

Environment variables required:
    TELEGRAM_BOT_TOKEN   (for all commands)   — NEVER printed by this tool
    WEBHOOK_URL          (for 'set')          — e.g. https://<proj>.vercel.app/api/telegram
    WEBHOOK_SECRET       (for 'set')          — random string also stored in Vercel

NOTE: setWebhook disables getUpdates and vice-versa. When you 'set', also flip
the GitHub Actions secret TELEGRAM_CALLBACK_MODE=webhook so the monitor stops
polling. When you 'delete', set it back to polling. This tool does NOT touch
GitHub secrets.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request


def _api(method: str, payload: dict | None = None) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in the environment.")
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        # sanitise so the token can never leak through an error message/URL
        raise SystemExit(f"{method} failed: {str(e).replace(token, '***')}")


def cmd_set() -> None:
    url    = os.environ.get("WEBHOOK_URL", "")
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not url or not secret:
        raise SystemExit("Set WEBHOOK_URL and WEBHOOK_SECRET before 'set'.")
    res = _api("setWebhook", {
        "url": url,
        "secret_token": secret,
        "allowed_updates": ["message", "callback_query"],
    })
    print("setWebhook:", json.dumps(res, indent=2))


def cmd_delete() -> None:
    res = _api("deleteWebhook", {"drop_pending_updates": False})
    print("deleteWebhook:", json.dumps(res, indent=2))


def cmd_info() -> None:
    # getWebhookInfo never returns the bot token or the secret_token — safe to print.
    res = _api("getWebhookInfo")
    print("getWebhookInfo:", json.dumps(res, indent=2))


def main() -> None:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    {"set": cmd_set, "delete": cmd_delete, "info": cmd_info}.get(
        cmd, lambda: print("usage: python tools/set_webhook.py [set|delete|info]"))()


if __name__ == "__main__":
    main()
