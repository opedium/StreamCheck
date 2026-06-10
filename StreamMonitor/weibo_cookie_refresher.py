#!/usr/bin/env python3
# coding=utf-8
"""
Weibo Cookie Refresher — Proactive Weibo session refresh via Playwright.

Runs as a standalone PM2-managed process.  Launches a persistent
Chrome profile, visits weibo.com every N hours to keep the session
alive, and writes refreshed cookies to weibo_cookies.json.

Usage:
    python3 weibo_cookie_refresher.py [INTERVAL_SECONDS]
    python3 weibo_cookie_refresher.py           # default: 43200 (12 hours)
    python3 weibo_cookie_refresher.py 21600     # every 6 hours
"""

import asyncio
import os
import sys
import traceback
from datetime import datetime

# ── Ensure StreamMonitor is on sys.path for sibling imports ──────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from weibo_cookie_manager import WeiboCookieManager
from telegram_notifier import TelegramNotifier


class WeiboCookieRefresher:
    """Launch persistent Chromium, visit Weibo, extract refreshed cookies."""

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
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
        old_cookie_str = data.get("cookie_str", "")
        profile_dir = os.path.join(_SCRIPT_DIR, "weibo_browser_profile")

        print(
            f"[WeiboCookieRefresher] Starting refresh cycle "
            f"(profile={profile_dir}, cookie_len={len(old_cookie_str)})",
            flush=True,
        )

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print(
                "[WeiboCookieRefresher] ERROR: playwright not installed. "
                "Run: pip install playwright && playwright install chrome",
                flush=True,
            )
            return False, False

        try:
            async with async_playwright() as p:
                # ── Launch persistent context ──────────────────────────
                # Persistent profile preserves localStorage, Service Workers,
                # and browser fingerprint across runs so Weibo sees the
                # SAME browser each time — critical for anti-fraud.
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

                # ── Seed existing cookies from weibo_cookies.json ──────
                # Required for first run (profile has no cookies yet)
                # and for recovery (profile lost/corrupt, weibo_cookies.json intact).
                if old_cookie_str:
                    cookie_list = []
                    for part in old_cookie_str.split(";"):
                        part = part.strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            k = k.strip()
                            v = v.strip()
                            if v:
                                cookie_list.append(
                                    {
                                        "name": k,
                                        "value": v,
                                        "domain": ".weibo.com",
                                        "path": "/",
                                    }
                                )
                    if cookie_list:
                        await context.add_cookies(cookie_list)

                page = await context.new_page()

                # ── Visit Weibo home page ─────────────────────────────
                print("[WeiboCookieRefresher] Navigating to weibo.com...", flush=True)
                await page.goto(
                    "https://weibo.com/", wait_until="domcontentloaded"
                )
                await asyncio.sleep(5)

                # ── Dead session detection ─────────────────────────────
                current_url = page.url
                if (
                    "passport.weibo.com" in current_url
                    or "login.sina.com.cn" in current_url
                ):
                    print(
                        f"[WeiboCookieRefresher] Session DEAD — redirected to "
                        f"login: {current_url}",
                        flush=True,
                    )
                    return False, False

                # ── Browse for cookie churn ────────────────────────────
                # Visiting the hot page triggers extra cookie churn
                await page.goto(
                    "https://weibo.com/hot",
                    wait_until="domcontentloaded",
                )
                await asyncio.sleep(3)

                # Re-check after browsing
                current_url = page.url
                if (
                    "passport.weibo.com" in current_url
                    or "login.sina.com.cn" in current_url
                ):
                    print(
                        f"[WeiboCookieRefresher] Session DEAD after browsing "
                        f"— redirected to login: {current_url}",
                        flush=True,
                    )
                    return False, False

                # ── Extract cookies ────────────────────────────────────
                page_cookies = await context.cookies()
                new_cookie_str_parts = []
                for c in page_cookies:
                    name = c.get("name", "")
                    value = c.get("value", "")
                    if name and value:
                        new_cookie_str_parts.append(f"{name}={value}")

                new_cookie_str = "; ".join(new_cookie_str_parts)

                # ── Cross-check: make sure we kept critical cookies ────
                # SUB and SUBP are the core identity cookies. If they're
                # gone, the session is effectively dead.
                has_sub = any("SUB=" in p for p in new_cookie_str_parts)
                if not has_sub:
                    print(
                        "[WeiboCookieRefresher] WARNING: SUB cookie missing "
                        "from extracted cookies — session may be degraded",
                        flush=True,
                    )

                print(
                    f"[WeiboCookieRefresher] Extracted {len(new_cookie_str_parts)} "
                    f"cookies ({len(new_cookie_str)} chars)",
                    flush=True,
                )

                # ── Save ───────────────────────────────────────────────
                self.manager.save(
                    {
                        "cookie_str": new_cookie_str,
                        "health": "ok" if has_sub else "degraded",
                        "refresh_count": data.get("refresh_count", 0) + 1,
                    }
                )

                # ── Test the refreshed cookie with a live API call ────
                test_ok = await self._test_cookie(new_cookie_str)

                print(
                    f"[WeiboCookieRefresher] Refresh SUCCESS "
                    f"(#{data.get('refresh_count', 0) + 1}) "
                    f"test={'PASS' if test_ok else 'FAIL'}",
                    flush=True,
                )
                return True, test_ok

        except Exception as e:
            print(
                f"[WeiboCookieRefresher] Refresh FAILED: {e}",
                flush=True,
            )
            traceback.print_exc()
            return False, False

    async def _test_cookie(self, cookie_str: str) -> bool:
        """Test a cookie string against Weibo's homepage.

        A working cookie loads the homepage without redirecting to login.
        Returns True if the cookie passes, False otherwise.
        """
        import requests as _r
        try:
            headers = {
                "User-Agent": self.USER_AGENT,
                "Cookie": cookie_str,
            }
            resp = _r.get(
                "https://weibo.com/login",
                headers=headers,
                allow_redirects=True,
                timeout=15,
            )
            final_url = resp.url or ""
            if "passport" in final_url or "login" in final_url:
                print(
                    f"[WeiboCookieRefresher] Test FAILED: "
                    f"redirected to {final_url[:80]}",
                    flush=True,
                )
                return False
            if resp.status_code != 200:
                print(
                    f"[WeiboCookieRefresher] Test FAILED: "
                    f"HTTP {resp.status_code}",
                    flush=True,
                )
                return False
            print(
                f"[WeiboCookieRefresher] Test PASSED: "
                f"status={resp.status_code}, url={final_url[:60]}",
                flush=True,
            )
            return True
        except Exception as e:
            print(
                f"[WeiboCookieRefresher] Test ERROR: {e}",
                flush=True,
            )
            return False


# ═══════════════════════════════════════════════════════════════════════
# Standalone entry point (PM2-managed scheduling loop)
# ═══════════════════════════════════════════════════════════════════════


async def main():
    interval = 43200  # default: 12 hours (Weibo sessions last weeks)
    if len(sys.argv) > 1:
        try:
            interval = int(sys.argv[1])
        except ValueError:
            print(
                f"Invalid interval '{sys.argv[1]}', using default {interval}s",
                flush=True,
            )

    mgr = WeiboCookieManager()
    notifier = TelegramNotifier()
    refresher = WeiboCookieRefresher(mgr)

    print(
        f"[WeiboCookieRefresher] Starting — interval={interval}s "
        f"({interval / 3600:.1f}h), "
        f"telegram={'enabled' if notifier.configured else 'disabled'}",
        flush=True,
    )

    # Bootstrap weibo_cookies.json from .env on first run
    WeiboCookieManager.bootstrap_from_env()

    # Run first refresh immediately so we don't wait 12h for initial data
    print("[WeiboCookieRefresher] Running initial refresh...", flush=True)
    success, test_ok = await refresher.refresh()
    if success:
        tag = "testing passed" if test_ok else "testing failed"
        notifier.send(
            f"weibo cookie refreshed ({tag}) "
            f"[{datetime.now().strftime('%H:%M')}]",
            state=None,
        )
    else:
        notifier.send(
            "weibo cookie refresh FAILED — "
            "check server logs",
            state="dead",
        )

    while True:
        await asyncio.sleep(interval)
        print(
            f"\n[WeiboCookieRefresher] Scheduled refresh at "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            flush=True,
        )
        success, test_ok = await refresher.refresh()
        if success:
            tag = "testing passed" if test_ok else "testing failed"
            notifier.send(
                f"weibo cookie refreshed ({tag}) "
                f"[{datetime.now().strftime('%H:%M')}]",
                state=None,
            )
        else:
            notifier.send(
                "weibo cookie refresh FAILED — session may be dead, "
                "manual re-login needed",
                state="dead",
            )


if __name__ == "__main__":
    asyncio.run(main())
