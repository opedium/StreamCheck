#!/usr/bin/env python3
"""Telegram bot — /refresh_douyin via Playwright QR screenshot."""

import asyncio
import io
import os
import sys
import time
import traceback

import requests

_dy_path = os.path.join(os.path.dirname(__file__), "..", "Douyin_Spider")
_dy_path = os.path.abspath(_dy_path)
if _dy_path not in sys.path:
    sys.path.insert(0, _dy_path)

from utils.dy_util import generate_msToken
from telegram_notifier import TelegramNotifier


class TelegramBot:
    def __init__(self):
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            from dotenv import load_dotenv
            load_dotenv(env_path)
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not self.token or not self.chat_id:
            print("[TGBot] TOKEN and CHAT_ID required", flush=True)
            sys.exit(1)
        self._offset = 0
        self._poll_timeout = 30
        # Track active QR sessions: chat_id -> (page, ctx)
        self._qr_sessions = {}

    def _api(self, method, **kw):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            r = requests.post(url, **kw, timeout=self._poll_timeout + 10)
            return r.json()
        except Exception as e:
            print(f"[TGBot] API err ({method}): {e}", flush=True)
            return {"ok": False}

    def get_updates(self):
        r = self._api("getUpdates", json={
            "offset": self._offset, "timeout": self._poll_timeout,
            "allowed_updates": ["message"],
        })
        if r.get("ok"):
            ups = r.get("result", [])
            if ups:
                self._offset = ups[-1]["update_id"] + 1
            return ups
        return []

    def send_message(self, cid, text):
        self._api("sendMessage", json={"chat_id": cid, "text": text})

    def send_photo(self, cid, data, caption=""):
        self._api("sendPhoto",
                  files={"photo": ("qr.png", data, "image/png")},
                  data={"chat_id": cid, "caption": caption})

    def _handle_refresh_douyin(self, cid):
        self.send_message(cid, "Launching browser...")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.send_message(cid, "Playwright not installed")
            return

        old = ""
        try:
            from cookies import DouyinCookieManager
            old = DouyinCookieManager().load().get("cookie_str", "")
        except Exception:
            pass

        async def _run():
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir="/tmp/tgbot_chrome", headless=True,
                    viewport={"width": 1280, "height": 720},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
                    args=["--disable-blink-features=AutomationControlled"],
                )
                # Don't seed old cookies — we want the QR login page to appear
                # so the user can scan and get fresh cookies.
                page = await ctx.new_page()

                # Visit user/self — shows QR if logged out, profile if logged in
                await page.goto("https://www.douyin.com/user/self",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)

                # Check if already logged in (no QR shown)
                current_url = page.url
                if "login_page" not in current_url and "user/self" in current_url:
                    # Already logged in — cookies are still valid
                    self.send_message(cid, "Already logged in — cookies still valid")
                    await ctx.close()
                    return

                # QR login page is shown — screenshot and send
                screenshot = await page.screenshot(type="png")
                self.send_photo(cid, screenshot,
                    caption="Scan this QR with the Douyin app, then send:\n/refresh_douyin_done")

                # Store session for later retrieval
                self._qr_sessions[cid] = (page, ctx)
                print(f"[TGBot] QR session started for {cid}", flush=True)

                # Wait for the done command (checked in main loop)
                # The browser stays open until done or timeout
                for _ in range(120):  # 10 minutes max
                    await asyncio.sleep(5)
                    if cid not in self._qr_sessions:
                        return  # session was cleaned up
                # Timeout — close session
                self.send_message(cid, "QR session timed out")
                await ctx.close()
                self._qr_sessions.pop(cid, None)

        try:
            asyncio.run(_run())
        except Exception as e:
            self.send_message(cid, f"Error: {e}")
            self._qr_sessions.pop(cid, None)

    async def _handle_done(self, cid):
        """Extract cookies from the QR session and save."""
        if cid not in self._qr_sessions:
            self.send_message(cid, "No active QR session. Send /refresh_douyin first.")
            return

        page, ctx = self._qr_sessions.pop(cid)
        self.send_message(cid, "Saving cookies...")

        try:
            # Navigate to douyin.com to ensure cookies are set
            await page.goto("https://www.douyin.com/",
                            wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)

            raw = await ctx.cookies()
            cookies = {c["name"]: c["value"] for c in raw}
            if "msToken" not in cookies:
                cookies["msToken"] = generate_msToken()
            await ctx.close()

            new_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            print(f"[TGBot] Login OK — {len(cookies)} cookies", flush=True)

            from cookies import DouyinCookieManager
            d = DouyinCookieManager().load()
            d["cookie_str"] = new_str
            d["cookie_dict"] = cookies
            d["health"] = "ok"
            d["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            d["refresh_count"] = d.get("refresh_count", 0) + 1
            DouyinCookieManager().save(d)

            self.send_message(cid, f"Douyin QR OK — {len(cookies)} cookies saved")
        except Exception as e:
            self.send_message(cid, f"Error saving cookies: {e}")
            await ctx.close()

    def run(self):
        print("[TGBot] Starting...", flush=True)
        self.send_message(self.chat_id,
            "Cookie bot online\n"
            "/refresh_douyin — get QR code\n"
            "/refresh_douyin_done — save cookies after scanning")

        while True:
            try:
                for u in self.get_updates():
                    msg = u.get("message", {})
                    text = msg.get("text", "").strip()
                    cid = msg.get("chat", {}).get("id")
                    if not cid:
                        continue
                    if text == "/refresh_douyin":
                        self._handle_refresh_douyin(cid)
                    elif text == "/refresh_douyin_done":
                        asyncio.run(self._handle_done(cid))
                    else:
                        self.send_message(cid, f"Unknown: {text}")
            except Exception as e:
                print(f"[TGBot] Loop: {e}", flush=True)
                traceback.print_exc()
                time.sleep(5)


def main():
    TelegramBot().run()

if __name__ == "__main__":
    main()
