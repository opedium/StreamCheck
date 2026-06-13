#!/usr/bin/env python3
# coding=utf-8
"""
Telegram bot — receive commands, send QR codes, save cookies.

Listens for:
  /refresh_douyin  — request a Douyin QR code, send to Telegram, poll for scan

Runs as a PM2 process alongside the other refreshers.
"""

import asyncio
import io
import os
import sys
import time
import traceback

import qrcode
import requests

_dy_path = os.path.join(os.path.dirname(__file__), "..", "Douyin_Spider")
_dy_path = os.path.abspath(_dy_path)
if _dy_path not in sys.path:
    sys.path.insert(0, _dy_path)

from dy_apis.login_api import DYLoginApi
from telegram_notifier import TelegramNotifier


class TelegramBot:
    """Long-poll Telegram bot that handles /refresh_douyin commands."""

    def __init__(self):
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path)

        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not self.token or not self.chat_id:
            print("[TGBot] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required", flush=True)
            sys.exit(1)

        self._offset = 0
        self._poll_timeout = 30

    # ── Telegram API ─────────────────────────────────────────────────

    def _api(self, method: str, **kwargs) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            r = requests.post(url, **kwargs, timeout=self._poll_timeout + 10)
            return r.json()
        except Exception as e:
            print(f"[TGBot] API error ({method}): {e}", flush=True)
            return {"ok": False}

    def get_updates(self) -> list[dict]:
        result = self._api(
            "getUpdates",
            json={
                "offset": self._offset,
                "timeout": self._poll_timeout,
                "allowed_updates": ["message"],
            },
        )
        if result.get("ok"):
            updates = result.get("result", [])
            if updates:
                self._offset = updates[-1]["update_id"] + 1
            return updates
        return []

    def send_message(self, chat_id: int | str, text: str):
        self._api("sendMessage", json={"chat_id": chat_id, "text": text})

    def send_photo(self, chat_id: int | str, photo_bytes: bytes, caption: str = ""):
        self._api(
            "sendPhoto",
            files={"photo": ("qr.png", photo_bytes, "image/png")},
            data={"chat_id": chat_id, "caption": caption},
        )

    # ── /refresh_douyin handler ──────────────────────────────────────

    def _handle_refresh_douyin(self, chat_id: int | str):
        """Full /refresh_douyin flow using DYLoginApi."""
        self.send_message(chat_id, "🔄 Launching browser to establish session...")

        login_api = DYLoginApi()
        try:
            # Step 1: Playwright Firefox → visit douyin.com → extract cookies + keys
            auth = asyncio.run(login_api.dyGenerateInitData())
        except Exception as e:
            self.send_message(chat_id, f"❌ Browser init failed: {e}")
            return

        print(f"[TGBot] Init OK — {len(auth.cookie)} cookies", flush=True)

        # Step 2: Request QR code from SSO
        self.send_message(chat_id, "📱 Requesting QR code...")
        try:
            qr_data = login_api.dyGenerateQRcode(auth)
        except Exception as e:
            self.send_message(chat_id, f"❌ QR request failed: {e}")
            return

        if qr_data.get("error_code") != 0:
            self.send_message(
                chat_id,
                f"❌ QR error: {qr_data.get('description', str(qr_data)[:200])}",
            )
            return

        token = qr_data["data"]["token"]
        qr_url = qr_data["data"]["qrcode_index_url"]
        print(f"[TGBot] QR OK — token={token[:20]}...", flush=True)

        # Step 3: Generate QR image and send
        img = qrcode.make(qr_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        self.send_photo(
            chat_id,
            buf.getvalue(),
            caption="📱 **Douyin QR Login**\nScan with the Douyin app.\n⏳ Polling for 5 minutes…",
        )

        # Step 4: Poll for scan
        poll_seconds = 0
        MAX_POLL = 300
        while poll_seconds < MAX_POLL:
            time.sleep(5)
            poll_seconds += 5
            try:
                check = login_api.dyCheckQrCodeLogin(auth, token)
            except Exception as e:
                print(f"[TGBot] Poll error ({poll_seconds}s): {e}", flush=True)
                continue

            err = check.get("error_code", -1)
            print(f"[TGBot] Poll {poll_seconds}s: error_code={err}", flush=True)

            if err == 0:
                redirect_url = check.get("data", {}).get("redirect_url", "")
                if not redirect_url:
                    self.send_message(chat_id, "❌ Scan confirmed but no redirect URL")
                    return

                # Follow redirect to capture session cookies
                session = requests.Session()
                session.cookies.update(auth.cookie)
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                    ),
                }
                resp = session.get(
                    redirect_url, headers=headers, allow_redirects=True, timeout=15
                )
                merged = dict(session.cookies)
                for c in resp.cookies:
                    merged[c.name] = c.value

                new_str = "; ".join(f"{k}={v}" for k, v in merged.items())
                print(f"[TGBot] Login OK — {len(merged)} cookies", flush=True)

                try:
                    from cookies import DouyinCookieManager
                    mgr = DouyinCookieManager()
                    data = mgr.load()
                    data["cookie_str"] = new_str
                    data["cookie_dict"] = merged
                    data["health"] = "ok"
                    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    data["refresh_count"] = data.get("refresh_count", 0) + 1
                    mgr.save(data)
                except Exception as e:
                    self.send_message(chat_id, f"❌ Failed to save cookies: {e}")
                    return

                self.send_message(
                    chat_id,
                    f"✅ **Douyin QR login OK**\n"
                    f"{len(merged)} cookies saved, health=ok",
                )
                return

            elif err == 10001:
                self.send_message(chat_id, "⏰ QR expired — send /refresh_douyin again")
                return

        self.send_message(chat_id, "⏰ Timed out (5 min) — send /refresh_douyin again")

    # ── main loop ────────────────────────────────────────────────────

    def run(self):
        print("[TGBot] Starting — polling for commands...", flush=True)
        self.send_message(
            self.chat_id,
            "🟢 **Cookie bot online**\n"
            "Commands:\n"
            "  `/refresh_douyin` — QR login via Douyin app",
        )

        while True:
            try:
                updates = self.get_updates()
                for update in updates:
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    chat_id = msg.get("chat", {}).get("id")
                    if not chat_id:
                        continue
                    if text == "/refresh_douyin":
                        self._handle_refresh_douyin(chat_id)
                    else:
                        self.send_message(
                            chat_id,
                            f"Unknown: `{text}`\nAvailable: `/refresh_douyin`",
                        )
            except Exception as e:
                print(f"[TGBot] Loop error: {e}", flush=True)
                traceback.print_exc()
                time.sleep(5)


def main():
    bot = TelegramBot()
    bot.run()


if __name__ == "__main__":
    main()
