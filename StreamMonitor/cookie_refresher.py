#!/usr/bin/env python3
# coding=utf-8
"""
Cookie Refresher — Proactive Douyin session refresh via Playwright.

Runs as a standalone PM2-managed process.  Launches a persistent
Chrome profile, visits Douyin every N hours to keep the session
alive, and writes refreshed cookies to cookies.json.

Usage:
    python3 cookie_refresher.py [INTERVAL_SECONDS]
    python3 cookie_refresher.py           # default: 21600 (6 hours)
    python3 cookie_refresher.py 3600      # every hour
"""

import asyncio
import builtins
import os
import sys
import traceback
from datetime import datetime

# ── Python 2 → 3 compatibility for protobuf_to_dict ──────────────────
# Required BEFORE importing anything from Douyin_Spider, because
# dy_apis/douyin_api.py imports protobuf_to_dict which references
# Python 2 builtins (long, unicode, basestring).
if not hasattr(builtins, 'long'):
    builtins.long = int
if not hasattr(builtins, 'unicode'):
    builtins.unicode = str
if not hasattr(builtins, 'basestring'):
    builtins.basestring = str

# ── Ensure StreamMonitor is on sys.path for sibling imports ──────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from cookie_manager import CookieManager
from telegram_notifier import TelegramNotifier


# ── Douyin_Spider path setup (same pattern as main.py) ───────────────
_DY_PATH = os.path.abspath(
    os.path.join(_SCRIPT_DIR, "..", "Douyin_Spider")
)
if _DY_PATH not in sys.path:
    sys.path.insert(0, _DY_PATH)


class CookieRefresher:
    """Launch persistent Chromium, visit Douyin, extract refreshed cookies."""

    # Matches HeaderBuilder.ua in builder/header.py for consistency
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self, cookie_manager):
        self.manager = cookie_manager

    async def refresh(self):
        """Run one refresh cycle.

        Returns:
            True if cookies were refreshed successfully.
            False if the session is dead (redirected to login).
        """
        data = self.manager.load()
        old_cookies = data.get("cookie_dict", {})
        profile_dir = os.path.join(_SCRIPT_DIR, "browser_profile")

        print(
            f"[CookieRefresher] Starting refresh cycle "
            f"(profile={profile_dir}, existing_cookies={len(old_cookies)})",
            flush=True,
        )

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print(
                "[CookieRefresher] ERROR: playwright not installed. "
                "Run: pip install playwright && playwright install chrome",
                flush=True,
            )
            return False

        try:
            async with async_playwright() as p:
                # ── Launch persistent context ──────────────────────────
                # This preserves localStorage, Service Workers, and browser
                # fingerprint (WebGL, canvas, fonts) across runs so Douyin
                # sees the SAME browser each time.
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    channel="chrome",
                    viewport={"width": 1920, "height": 1080},
                    user_agent=self.USER_AGENT,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                    ],
                )

                # ── Seed existing cookies ──────────────────────────────
                # Required for first run (profile has no cookies yet)
                # and for recovery (profile lost/corrupt, cookies.json intact).
                if old_cookies:
                    cookie_list = []
                    for k, v in old_cookies.items():
                        if v:
                            cookie_list.append(
                                {
                                    "name": k,
                                    "value": v,
                                    "domain": ".douyin.com",
                                    "path": "/",
                                }
                            )
                    if cookie_list:
                        await context.add_cookies(cookie_list)

                page = await context.new_page()

                # ── Visit homepage ─────────────────────────────────────
                print(
                    "[CookieRefresher] Navigating to douyin.com...",
                    flush=True,
                )
                await page.goto(
                    "https://www.douyin.com/", wait_until="networkidle"
                )
                await asyncio.sleep(5)

                # ── Dead session detection ─────────────────────────────
                current_url = page.url
                if (
                    "passport" in current_url
                    or "sso.douyin.com" in current_url
                ):
                    print(
                        f"[CookieRefresher] Session DEAD — redirected to "
                        f"login: {current_url}",
                        flush=True,
                    )
                    return False

                # ── Browse for cookie churn ────────────────────────────
                # Visiting additional pages triggers Douyin's sliding
                # session extension and may rotate short-lived tokens.
                await page.goto(
                    "https://www.douyin.com/discover",
                    wait_until="networkidle",
                )
                await asyncio.sleep(3)

                # ── Visit live.douyin.com for s_v_web_id ──────────────
                # s_v_web_id is the device fingerprint cookie used in
                # every Douyin API call (as the "fp" parameter).  It is
                # ONLY set by the live.douyin.com subdomain — visiting
                # www.douyin.com alone will never produce it.
                print(
                    "[CookieRefresher] Visiting live.douyin.com "
                    "for s_v_web_id...",
                    flush=True,
                )
                await page.goto(
                    "https://live.douyin.com/",
                    wait_until="networkidle",
                )
                await asyncio.sleep(3)

                # ── Extract cookies ────────────────────────────────────
                page_cookies = await context.cookies()
                new_cookies = {}
                for c in page_cookies:
                    new_cookies[c["name"]] = c["value"]

                print(
                    f"[CookieRefresher] Extracted {len(new_cookies)} cookies",
                    flush=True,
                )

                # ── Extract localStorage signing keys ──────────────────
                try:
                    keys_str = (
                        await page.evaluate(
                            'localStorage["security-sdk/s_sdk_crypt_sdk"]'
                        )
                        or ""
                    )
                except Exception:
                    keys_str = ""

                # ── Rebuild auth via DouyinAuth ────────────────────────
                # This derives private_key, ticket, ts_sign etc. from
                # the localStorage keys, which are needed for API calls
                # that use bd-ticket-guard-* headers.
                from builder.auth import DouyinAuth

                auth = DouyinAuth()
                auth.perepare_auth("", "", keys_str)
                auth.cookie = new_cookies
                auth.cookie_str = "; ".join(
                    f"{k}={v}" for k, v in new_cookies.items()
                )

                # ── Save ───────────────────────────────────────────────
                self.manager.save(
                    {
                        "cookie_str": auth.cookie_str,
                        "cookie_dict": new_cookies,
                        "private_key": auth.private_key or "",
                        "ticket": auth.ticket or "",
                        "ts_sign": auth.ts_sign or "",
                        "client_cert": auth.client_cert or "",
                        "ree_public_key": auth.ree_public_key or "",
                        "uid": auth.uid or "",
                        "health": "ok",
                        "refresh_count": data.get("refresh_count", 0) + 1,
                    }
                )

                print(
                    f"[CookieRefresher] Refresh SUCCESS "
                    f"(#{data.get('refresh_count', 0) + 1})",
                    flush=True,
                )
                return True

        except Exception as e:
            print(
                f"[CookieRefresher] Refresh FAILED: {e}",
                flush=True,
            )
            traceback.print_exc()
            return False


# ═══════════════════════════════════════════════════════════════════════
# Standalone entry point (PM2-managed scheduling loop)
# ═══════════════════════════════════════════════════════════════════════


async def main():
    interval = 21600  # default: 6 hours
    if len(sys.argv) > 1:
        try:
            interval = int(sys.argv[1])
        except ValueError:
            print(
                f"Invalid interval '{sys.argv[1]}', "
                f"using default {interval}s",
                flush=True,
            )

    mgr = CookieManager()
    notifier = TelegramNotifier()
    refresher = CookieRefresher(mgr)

    print(
        f"[CookieRefresher] Starting — interval={interval}s "
        f"({interval / 3600:.1f}h), "
        f"telegram={'enabled' if notifier.configured else 'disabled'}",
        flush=True,
    )

    # Bootstrap cookies.json from .env on first run
    CookieManager.bootstrap_from_env()

    # Run first refresh immediately so we don't wait 6h for initial data
    print("[CookieRefresher] Running initial refresh...", flush=True)
    success = await refresher.refresh()
    if success:
        notifier.send(
            "✅ Cookies refreshed successfully "
            f"({datetime.now().strftime('%H:%M')})",
            state="ok",
        )
    else:
        notifier.send(
            "⚠️ Initial cookie refresh FAILED — "
            "check server logs",
            state="dead",
        )

    while True:
        await asyncio.sleep(interval)
        print(
            f"\n[CookieRefresher] Scheduled refresh at "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            flush=True,
        )
        success = await refresher.refresh()
        if success:
            notifier.send(
                "✅ Cookies refreshed successfully "
                f"({datetime.now().strftime('%H:%M')})",
                state="ok",
            )
        else:
            notifier.send(
                "\U0001f6a8 Cookie refresh FAILED — session may be dead, "
                "manual re-login needed",
                state="dead",
            )


if __name__ == "__main__":
    asyncio.run(main())
