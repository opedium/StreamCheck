#!/usr/bin/env python3
"""Telegram bot — /refresh_douyin via SSO QR + background thread."""

import asyncio
import io
import json as _json
import os
import sys
import threading
import time
import traceback

import qrcode
import requests

_dy_path = os.path.join(os.path.dirname(__file__), "..", "Douyin_Spider")
_dy_path = os.path.abspath(_dy_path)
if _dy_path not in sys.path:
    sys.path.insert(0, _dy_path)

from builder.params import Params
from utils.dy_util import generate_msToken
from telegram_notifier import TelegramNotifier

_SSO_BASE = "https://sso.douyin.com/"


def _sso_query() -> dict:
    msToken = generate_msToken()
    p = Params()
    p.add_param("service", "https://www.douyin.com")
    p.add_param("need_logo", "false")
    p.add_param("need_short_url", "false")
    p.add_param("passport_jssdk_version", "1.0.26")
    p.add_param("passport_jssdk_type", "pro")
    p.add_param("aid", "6383")
    p.add_param("language", "zh")
    p.add_param("account_sdk_source", "sso")
    p.add_param("device_platform", "web_app")
    p.add_param("msToken", msToken)
    p.with_a_bogus()
    return p.get()


def _build_url(base: str, query: dict, extra: dict = None) -> str:
    q = dict(query)
    if extra:
        q.update(extra)
    return base + "?" + "&".join(f"{k}={v}" for k, v in q.items())


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

    def _show_qr(self, cid):
        """Playwright → SSO QR → send to Telegram → wait for done."""
        async def _run():
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=f"/tmp/tgbot_qr_{cid}", headless=True,
                    viewport={"width": 1280, "height": 720},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = await ctx.new_page()
                await page.goto("https://www.douyin.com/",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                # Intercept SSO QR response
                qr_data = {}
                async def on_resp(resp):
                    if "get_qrcode" in resp.url and resp.ok:
                        try:
                            j = await resp.json()
                            if j.get("data", {}).get("qrcode_index_url"):
                                qr_data.update(j)
                                print("[TGBot] QR intercepted", flush=True)
                        except Exception:
                            pass
                page.on("response", on_resp)

                sso_url = _build_url(_SSO_BASE + "get_qrcode/", _sso_query())
                await page.goto(sso_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
                if not qr_data:
                    await page.goto(sso_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(5000)
                if not qr_data:
                    self.send_message(cid, "No QR data from SSO")
                    await ctx.close()
                    return

                qr_url = qr_data["data"]["qrcode_index_url"]
                print(f"[TGBot] QR OK url={qr_url[:60]}...", flush=True)

                # Generate QR image from the URL using qrcode library
                img = qrcode.make(qr_url)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                self.send_photo(cid, buf.getvalue(),
                    caption="Scan QR with Douyin app, then send:\n/refresh_douyin_done")

                self._qr_sessions[cid] = ctx
                print(f"[TGBot] QR session started for {cid}", flush=True)

                # Wait for done signal (up to 10 min)
                for _ in range(120):
                    await asyncio.sleep(5)
                    if cid not in self._qr_sessions:
                        return
                self.send_message(cid, "QR session timed out")
                await ctx.close()
                self._qr_sessions.pop(cid, None)

        asyncio.run(_run())

    def _handle_refresh_douyin(self, cid):
        self.send_message(cid, "Launching browser...")
        t = threading.Thread(target=self._show_qr, args=(cid,), daemon=True)
        t.start()

    def _handle_done(self, cid):
        ctx = self._qr_sessions.pop(cid, None)
        if not ctx:
            self.send_message(cid, "No active QR session. Send /refresh_douyin first.")
            return

        self.send_message(cid, "Saving cookies...")

        async def _save():
            page = await ctx.new_page()
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

        asyncio.run(_save())

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
                        self._handle_done(cid)
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
