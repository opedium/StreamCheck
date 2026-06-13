#!/usr/bin/env python3
"""Telegram bot — /refresh_douyin using DYLoginApi Firefox session."""

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
from dy_apis.login_api import DYLoginApi
from telegram_notifier import TelegramNotifier


_QR_SESSIONS = {}  # cid -> (ctx, token)


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


def _build_url(base, query, extra=None):
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
        """DYLoginApi Firefox session → QR → Telegram → poll → done."""
        async def _run():
            from playwright.async_api import async_playwright

            # Step 1: Get Firefox session via DYLoginApi
            api = DYLoginApi()
            auth = await api.dyGenerateInitData()
            print(f"[TGBot] Init {len(auth.cookie)} cookies", flush=True)

            # Step 2: Launch Firefox with those cookies
            async with async_playwright() as pw:
                browser = await pw.firefox.launch(headless=True)
                ctx = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
                )
                seeds = [{"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                         for k, v in auth.cookie.items()]
                await ctx.add_cookies(seeds)

                page = await ctx.new_page()
                await page.goto("https://www.douyin.com/",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                # Intercept QR response
                qr_data = {}
                async def on_resp(resp):
                    if "get_qrcode" in resp.url and resp.ok:
                        try:
                            body = await resp.text()
                            j = _json.loads(body)
                            if j.get("data", {}).get("qrcode_index_url"):
                                qr_data.update(j)
                                print("[TGBot] QR intercepted", flush=True)
                        except Exception:
                            pass
                page.on("response", on_resp)

                await page.goto(_build_url("https://sso.douyin.com/get_qrcode/", _sso_query()),
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)

                if not qr_data:
                    self.send_message(cid, "No QR data")
                    await browser.close()
                    return

                token = qr_data["data"]["token"]
                qr_url = qr_data["data"]["qrcode_index_url"]
                print(f"[TGBot] QR OK token={token[:20]}...", flush=True)

                # Send QR to Telegram
                img = qrcode.make(qr_url)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                self.send_photo(cid, buf.getvalue(),
                    caption="Scan QR with Douyin app, then send:\n/refresh_douyin_done")

                _QR_SESSIONS[cid] = (ctx, token)
                print(f"[TGBot] Session active for {cid}", flush=True)

                # Poll check_qrconnect via Firefox page navigation
                check_data = {}
                async def on_check(resp):
                    if "check_qrconnect" in resp.url and resp.ok:
                        try:
                            body = await resp.text()
                            j = _json.loads(body)
                            check_data.update(j)
                            print(f"[TGBot] Check: err={j.get('error_code', '?')}", flush=True)
                        except Exception:
                            pass
                page.on("response", on_check)

                for i in range(60):
                    await asyncio.sleep(5)
                    if cid not in _QR_SESSIONS:
                        return
                    # Don't spam check until user has had time to scan
                    if i < 6:
                        continue
                    try:
                        ck_url = _build_url("https://sso.douyin.com/check_qrconnect/",
                                            _sso_query(), {"token": token})
                        await page.goto(ck_url, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(2000)
                        if check_data.get("error_code") == 0:
                            self.send_message(cid, "QR scanned! Following redirect...")
                            ru = check_data.get("data", {}).get("redirect_url", "")
                            if ru:
                                await page.goto(ru, wait_until="domcontentloaded", timeout=15000)
                                await page.wait_for_timeout(3000)
                            break
                        elif check_data.get("error_code") == 10001:
                            self.send_message(cid, "QR expired")
                            _QR_SESSIONS.pop(cid, None)
                            await browser.close()
                            return
                    except Exception as e:
                        print(f"[TGBot] Check err: {e}", flush=True)

                # Save cookies
                raw = await ctx.cookies()
                cookies = {c["name"]: c["value"] for c in raw}
                if "msToken" not in cookies:
                    cookies["msToken"] = generate_msToken()
                _QR_SESSIONS.pop(cid, None)
                await browser.close()

                new_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                print(f"[TGBot] Login OK — {len(cookies)} cookies", flush=True)
                try:
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
                    self.send_message(cid, f"Save error: {e}")

        asyncio.run(_run())
        _QR_SESSIONS.pop(cid, None)

    def _handle_refresh_douyin(self, cid):
        self.send_message(cid, "Launching browser (Firefox)...")
        t = threading.Thread(target=self._show_qr, args=(cid,), daemon=True)
        t.start()

    def _handle_done(self, cid):
        if cid not in _QR_SESSIONS:
            self.send_message(cid, "No active session. Send /refresh_douyin first.")
            return
        self.send_message(cid, "Waiting for Firefox to detect scan...")
        # The Firefox session is already polling check_qrconnect.
        # Just tell the user it's working - when detected, cookies auto-save.

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
