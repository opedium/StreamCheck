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

        async def _run():
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir="/tmp/tgbot_chrome", headless=True,
                    viewport={"width": 1280, "height": 720},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = await ctx.new_page()

                await page.goto("https://www.douyin.com/user/self",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)

                # Check if logged in
                raw_cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
                if "sessionid" in raw_cookies:
                    self.send_message(cid, "Already logged in")
                    await ctx.close()
                    return

                # Click the QR login button to reveal the QR code
                try:
                    btn = page.locator("text=扫码登录")
                    await btn.wait_for(timeout=5000)
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    print("[TGBot] Clicked QR login button", flush=True)
                except Exception as e:
                    print(f"[TGBot] QR button click: {e}", flush=True)

                screenshot = await page.screenshot(type="png")
                self.send_photo(cid, screenshot,
                    caption="Scan QR with Douyin app, then send:\n/refresh_douyin_done")

                self._qr_sessions[cid] = (page, ctx)
                print(f"[TGBot] QR session started for {cid}", flush=True)

                for _ in range(120):
                    await asyncio.sleep(5)
                    if cid not in self._qr_sessions:
                        return
                self.send_message(cid, "QR session timed out")
                await ctx.close()
                self._qr_sessions.pop(cid, None)

        try:
            asyncio.run(_run())
        except Exception as e:
            self.send_message(cid, f"Error: {e}")
            self._qr_sessions.pop(cid, None)

    async def _handle_done(self, cid):
        if cid not in self._qr_sessions:
            self.send_message(cid, "No active QR session. Send /refresh_douyin first.")
            return

        page, ctx = self._qr_sessions.pop(cid)
        self.send_message(cid, "Saving cookies...")

        try:
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
            self.send_message(cid, f"Error: {e}")
            await ctx.close()

    def run(self):
        print("[TGBot] Starting...", flush=True)
        self.send_message(self.chat_id,
            "Cookie bot online\n"
            "/refresh_douyin — get QR code\n"
            "/refresh_douyin_done — save cookies after scan")

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
