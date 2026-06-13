#!/usr/bin/env python3
# coding=utf-8
"""
Telegram bot — receive commands, send QR codes, save cookies.

Listens for:
  /refresh_douyin  — request a Douyin QR code, send to Telegram, poll for scan

Runs as a PM2 process alongside the other refreshers.
"""

import io
import json
import os
import sys
import time
import traceback

import requests

# Make Douyin_Spider importable
_dy_path = os.path.join(os.path.dirname(__file__), "..", "Douyin_Spider")
_dy_path = os.path.abspath(_dy_path)
if _dy_path not in sys.path:
    sys.path.insert(0, _dy_path)

from builder.auth import DouyinAuth
from builder.header import HeaderBuilder, HeaderType
from builder.params import Params
from telegram_notifier import TelegramNotifier


# ── SSO helpers (same as qr_login_helper.py, no Playwright) ──────────

_SSO_BASE = "https://sso.douyin.com/"


def _cookie_str_to_dict(cookie_str: str) -> dict:
    result = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def _auth_from_cookie_str(cookie_str: str) -> DouyinAuth:
    auth = DouyinAuth()
    auth.cookie = _cookie_str_to_dict(cookie_str)
    auth.cookie_str = cookie_str
    return auth


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
    params.add_param("device_platform", "web_app")
    if "msToken" in auth.cookie:
        params.add_param("msToken", auth.cookie["msToken"])
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

        self._offset = 0  # Telegram update_id cursor
        self._poll_timeout = 30  # seconds, long-poll

    # ── API calls ────────────────────────────────────────────────────

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

    # ── command handlers ─────────────────────────────────────────────

    def _handle_refresh_douyin(self, chat_id: int | str):
        """Full /refresh_douyin flow: QR → wait → save."""
        # Load cookies
        try:
            from cookies import DouyinCookieManager
            mgr = DouyinCookieManager()
            data = mgr.load()
            cookie_str = data.get("cookie_str", "")
        except Exception as e:
            self.send_message(chat_id, f"❌ Failed to load cookies: {e}")
            return

        if not cookie_str:
            self.send_message(chat_id, "❌ No Douyin cookie — run cookie-refresher first")
            return

        auth = _auth_from_cookie_str(cookie_str)

        # Request QR
        self.send_message(chat_id, "📱 Requesting Douyin QR code...")
        try:
            qr_data = _request_qr(auth)
        except Exception as e:
            self.send_message(chat_id, f"❌ QR request failed: {e}")
            return

        if qr_data.get("error_code") != 0:
            self.send_message(
                chat_id,
                f"❌ QR API error: {qr_data.get('description', str(qr_data))}",
            )
            return

        token = qr_data["data"]["token"]
        qr_url = qr_data["data"]["qrcode_index_url"]
        print(f"[TGBot] token={token[:24]}...", flush=True)

        # Generate QR image
        import qrcode
        img = qrcode.make(qr_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        self.send_photo(
            chat_id,
            buf.getvalue(),
            caption=(
                "📱 **Douyin QR Login**\n"
                "Scan with the Douyin app.\n"
                f"⏳ Polling for 5 minutes…"
            ),
        )

        # Poll for scan
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
                    self.send_message(chat_id, "❌ No redirect URL after scan")
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

                new_cookie_str = "; ".join(f"{k}={v}" for k, v in merged.items())
                print(
                    f"[TGBot] Login OK — {len(merged)} cookies",
                    flush=True,
                )

                save_data = dict(data)
                save_data["cookie_str"] = new_cookie_str
                save_data["cookie_dict"] = merged
                save_data["health"] = "ok"
                save_data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                save_data["refresh_count"] = (
                    save_data.get("refresh_count", 0) + 1
                )
                mgr.save(save_data)

                self.send_message(
                    chat_id,
                    f"✅ **Douyin QR login OK**\n"
                    f"{len(merged)} cookies saved, health=ok",
                )
                return

            elif err == 10001:
                self.send_message(
                    chat_id, "⏰ QR expired — send /refresh_douyin again"
                )
                return

        self.send_message(chat_id, "⏰ Timed out (5 min) — send /refresh_douyin again")

    # ── main loop ────────────────────────────────────────────────────

    def run(self):
        print("[TGBot] Starting — polling for commands...", flush=True)
        # Notify that the bot is alive
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
                            f"Unknown: `{text}`\n"
                            "Available: `/refresh_douyin`",
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
