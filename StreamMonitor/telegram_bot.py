#!/usr/bin/env python3
# coding=utf-8
"""
Telegram bot — receive commands, send QR codes, save cookies.

Listens for:
  /refresh_douyin  — request a Douyin QR code, send to Telegram, poll for scan
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

from builder.auth import DouyinAuth
from dy_apis.login_api import DYLoginApi
from telegram_notifier import TelegramNotifier


def _cookie_dict_to_str(cd: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cd.items())


def _chromium_visit(url: str, old_cookie_str: str, wait_s: int = 8) -> dict | None:
    """Open URL in headless Chromium, wait, extract cookies."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    async def _run():
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir="/tmp/tgbot_chrome",
                headless=True,
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                ),
                args=["--disable-blink-features=AutomationControlled"],
            )
            if old_cookie_str:
                seeds = []
                for part in old_cookie_str.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        seeds.append({
                            "name": k.strip(), "value": v.strip(),
                            "domain": ".douyin.com", "path": "/",
                        })
                if seeds:
                    try:
                        await ctx.add_cookies(seeds)
                    except Exception:
                        pass
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(wait_s * 1000)
            raw = await ctx.cookies()
            await ctx.close()
            return {c["name"]: c["value"] for c in raw}

    try:
        return asyncio.run(_run())
    except Exception as e:
        print(f"[TGBot] Chromium error: {e}", flush=True)
        return None


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

    def _api(self, method: str, **kwargs) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            r = requests.post(url, **kwargs, timeout=self._poll_timeout + 10)
            return r.json()
        except Exception as e:
            print(f"[TGBot] API error ({method}): {e}", flush=True)
            return {"ok": False}

    def get_updates(self) -> list[dict]:
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

    def send_message(self, cid: int | str, text: str):
        self._api("sendMessage", json={"chat_id": cid, "text": text})

    def send_photo(self, cid: int | str, data: bytes, caption: str = ""):
        self._api("sendPhoto", files={"photo": ("qr.png", data, "image/png")},
                  data={"chat_id": cid, "caption": caption})

    # ── /refresh_douyin ──────────────────────────────────────────────

    def _handle_refresh_douyin(self, cid: int | str):
        self.send_message(cid, "🔄 Launching browser...")

        old_cookie_str = ""
        try:
            from cookies import DouyinCookieManager
            old_cookie_str = DouyinCookieManager().load().get("cookie_str", "")
        except Exception:
            pass

        # Step 1: Visit douyin.com via Chromium to establish cookies
        cookies1 = _chromium_visit(
            "https://www.douyin.com/", old_cookie_str, wait_s=5
        )
        if not cookies1:
            self.send_message(cid, "❌ Browser failed")
            return

        # Build a DouyinAuth with these cookies
        auth = DouyinAuth()
        auth.cookie = cookies1
        auth.cookie_str = _cookie_dict_to_str(cookies1)
        print(f"[TGBot] douyin.com OK — {len(cookies1)} cookies", flush=True)

        # Step 2: Use DYLoginApi to request QR code
        login_api = DYLoginApi()
        self.send_message(cid, "📱 Requesting QR code...")
        try:
            qr_data = login_api.dyGenerateQRcode(auth)
        except Exception as e:
            print(f"[TGBot] QR error: {e}", flush=True)
            # Maybe need SSO-specific cookies — try visiting SSO URL too
            self.send_message(cid, "🔄 Retrying with SSO visit...")

            # Build the SSO URL
            from builder.params import Params
            from utils.dy_util import generate_msToken
            params = Params()
            params.add_param("service", "https://www.douyin.com")
            params.add_param("need_logo", "false")
            params.add_param("need_short_url", "false")
            params.add_param("passport_jssdk_version", "1.0.26")
            params.add_param("passport_jssdk_type", "pro")
            params.add_param("aid", "6383")
            params.add_param("language", "zh")
            params.add_param("account_sdk_source", "sso")
            params.add_param("device_platform", "web_app")
            params.add_param("msToken", generate_msToken())
            params.with_a_bogus()

            sso_url = "https://sso.douyin.com/get_qrcode/?" + "&".join(
                f"{k}={v}" for k, v in params.get().items()
            )

            cookies2 = _chromium_visit(sso_url, auth.cookie_str, wait_s=10)
            if not cookies2:
                self.send_message(cid, "❌ SSO visit failed")
                return

            auth2 = DouyinAuth()
            auth2.cookie = cookies2
            auth2.cookie_str = _cookie_dict_to_str(cookies2)
            print(f"[TGBot] SSO visit OK — {len(cookies2)} cookies", flush=True)

            try:
                qr_data = login_api.dyGenerateQRcode(auth2)
            except Exception as e2:
                self.send_message(cid, f"❌ QR failed after SSO visit: {e2}")
                return
            auth = auth2

        if qr_data.get("error_code") != 0:
            self.send_message(
                cid,
                f"❌ QR error: {qr_data.get('description', str(qr_data)[:200])}",
            )
            return

        token = qr_data["data"]["token"]
        qr_url = qr_data["data"]["qrcode_index_url"]
        print(f"[TGBot] QR OK token={token[:20]}...", flush=True)

        # Step 3: Send QR to Telegram
        img = qrcode.make(qr_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        self.send_photo(
            cid, buf.getvalue(),
            caption="📱 **Douyin QR Login**\nScan with the Douyin app.\n⏳ Polling for 5 minutes…",
        )

        # Step 4: Poll for scan
        poll_sec = 0
        while poll_sec < 300:
            time.sleep(5)
            poll_sec += 5
            try:
                check = login_api.dyCheckQrCodeLogin(auth, token)
            except Exception as e:
                print(f"[TGBot] Poll error ({poll_sec}s): {e}", flush=True)
                continue

            err = check.get("error_code", -1)
            print(f"[TGBot] Poll {poll_sec}s: err={err}", flush=True)

            if err == 0:
                redirect_url = check.get("data", {}).get("redirect_url", "")
                if not redirect_url:
                    self.send_message(cid, "❌ No redirect after scan")
                    return

                session = requests.Session()
                session.cookies.update(auth.cookie)
                hdrs = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                }
                resp = session.get(redirect_url, headers=hdrs, allow_redirects=True, timeout=15)
                merged = dict(session.cookies)
                for c in resp.cookies:
                    merged[c.name] = c.value

                new_str = _cookie_dict_to_str(merged)
                print(f"[TGBot] Login OK — {len(merged)} cookies", flush=True)

                try:
                    from cookies import DouyinCookieManager
                    d = DouyinCookieManager().load()
                    d["cookie_str"] = new_str
                    d["cookie_dict"] = merged
                    d["health"] = "ok"
                    d["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    d["refresh_count"] = d.get("refresh_count", 0) + 1
                    DouyinCookieManager().save(d)
                except Exception as e:
                    self.send_message(cid, f"❌ Save failed: {e}")
                    return

                self.send_message(
                    cid,
                    f"✅ **Douyin QR login OK**\n{len(merged)} cookies saved, health=ok",
                )
                return

            elif err == 10001:
                self.send_message(cid, "⏰ QR expired — send /refresh_douyin again")
                return

        self.send_message(cid, "⏰ Timed out (5 min) — send /refresh_douyin again")

    def run(self):
        print("[TGBot] Starting...", flush=True)
        self.send_message(self.chat_id, "🟢 **Cookie bot online**\nCommands:\n  `/refresh_douyin`")
        while True:
            try:
                for update in self.get_updates():
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    cid = msg.get("chat", {}).get("id")
                    if not cid:
                        continue
                    if text == "/refresh_douyin":
                        self._handle_refresh_douyin(cid)
                    else:
                        self.send_message(cid, f"Unknown: `{text}`\nAvailable: `/refresh_douyin`")
            except Exception as e:
                print(f"[TGBot] Loop: {e}", flush=True)
                traceback.print_exc()
                time.sleep(5)


def main():
    TelegramBot().run()

if __name__ == "__main__":
    main()
