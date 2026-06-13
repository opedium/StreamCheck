#!/usr/bin/env python3
"""Telegram bot — /refresh_douyin via Playwright end-to-end."""

import asyncio
import io
import json as _json
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

from builder.params import Params
from utils.dy_util import generate_msToken
from telegram_notifier import TelegramNotifier

_SSO_BASE = "https://sso.douyin.com/"


def _cookie_dict_to_str(cd: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cd.items())


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
                    viewport={"width": 1920, "height": 1080},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"),
                    args=["--disable-blink-features=AutomationControlled"],
                )
                if old:
                    seeds = []
                    for part in old.split(";"):
                        part = part.strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            seeds.append({"name": k.strip(), "value": v.strip(),
                                          "domain": ".douyin.com", "path": "/"})
                    if seeds:
                        try:
                            await ctx.add_cookies(seeds)
                        except Exception:
                            pass
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
                    self.send_message(cid, "No QR data")
                    await ctx.close()
                    return

                token = qr_data["data"]["token"]
                qr_url = qr_data["data"]["qrcode_index_url"]
                print(f"[TGBot] QR OK token={token[:20]}...", flush=True)

                img = qrcode.make(qr_url)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                self.send_photo(cid, buf.getvalue(),
                    caption="Douyin QR Login\nScan with Douyin app.\nPolling 5 min...")

                # Poll via Playwright's page.request — bypasses JS monkey-patching
                # of window.fetch by Douyin's security SDK (sdk-glue.js).
                print("[TGBot] Polling...", flush=True)
                cq = _sso_query()
                for i in range(60):
                    await asyncio.sleep(5)
                    try:
                        url = _build_url(_SSO_BASE + "check_qrconnect/", cq, {"token": token})
                        resp = await page.request.fetch(url)
                        body = await resp.text()
                        check = _json.loads(body.strip())
                        err = check.get("error_code", -1)
                        print(f"[TGBot] Poll {i*5}s err={err}", flush=True)

                        if err == 0:
                            self.send_message(cid, "QR scanned! Following redirect...")
                            ru = check.get("data", {}).get("redirect_url", "")
                            if ru:
                                await page.goto(ru, wait_until="domcontentloaded", timeout=15000)
                                await page.wait_for_timeout(3000)
                            break
                        elif err == 10001:
                            self.send_message(cid, "QR expired")
                            await ctx.close()
                            return
                    except Exception as e:
                        print(f"[TGBot] Poll ({i*5}s): {e}", flush=True)
                        continue
                else:
                    self.send_message(cid, "Timed out (5 min)")
                    await ctx.close()
                    return

                raw = await ctx.cookies()
                cookies = {c["name"]: c["value"] for c in raw}
                if "msToken" not in cookies:
                    cookies["msToken"] = generate_msToken()
                await ctx.close()

                new_str = _cookie_dict_to_str(cookies)
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
                except Exception as e:
                    self.send_message(cid, f"Save: {e}")
                    return
                self.send_message(cid, f"Douyin QR OK — {len(cookies)} cookies saved")

        try:
            asyncio.run(_run())
        except Exception as e:
            self.send_message(cid, f"Error: {e}")

    def run(self):
        print("[TGBot] Starting...", flush=True)
        self.send_message(self.chat_id, "Cookie bot online\n/refresh_douyin")
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
