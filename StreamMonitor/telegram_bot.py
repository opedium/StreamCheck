#!/usr/bin/env python3
# coding=utf-8
"""
Telegram bot — /refresh_douyin: QR login via Playwright end-to-end.
"""

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
    """Build SSO query params (same as login_api.py)."""
    msToken = generate_msToken()
    params = Params()
    params.add_param("service", "https://www.douyin.com")
    params.add_param("need_logo", "false")
    params.add_param("need_short_url", "false")
    params.add_param("passport_jssdk_version", "1.0.26")
    params.add_param("passport_jssdk_type", "pro")
    params.add_param("aid", "6383")
    params.add_param("language", "zh")
    params.add_param("account_sdk_source", "sso")
    params.add_param(
        "account_sdk_source_info",
        "7e276d64776172647760466a6b66707777606b667c273f3735292772606761776c736077273f63646976602927666d776a686061776c736077273f63646976602927766d60696961776c736077273f63646976602927756970626c6b76273f302927756077686c76766c6a6b76273f5e7e276b646860273f276b6a716c636c6664716c6a6b762729277671647160273f2775776a68757127785829276c6b6b60774d606c626d71273f3431313729276c6b6b6077526c61716d273f3436363129276a707160774d606c626d71273f3430303729276a70716077526c61716d273f37303335292776716a64776260567164717076273f7e276c6b61607d60614147273f7e276c6167273f276a676f6066712729276a75606b273f2763706b66716c6a6b2729276c6b61607d60614147273f276a676f6066712729274c41474e607c57646b6260273f2763706b66716c6a6b2729276a75606b4164716467647660273f27706b6160636c6b60612729276c7656646364776c273f636469766029276d6476436071666d273f6364697660782927696a66646956716a77646260273f7e276c76567075756a77714956716a77646260273f717770602927766c7f60273f3337313c32292772776c7160273f7177706078292776716a7764626054706a7164567164717076273f7e277076646260273f343031323236292774706a7164273f34373d3d313c33313030333d29276c7655776c73647160273f6364697660787829276b6a716c636c6664716c6a6b556077686c76766c6a6b273f2761606364706971272927756077636a7768646b6660273f7e27716c68604a776c626c6b273f3432373635343636303c3131372b362927707660614f564d606475566c7f60273f3437333c373c32343529276b64736c6264716c6a6b516c686c6b62273f7e276160666a616061476a617c566c7f60273f3035333434322927606b71777c517c7560273f276b64736c6264716c6a6b2729276c6b6c716c64716a77517c7560273f276b64736c6264716c6a6b2729276b646860273f276d717175763f2a2a7272722b616a707c6c6b2b666a682a707660772a48563172496f4447444444444075684d363131466e46723748303d513636543d5170437561734f764a7c645f6667527d444866334d3536724a534363344a72316855553c315141505631507627292777606b61607747696a666e6c6b62567164717076273f276b6a6b2867696a666e6c6b62272927766077736077516c686c6b62273f276c6b6b60772971715a6462722966616b286664666d602960616260296a776c626c6b272927627069605671647771273f343d3d3d2b3029276270696041707764716c6a6b273f34362b363c3c3c3c3c3c323334303d34313778782927776074706076715a6d6a7671273f277272722b616a707c6c6b2b666a68272927776074706076715a7564716d6b646860273f272a707660772a48563172496f4447444444444075684d363131466e46723748303d513636543d5170437561734f764a7c645f6667527d444866334d3536724a534363344a72316855553c31514150563150762778",
    )
    params.add_param("passport_ztsdk", "3.0.20")
    params.add_param("passport_verify", "1.0.17")
    params.add_param("device_platform", "web_app")
    params.add_param("msToken", msToken)
    params.with_a_bogus()
    return params.get()


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

    def _handle_refresh_douyin(self, cid: int | str):
        self.send_message(cid, "🔄 Launching browser...")

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.send_message(cid, "❌ Playwright not installed")
            return

        old_cookie_str = ""
        try:
            from cookies import DouyinCookieManager
            old_cookie_str = DouyinCookieManager().load().get("cookie_str", "")
        except Exception:
            pass

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
                            seeds.append({"name": k.strip(), "value": v.strip(),
                                          "domain": ".douyin.com", "path": "/"})
                    if seeds:
                        try:
                            await ctx.add_cookies(seeds)
                        except Exception:
                            pass

                page = await ctx.new_page()

                # Establish session on douyin.com
                await page.goto("https://www.douyin.com/",
                                wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                # Intercept SSO QR response
                qr_data = {}

                async def _on_response(resp):
                    if "get_qrcode" in resp.url and resp.ok:
                        try:
                            data = await resp.json()
                            if data.get("data", {}).get("qrcode_index_url"):
                                qr_data.update(data)
                        except Exception:
                            pass

                page.on("response", _on_response)

                # Request QR code via SSO
                sso_url = _build_url(_SSO_BASE + "get_qrcode/", _sso_query())
                await page.goto(sso_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)

                if not qr_data:
                    await page.goto(sso_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(5000)

                if not qr_data:
                    self.send_message(cid, "❌ No QR data from SSO")
                    await ctx.close()
                    return

                token = qr_data["data"]["token"]
                qr_url = qr_data["data"]["qrcode_index_url"]
                print(f"[TGBot] QR OK token={token[:20]}...", flush=True)

                # Send QR to Telegram immediately
                img = qrcode.make(qr_url)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                buf.seek(0)
                self.send_photo(cid, buf.getvalue(),
                                caption="📱 **Douyin QR Login**\nScan with the Douyin app.\n⏳ Polling for 5 minutes…")

                # Poll for scan inside the browser (keeps SSO cookies alive)
                print("[TGBot] Polling...", flush=True)
                check_query = _sso_query()
                for i in range(60):
                    await asyncio.sleep(5)
                    try:
                        url = _build_url(_SSO_BASE + "check_qrconnect/",
                                         check_query, {"token": token})
                        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(1000)
                        body = await page.evaluate("document.body.innerText")
                        check = _json.loads(body.strip())
                        err = check.get("error_code", -1)
                        print(f"[TGBot] Poll {i*5}s: err={err}", flush=True)

                        if err == 0:
                            redirect_url = check.get("data", {}).get("redirect_url", "")
                            if redirect_url:
                                await page.goto(redirect_url, wait_until="domcontentloaded", timeout=15000)
                                await page.wait_for_timeout(3000)
                            break
                        elif err == 10001:
                            self.send_message(cid, "⏰ QR expired")
                            await ctx.close()
                            return
                    except Exception as e:
                        print(f"[TGBot] Poll err ({i*5}s): {e}", flush=True)
                        continue
                else:
                    self.send_message(cid, "⏰ Timed out (5 min)")
                    await ctx.close()
                    return

                # Extract fresh cookies after login
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
                    self.send_message(cid, f"❌ Save: {e}")
                    return

                self.send_message(cid,
                    f"✅ **Douyin QR login OK**\n{len(cookies)} cookies saved, health=ok")

        try:
            asyncio.run(_run())
        except Exception as e:
            self.send_message(cid, f"❌ Error: {e}")

    def run(self):
        print("[TGBot] Starting...", flush=True)
        self.send_message(self.chat_id,
            "🟢 **Cookie bot online**\nCommands:\n  `/refresh_douyin`")
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
                        self.send_message(cid, f"Unknown: `{text}`")
            except Exception as e:
                print(f"[TGBot] Loop: {e}", flush=True)
                traceback.print_exc()
                time.sleep(5)


def main():
    TelegramBot().run()

if __name__ == "__main__":
    main()
