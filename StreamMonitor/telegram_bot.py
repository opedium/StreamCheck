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
import json
import os
import sys
import time
import traceback

import qrcode
import requests

# Make Douyin_Spider importable
_dy_path = os.path.join(os.path.dirname(__file__), "..", "Douyin_Spider")
_dy_path = os.path.abspath(_dy_path)
if _dy_path not in sys.path:
    sys.path.insert(0, _dy_path)

from builder.auth import DouyinAuth
from builder.header import HeaderBuilder, HeaderType
from builder.params import Params
from utils.dy_util import generate_msToken
from telegram_notifier import TelegramNotifier


# ── SSO helpers ──────────────────────────────────────────────────────

_SSO_BASE = "https://sso.douyin.com/"


def _cookie_dict_to_str(cookie_dict: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items())


def _sso_params(auth: DouyinAuth, extra: dict = None) -> Params:
    params = Params()
    for k, v in (extra or {}).items():
        params.add_param(k, v)
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
    _msToken = auth.cookie.get("msToken", "")
    if not _msToken:
        _msToken = generate_msToken()
        auth.cookie["msToken"] = _msToken
    params.add_param("msToken", _msToken)
    params.with_a_bogus()
    return params


def _sso_headers() -> dict:
    h = HeaderBuilder().build(HeaderType.GET)
    h.set_referer("https://www.douyin.com/")
    return h.get()


def _request_qr(auth: DouyinAuth) -> dict:
    params = _sso_params(auth)
    resp = requests.get(
        _SSO_BASE + "get_qrcode/",
        headers=_sso_headers(),
        cookies=auth.cookie,
        params=params.get(),
        verify=False,
        timeout=15,
    )
    return resp.json()


def _check_qr(auth: DouyinAuth, token: str) -> dict:
    params = _sso_params(auth, {"token": token})
    resp = requests.get(
        _SSO_BASE + "check_qrconnect/",
        headers=_sso_headers(),
        cookies=auth.cookie,
        params=params.get(),
        verify=False,
        timeout=15,
    )
    return resp.json()


# ── Telegram bot ─────────────────────────────────────────────────────


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

    # ── Playwright bootstrap ─────────────────────────────────────────

    def _playwright_bootstrap(self, cookie_str: str) -> dict | None:
        """Launch headless Chromium, visit douyin.com, extract fresh cookies.

        Douyin's SSO endpoint requires anti-bot cookies that are only set by
        client-side JavaScript.  A quick Playwright visit to www.douyin.com
        executes the JS and gives us a usable session.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("[TGBot] Playwright not installed", flush=True)
            return None

        async def _run():
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir="/tmp/tgbot_profile",
                    headless=True,
                    channel="chrome",
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                # Seed cookies so the session is authenticated
                if cookie_str:
                    seeds = []
                    for part in cookie_str.split(";"):
                        part = part.strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            seeds.append({
                                "name": k.strip(),
                                "value": v.strip(),
                                "domain": ".douyin.com",
                                "path": "/",
                            })
                    if seeds:
                        await context.add_cookies(seeds)

                page = await context.new_page()
                await page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                # Let anti-bot JavaScript execute and set cookies
                await page.wait_for_timeout(5000)

                raw = await context.cookies()
                await context.close()
                return {c["name"]: c["value"] for c in raw}

        try:
            return asyncio.run(_run())
        except Exception as e:
            print(f"[TGBot] Playwright bootstrap failed: {e}", flush=True)
            return None

    # ── /refresh_douyin handler ──────────────────────────────────────

    def _handle_refresh_douyin(self, chat_id: int | str):
        """Full /refresh_douyin flow: bootstrap → QR → poll → save."""
        # 1. Load stored cookie
        try:
            from cookies import DouyinCookieManager
            mgr = DouyinCookieManager()
            data = mgr.load()
            cookie_str = data.get("cookie_str", "")
        except Exception as e:
            self.send_message(chat_id, f"❌ Failed to load cookies: {e}")
            return

        if not cookie_str:
            self.send_message(chat_id, "❌ No Douyin cookie found")
            return

        self.send_message(chat_id, "🔄 Launching browser to establish session...")

        # 2. Playwright bootstrap — get past anti-bot JS
        fresh = self._playwright_bootstrap(cookie_str)
        if not fresh:
            self.send_message(chat_id, "❌ Browser bootstrap failed")
            return

        auth = DouyinAuth()
        auth.cookie = fresh
        auth.cookie_str = _cookie_dict_to_str(fresh)
        print(f"[TGBot] Bootstrap OK — {len(fresh)} cookies", flush=True)

        # 3. Request QR code
        self.send_message(chat_id, "📱 Requesting QR code...")
        try:
            qr_data = _request_qr(auth)
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
        print(f"[TGBot] QR token={token[:24]}...", flush=True)

        # 4. Generate QR image and send to Telegram
        img = qrcode.make(qr_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        self.send_photo(
            chat_id,
            buf.getvalue(),
            caption="📱 **Douyin QR Login**\nScan with the Douyin app.\n⏳ Polling for 5 minutes…",
        )

        # 5. Poll for scan
        poll_seconds = 0
        MAX_POLL = 300
        while poll_seconds < MAX_POLL:
            time.sleep(5)
            poll_seconds += 5
            try:
                check = _check_qr(auth, token)
            except Exception as e:
                print(f"[TGBot] Poll error ({poll_seconds}s): {e}", flush=True)
                continue

            err = check.get("error_code", -1)
            print(f"[TGBot] Poll {poll_seconds}s: error_code={err}", flush=True)

            if err == 0:
                redirect_url = check.get("data", {}).get("redirect_url", "")
                if not redirect_url:
                    self.send_message(chat_id, "❌ Scan confirmed but no redirect")
                    return

                # Follow redirect chain to capture session cookies
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

                new_cookie_str = _cookie_dict_to_str(merged)
                print(f"[TGBot] Login OK — {len(merged)} cookies", flush=True)

                save_data = dict(data)
                save_data["cookie_str"] = new_cookie_str
                save_data["cookie_dict"] = merged
                save_data["health"] = "ok"
                save_data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                save_data["refresh_count"] = data.get("refresh_count", 0) + 1
                mgr.save(save_data)

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
