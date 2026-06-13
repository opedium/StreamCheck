#!/usr/bin/env python3
# coding=utf-8
"""
QR code login helper — last-resort cookie recovery via Telegram.

When all automated refresh methods fail (Playwright dead, CAPTCHA, IP
blocked), this script requests a Douyin SSO QR code, sends it to your
Telegram, and waits for you to scan it with the Douyin app.  On success
the fresh cookies are saved to ``cookies.json``.

Usage::

    python3 qr_login_helper.py douyin
"""

import io
import json
import os
import sys
import time

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
from utils.dy_util import generate_signature

from telegram_notifier import TelegramNotifier


# ── helpers ──────────────────────────────────────────────────────────

_SSO_BASE = "https://sso.douyin.com/"


def _cookie_str_to_dict(cookie_str: str) -> dict:
    """Parse a semicolon-separated cookie string into a dict."""
    result = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def _cookie_dict_to_str(cookie_dict: dict) -> str:
    """Convert a cookie dict to a semicolon-separated string."""
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items())


def _auth_from_cookie_str(cookie_str: str) -> DouyinAuth:
    """Build a DouyinAuth from a raw cookie string (minimal — no Playwright)."""
    auth = DouyinAuth()
    auth.cookie = _cookie_str_to_dict(cookie_str)
    auth.cookie_str = cookie_str
    return auth


def _sso_build_params(auth: DouyinAuth, extra: dict = None) -> Params:
    """Build the shared SSO parameter block (same as login_api.py)."""
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
    if "msToken" in auth.cookie:
        params.add_param("msToken", auth.cookie["msToken"])
    params.with_a_bogus()
    return params


def _sso_headers() -> dict:
    """Standard SSO request headers."""
    h = HeaderBuilder().build(HeaderType.GET)
    h.set_referer("https://www.douyin.com/")
    return h.get()


def _request_qr(auth: DouyinAuth) -> dict:
    """Call ``get_qrcode/`` and return the JSON response."""
    params = _sso_build_params(auth)
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
    """Call ``check_qrconnect/`` and return the JSON response."""
    params = _sso_build_params(auth, {"token": token})
    resp = requests.get(
        _SSO_BASE + "check_qrconnect/",
        headers=_sso_headers(),
        cookies=auth.cookie,
        params=params.get(),
        verify=False,
        timeout=15,
    )
    return resp.json()


# ── main flow ────────────────────────────────────────────────────────


def qr_login_douyin() -> bool:
    """Douyin QR code login flow — request QR → Telegram → poll → save."""
    notifier = TelegramNotifier()
    if not notifier.configured:
        print("[QR] Telegram not configured", flush=True)
        return False

    # Load existing cookie from cookies.json
    try:
        from cookies import DouyinCookieManager

        mgr = DouyinCookieManager()
        data = mgr.load()
        cookie_str = data.get("cookie_str", "")
    except Exception as e:
        print(f"[QR] Failed to load cookie manager: {e}", flush=True)
        return False

    if not cookie_str:
        print("[QR] No existing Douyin cookie found", flush=True)
        notifier.send(
            "❌ No Douyin cookie to start from — "
            "run the cookie-refresher first to establish a session",
            state=None,
        )
        return False

    auth = _auth_from_cookie_str(cookie_str)

    # Request QR code from Douyin SSO
    print("[QR] Requesting QR code from Douyin SSO...", flush=True)
    try:
        qr_data = _request_qr(auth)
    except Exception as e:
        print(f"[QR] QR request failed: {e}", flush=True)
        notifier.send(f"❌ Douyin QR request failed: {e}", state=None)
        return False

    if qr_data.get("error_code") != 0:
        msg = f"QR API error: {qr_data.get('description', qr_data)}"
        print(f"[QR] {msg}", flush=True)
        notifier.send(f"❌ Douyin {msg}", state=None)
        return False

    token = qr_data["data"]["token"]
    qr_url = qr_data["data"]["qrcode_index_url"]
    print(f"[QR] token={token[:24]}... url={qr_url[:60]}...", flush=True)

    # Generate QR code image and send via Telegram
    img = qrcode.make(qr_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    sent = notifier.send_photo(
        buf.getvalue(),
        caption=(
            "📱 **Douyin QR Login**\n"
            "Scan with the Douyin app to refresh cookies.\n"
            f"Token: `{token[:16]}`\n"
            "⏳ Polling for 5 minutes…"
        ),
    )
    if not sent:
        print("[QR] Failed to send QR image to Telegram", flush=True)
        return False

    # Poll for scan
    print("[QR] QR sent — polling every 5s...", flush=True)
    poll_seconds = 0
    MAX_POLL = 300
    while poll_seconds < MAX_POLL:
        time.sleep(5)
        poll_seconds += 5
        try:
            check = _check_qr(auth, token)
        except Exception as e:
            print(f"[QR] Poll error ({poll_seconds}s): {e}", flush=True)
            continue

        err = check.get("error_code", -1)
        print(f"[QR] Poll {poll_seconds}s: error_code={err}", flush=True)

        if err == 0:
            # Login confirmed — follow redirect URL to capture session cookies
            redirect_url = check.get("data", {}).get("redirect_url", "")
            if not redirect_url:
                print(f"[QR] No redirect_url in response: {check}", flush=True)
                notifier.send("❌ Douyin QR scan confirmed but no redirect", state=None)
                return False

            print(f"[QR] Following redirect to capture cookies...", flush=True)
            session = requests.Session()
            session.cookies.update(auth.cookie)
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            resp = session.get(redirect_url, headers=headers, allow_redirects=True, timeout=15)
            # Capture ALL cookies from the full redirect chain
            merged = dict(session.cookies)
            for c in resp.cookies:
                merged[c.name] = c.value

            new_cookie_str = _cookie_dict_to_str(merged)
            print(
                f"[QR] Login OK — {len(merged)} cookies ({len(new_cookie_str)} chars)",
                flush=True,
            )

            # Save to cookies.json
            save_data = dict(data)
            save_data["cookie_str"] = new_cookie_str
            save_data["cookie_dict"] = merged
            save_data["health"] = "ok"
            save_data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save_data["refresh_count"] = save_data.get("refresh_count", 0) + 1
            mgr.save(save_data)

            notifier.send(
                f"✅ **Douyin QR login successful**\n"
                f"{len(merged)} cookies saved, health=ok",
                state=None,
            )
            return True

        elif err == 10001:
            print("[QR] QR expired", flush=True)
            notifier.send("⏰ Douyin QR expired — run `qr_login_helper.py douyin` again", state=None)
            return False
        # 10002 = not yet scanned — keep polling

    print("[QR] Timed out (5 min)", flush=True)
    notifier.send("⏰ Douyin QR timed out (5min) — run again for a new code", state=None)
    return False


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python3 qr_login_helper.py <platform>", flush=True)
        print("  Platforms: douyin", flush=True)
        sys.exit(1)

    platform = sys.argv[1].lower()
    ok = qr_login_douyin() if platform == "douyin" else None
    if ok is None:
        print(f"Unsupported: {platform}", flush=True)
        sys.exit(1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
