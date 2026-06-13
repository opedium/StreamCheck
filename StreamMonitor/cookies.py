#!/usr/bin/env python3
# coding=utf-8
"""Unified cookie management for Douyin / Weibo / Bilibili.

Replaces: cookie_manager.py, weibo_cookie_manager.py, bilibili_cookie_manager.py,
          cookie_refresher.py, weibo_cookie_refresher.py, bilibili_cookie_refresher.py

Architecture
────────────
Platform-specific behaviour is driven by PLATFORM config dicts — no class hierarchy.
One file, three platforms, all storage + refresh + recovery in one place.

Recovery pyramid (6 layers, 100 % server-side, no human needed until Layer 6):
  Layer 1 — KeepaliveChecker  (lightweight HTTP, no browser)
  Layer 2 — Browser refresh   (Playwright persistent profile)
  Layer 3 — Clean-profile     (delete corrupt profile, retry once)
  Layer 4 — CookiePool        (fall back to backup cookie set)
  Layer 5 — MsToken fetch     (real /sdk_token endpoint, not fake random)
  Layer 6 — Telegram alert    (last resort — manual intervention)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from typing import Optional


# ──────────────────────────────────────────────────────────────────────
# Section A — Platform configuration
# ──────────────────────────────────────────────────────────────────────

PLATFORM: dict[str, dict] = {
    "douyin": {
        "cookie_file": "cookies.json",
        "profile_dir": "browser_profile",
        "env_var": "DY_LIVE_COOKIES",
        "env_var_fallback": "DY_COOKIES",
        "domains": [".douyin.com"],
        "critical_cookies": ["s_v_web_id"],
        "refresh_urls": [
            "https://www.douyin.com/",
            "https://www.douyin.com/discover",
            "https://live.douyin.com/",
        ],
        "sleep_after_nav": 8,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "has_auth_integration": True,
        "refresh_interval": 21600,       # 6 h
        "base_dir": None,               # resolved at runtime
    },
    "weibo": {
        "cookie_file": "weibo_cookies.json",
        "profile_dir": "weibo_browser_profile",
        "env_var": "WEIBO_COOKIE",
        "env_var_fallback": None,
        "domains": [".weibo.com"],
        "critical_cookies": ["SUB"],
        "refresh_urls": [
            "https://weibo.com/",
            "https://weibo.com/hot",
        ],
        "sleep_after_nav": 5,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "has_auth_integration": False,
        "refresh_interval": 43200,       # 12 h
        "base_dir": None,
    },
    "bilibili": {
        "cookie_file": "bilibili_cookies.json",
        "profile_dir": "bilibili_browser_profile",
        "env_var": "BILI_COOKIE",
        "env_var_fallback": None,
        "domains": [".bilibili.com"],
        "critical_cookies": ["SESSDATA", "bili_jct"],
        "refresh_urls": [
            "https://www.bilibili.com/",
            "https://space.bilibili.com/",
        ],
        "sleep_after_nav": 5,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "has_auth_integration": False,
        "refresh_interval": 86400,       # 24 h
        "base_dir": None,
    },
}

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
for _cfg in PLATFORM.values():
    _cfg["base_dir"] = _SCRIPT_DIR

# ──────────────────────────────────────────────────────────────────────
# Section B — Utility functions
# ──────────────────────────────────────────────────────────────────────


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """Split ``"key=value; key2=value2"`` into ``{key: value, ...}``."""
    result: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def build_cookie_str(cookie_dict: dict[str, str]) -> str:
    """Join ``{key: value}`` into ``"key=value; key2=value2"``."""
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items())


def extract_cookie_value(
    cookie_str: str, key: str, case_insensitive: bool = False
) -> str:
    """Return the value of the first cookie whose name matches *key*."""
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if case_insensitive:
                if k.strip().upper() == key.upper():
                    return v.strip()
            elif k.strip() == key:
                return v.strip()
    return ""


# ──────────────────────────────────────────────────────────────────────
# Section C — CookieManager  (atomic file I/O, one per platform)
# ──────────────────────────────────────────────────────────────────────

_DEFAULT_COOKIE_DATA: dict[str, dict] = {
    "douyin": {
        "cookie_str": "",
        "cookie_dict": {},
        "private_key": "",
        "ticket": "",
        "ts_sign": "",
        "client_cert": "",
        "ree_public_key": "",
        "uid": "",
        "health": "unknown",
        "updated_at": "",
        "refresh_count": 0,
    },
    "weibo": {
        "cookie_str": "",
        "health": "unknown",
        "updated_at": "",
        "refresh_count": 0,
    },
    "bilibili": {
        "cookie_str": "",
        "health": "unknown",
        "updated_at": "",
        "refresh_count": 0,
    },
}


def _resolve_cookie_path(platform: str, cookies_file: str | None = None) -> str:
    """Return the absolute path to the platform's cookie JSON file."""
    cfg = PLATFORM[platform]
    if cookies_file:
        return cookies_file
    return os.path.join(cfg["base_dir"], cfg["cookie_file"])


class _UnifiedCookieManager:
    """Atomic read / write for one platform's cookie JSON file.

    Designed to be completely transparent about the ``data`` dict it
    passes around — the caller reads a dict, modifies fields, writes it
    back.  The manager only guarantees atomic I/O and default values.
    """

    def __init__(self, platform: str, cookies_file: str | None = None):
        self.platform = platform
        self.cfg = PLATFORM[platform]
        self.file = _resolve_cookie_path(platform, cookies_file)

    # ── load / save ─────────────────────────────────────────────────

    def load(self) -> dict:
        """Return cookie data.  Never raises — returns defaults on error."""
        defaults = dict(_DEFAULT_COOKIE_DATA[self.platform])
        try:
            if os.path.exists(self.file):
                with open(self.file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                defaults.update(data)
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"[{self.platform}] Failed to load {self.file}: {e}",
                flush=True,
            )
        return defaults

    def save(self, data: dict):
        """Atomic write via ``.tmp`` → ``os.replace``.

        The reader process never sees a half-written file because
        ``os.replace`` is atomic on Linux (the deployment target).
        """
        tmp = self.file + ".tmp"
        try:
            data.setdefault("updated_at", datetime.now().isoformat())
            data.setdefault("refresh_count", 0)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.file)
        except Exception as e:
            print(
                f"[{self.platform}] Failed to save {self.file}: {e}",
                flush=True,
            )
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    # ── convenience accessors ───────────────────────────────────────

    def get_cookie_str(self) -> str:
        return self.load().get("cookie_str", "")

    # ── health helpers ──────────────────────────────────────────────

    def set_health(self, state: str):
        data = self.load()
        if data.get("health") != state:
            data["health"] = state
            self.save(data)

    def mark_healthy(self):
        self.set_health("ok")

    def mark_unhealthy(self):
        self.set_health("expired")

    def mark_degraded(self):
        """e.g. Bilibili — has SESSDATA but missing ``bili_jct``."""
        self.set_health("degraded")

    # ── bootstrap from .env ─────────────────────────────────────────
    # NOTE: Removed — cookies now live exclusively in JSON files.
    #       bootstrap_from_env() was deleted; the refresher loop calls
    #       the CookieManager directly instead.


# ──────────────────────────────────────────────────────────────────────
# Section D — CookiePool  (primary + backup rotation)
# ──────────────────────────────────────────────────────────────────────


class CookiePool:
    """Maintain two cookie sets per platform for resilience.

    Writes to **both** files on every successful refresh.  On read, tries
    the primary first; if it is empty, falls back to the backup.  This
    gives the operator hours (or days) to fix a dead session without
    service interruption.
    """

    def __init__(self, platform: str, manager: CookieManager):
        self.platform = platform
        self.manager = manager
        base, ext = os.path.splitext(manager.file)
        self.backup_file = base + "_backup" + ext
        self._backup_mgr = _UnifiedCookieManager(platform, self.backup_file)

    def get_active(self) -> dict:
        """Return cookie data from the best available source.

        The returned dict includes a ``"source"`` key — ``"primary"`` or
        ``"backup"`` — so the caller can decide whether to alert.
        """
        data = self.manager.load()
        if data.get("cookie_str"):
            data["source"] = "primary"
            return data

        bdata = self._backup_mgr.load()
        if bdata.get("cookie_str"):
            bdata["source"] = "backup"
            print(
                f"[{self.platform}Pool] Primary empty, using backup",
                flush=True,
            )
            return bdata

        data["source"] = "primary"
        return data

    def save_both(self, data: dict):
        """Write to primary **and** backup on successful refresh."""
        self.manager.save(data)
        self._backup_mgr.save(data)
        print(
            f"[{self.platform}Pool] Saved to primary + backup",
            flush=True,
        )

    def swap(self):
        """Swap primary ↔ backup files (manual recovery helper).

        Useful when the backup contains an older-but-still-working cookie
        and the primary has been overwritten with a dead one.
        """
        tmp = self.manager.file + ".swap"
        shutil.copy2(self.manager.file, tmp)
        shutil.copy2(self.backup_file, self.manager.file)
        shutil.copy2(tmp, self.backup_file)
        os.remove(tmp)
        print(
            f"[{self.platform}Pool] Swapped primary ↔ backup",
            flush=True,
        )


# ──────────────────────────────────────────────────────────────────────
# Section E — KeepaliveChecker  (lightweight HTTP, no browser)
# ──────────────────────────────────────────────────────────────────────


class KeepaliveChecker:
    """Layer 1 of the recovery pyramid.

    Makes a lightweight HTTP request to the platform's API to determine
    whether the cookie is still alive.  If it is, the caller can skip
    the full Playwright refresh — saving ~3-8 s per cycle and reducing
    the risk of anti-bot detection.
    """

    KEEPALIVE_TIMEOUT = 15  # seconds

    def __init__(self, platform: str):
        self.platform = platform
        self.cfg = PLATFORM[platform]

    async def check(self, cookie_str: str) -> bool:
        """Return ``True`` if the cookie is still valid."""
        if not cookie_str:
            return False
        method = getattr(
            self, f"_check_{self.platform}", self._check_fallback
        )
        return await method(cookie_str)

    async def _check_douyin(self, cookie_str: str) -> bool:
        """GET ``/user/self`` — alive if not redirected to passport.

        Also performs a secondary SSR-based ``isLogin`` check by fetching
        the live page with minimal headers.  The SSR check catches cases
        where the user endpoint doesn't redirect but the session is
        still considered invalid server-side.
        """
        import aiohttp

        try:
            headers = {
                "User-Agent": self.cfg["user_agent"],
                "Cookie": cookie_str,
            }
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://www.douyin.com/user/self",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.KEEPALIVE_TIMEOUT),
                    allow_redirects=True,
                ) as resp:
                    url = str(resp.url)
                    redirect_alive = (
                        "passport" not in url
                        and "login" not in url
                        and resp.status == 200
                    )
        except Exception:
            return False

        # Secondary check: SSR isLogin for more precise validation
        # Even if /user/self didn't redirect, the session might be degraded.
        if redirect_alive:
            ssr_ok, _, _ = await self._check_douyin_ssr(cookie_str)
            return ssr_ok

        return False

    async def _check_douyin_ssr(
        self, cookie_str: str
    ) -> tuple[bool, str, str]:
        """Check Douyin login state via SSR ``defaultHeaderUserInfo.isLogin``.

        Visits ``live.douyin.com`` with minimal headers (no ``Sec-*``) to
        force server-side rendering, which embeds the real login state in
        the HTML as ``window.__INITIAL_STATE__.defaultHeaderUserInfo``.

        Returns:
            ``(is_logged_in, nickname, uid)`` tuple.
            On failure or parse error returns ``(False, '', '')``.
        """
        import aiohttp
        import re

        try:
            # Minimal headers — no Sec-Ch-Ua so Douyin returns SSR HTML
            headers = {
                "User-Agent": self.cfg["user_agent"],
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://live.douyin.com/",
                "Cookie": cookie_str,
            }
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://live.douyin.com/",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.KEEPALIVE_TIMEOUT),
                    allow_redirects=True,
                ) as resp:
                    body = await resp.text()

            # Extract isLogin + nickname from SSR payload
            m = re.search(
                r'defaultHeaderUserInfo.*?isLogin.*?(true|false).*?'
                r'nickname\\?"[,:]\\?"([^"\\]+)',
                body, re.DOTALL
            )
            is_login = False
            nickname = ""
            if m:
                is_login = m.group(1) == "true"
                nickname = m.group(2)

            # Extract uid
            uid = ""
            m_uid = re.search(
                r'defaultHeaderUserInfo.*?uid\\?"[,:]\\?"(\d+)',
                body, re.DOTALL
            )
            if m_uid:
                uid = m_uid.group(1)

            return is_login, nickname, uid

        except Exception as e:
            print(
                f"[Keepalive] Douyin SSR check failed: {e}",
                flush=True,
            )
            return False, "", ""

    async def _check_weibo(self, cookie_str: str) -> bool:
        """Check Weibo cookie validity via ``GET weibo.com``.

        A valid cookie stays on ``weibo.com``; an expired/absent one gets
        redirected to ``passport.weibo.com`` or a visitor page.  This is the
        same check that ``WeiboPoster.check_validity()`` in ``main.py`` uses
        successfully — douyin's anti-bot measures are the aggressive ones,
        not Weibo's desktop site.
        """
        import aiohttp

        try:
            headers = {
                "User-Agent": self.cfg["user_agent"],
                "Cookie": cookie_str,
            }
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://weibo.com",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.KEEPALIVE_TIMEOUT),
                    allow_redirects=True,
                ) as resp:
                    url = str(resp.url)
                    valid = (
                        "passport.weibo.com" not in url
                        and "visitor" not in url.lower()
                    )
                    if not valid:
                        print(
                            f"[Keepalive] Weibo cookie INVALID — "
                            f"redirected to {url[:80]}",
                            flush=True,
                        )
                    return valid
        except Exception as e:
            print(
                f"[Keepalive] Weibo cookie check error: {e}",
                flush=True,
            )
            return False

    async def _check_bilibili(self, cookie_str: str) -> bool:
        """Check via the nav API — ``code == 0`` means authenticated."""
        import aiohttp

        try:
            headers = {
                "User-Agent": self.cfg["user_agent"],
                "Cookie": cookie_str,
                "Referer": "https://www.bilibili.com/",
            }
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.KEEPALIVE_TIMEOUT),
                ) as resp:
                    j = await resp.json()
                    return j.get("code") == 0
        except Exception:
            return False

    async def _check_fallback(self, cookie_str: str) -> bool:
        """Generic fallback — always ``False`` (forces a browser refresh)."""
        return False


# ──────────────────────────────────────────────────────────────────────
# Section F — UnifiedCookieRefresher  (Playwright)
# ──────────────────────────────────────────────────────────────────────


class UnifiedCookieRefresher:
    """Layers 2-6 of the recovery pyramid.

    Uses a persistent Chromium profile to visit the target website,
    extract refreshed cookies, test them, and save.  If the session is
    dead or a CAPTCHA is encountered, the profile is wiped and the
    refresh retried once (Layer 3).  The backing cookie pool provides
    an automatic fallback (Layer 4).

    Usage::

        refresher = UnifiedCookieRefresher("weibo")
        success, test_ok = await refresher.refresh()
    """

    def __init__(
        self,
        platform: str,
        notifier: Optional["TelegramNotifier"] = None,
    ):
        self.platform = platform
        self.cfg = PLATFORM[platform]
        self.manager = _UnifiedCookieManager(platform)
        self.pool = CookiePool(platform, self.manager)
        self.keepalive = KeepaliveChecker(platform)
        self.notifier = notifier
        self._refresh_info: dict = {}  # populated by _browser_refresh
        self._last_keepalive_notify: float = 0  # epoch seconds, for rate-limiting

    # ── public entry point ──────────────────────────────────────────

    async def refresh(self) -> tuple[bool, bool]:
        """Run one full refresh cycle.

        Returns ``(success, test_ok)`` where *success* means the browser
        completed without crashing (even if the session was dead), and
        *test_ok* means the extracted cookie actually validates against
        the platform's API.
        """
        data = self.pool.get_active()
        old_cookie = data.get("cookie_str", "")

        # Layer 1 — keepalive check (no browser)
        if old_cookie:
            alive = await self.keepalive.check(old_cookie)
            if alive:
                print(
                    f"[{self.platform}] Cookie still valid — skipping browser",
                    flush=True,
                )

                # For Douyin, also run SSR isLogin check for richer notification
                if self.platform == "douyin":
                    is_login, nickname, uid = (
                        await self.keepalive._check_douyin_ssr(old_cookie)
                    )
                    self._refresh_info = {
                        "ssr_login": is_login,
                        "ssr_nickname": nickname,
                        "ssr_uid": uid,
                        "new_cookie_str": old_cookie,
                    }
                    if is_login:
                        print(
                            f"[Keepalive] SSR verified: logged in as {nickname}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[Keepalive] SSR reports NOT logged in "
                            f"despite no redirect — may be degraded",
                            flush=True,
                        )

                # Notify at most once per 24h so user knows the refresher
                # is alive even when no work is needed
                if self.notifier and (
                    time.time() - self._last_keepalive_notify > 86400
                ):
                    self._last_keepalive_notify = time.time()
                    self.notifier.send(
                        self._format_refresh_msg(
                            True, True, method="keepalive"
                        ),
                        state=None,
                    )
                return True, True

        # Layer 2 — browser refresh
        success, test_ok = await self._browser_refresh(old_cookie, data)

        # Layer 3 — clean-profile retry if CAPTCHA / dead session
        if not success:
            print(
                f"[{self.platform}] Browser refresh failed — "
                f"retrying with clean profile...",
                flush=True,
            )
            self._clean_profile()
            success, test_ok = await self._browser_refresh(old_cookie, data)
            if not success:
                print(
                    f"[{self.platform}] Clean-profile retry also failed — "
                    f"session likely dead",
                    flush=True,
                )

        # Layer 5 — alert (Layer 4 = CookiePool, transparent here)
        if not success and self.notifier:
            self.notifier.send(
                self._format_refresh_msg(success, test_ok, method="browser"),
                state="dead",
            )
        elif success and self.notifier:
            self.notifier.send(
                self._format_refresh_msg(success, test_ok, method="browser"),
                state=None,
            )

        return success, test_ok

    # ── browser refresh logic (extracted for retry) ─────────────────

    async def _browser_refresh(
        self, old_cookie: str, prev_data: dict
    ) -> tuple[bool, bool]:
        """Core browser refresh — no retry logic.

        Returns ``(success, test_ok)``.  Does NOT handle profile cleanup
        or retry — that is the caller's responsibility.

        *prev_data* is the data dict from ``self.pool.get_active()``,
        passed through so the finalise methods can increment
        ``refresh_count`` and preserve platform-specific fields.
        """
        profile_dir = os.path.join(
            self.cfg["base_dir"], self.cfg["profile_dir"]
        )
        print(
            f"[{self.platform}] Browser refresh "
            f"(profile={profile_dir})",
            flush=True,
        )

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print(
                f"[{self.platform}] Playwright not installed — "
                f"run ``pip install playwright && playwright install chrome``",
                flush=True,
            )
            return False, False

        try:
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    channel="chrome",
                    viewport={"width": 1920, "height": 1080},
                    user_agent=self.cfg["user_agent"],
                    args=[
                        "--disable-blink-features=AutomationControlled",
                    ],
                )

                # Seed existing cookies into the fresh browser context
                if old_cookie:
                    seed_list = self._build_seed_cookies(old_cookie)
                    if seed_list:
                        await context.add_cookies(seed_list)

                page = await context.new_page()

                # Visit each URL in the refresh sequence
                for i, url in enumerate(self.cfg["refresh_urls"]):
                    print(
                        f"[{self.platform}] Navigating to {url}",
                        flush=True,
                    )
                    await page.goto(url, wait_until="domcontentloaded")

                    nap = (
                        self.cfg["sleep_after_nav"]
                        if i == 0
                        else 3
                    )
                    await asyncio.sleep(nap)

                    # Dead-session detection
                    if self._is_dead_url(page.url):
                        print(
                            f"[{self.platform}] Session DEAD — "
                            f"redirected to {page.url[:80]}",
                            flush=True,
                        )
                        return False, False

                # CAPTCHA check (Bilibili-specific)
                if self.platform == "bilibili":
                    title = await page.title()
                    if "captcha" in title.lower() or "verify" in title.lower():
                        print(
                            f"[{self.platform}] CAPTCHA detected: {title}",
                            flush=True,
                        )
                        return False, False

                # Extract all cookies from the browser context
                raw = await context.cookies()
                new_cookies: dict[str, str] = {}
                for c in raw:
                    name = c.get("name", "")
                    value = c.get("value", "")
                    if name and value:
                        new_cookies[name] = value

                new_cookie_str = build_cookie_str(new_cookies)
                print(
                    f"[{self.platform}] Extracted {len(new_cookies)} "
                    f"cookies ({len(new_cookie_str)} chars)",
                    flush=True,
                )

                # Critical-cookie cross-check
                missing = self._missing_critical(new_cookies)
                if missing:
                    print(
                        f"[{self.platform}] WARNING: critical cookies "
                        f"missing: {missing}",
                        flush=True,
                    )

                # Stash info for Telegram notification
                old_str = prev_data.get("cookie_str", "")
                self._refresh_info = {
                    "cookie_count": len(new_cookies),
                    "missing": missing,
                    "new_cookie_str": new_cookie_str,
                    "critical_changed": self._detect_critical_change(
                        new_cookie_str, old_str, self.cfg["critical_cookies"]
                    ),
                    "odin_tt_changed": (
                        extract_cookie_value(new_cookie_str, "odin_tt")
                        != extract_cookie_value(old_str, "odin_tt")
                    ),
                }

                # Platform-specific post-processing
                if self.platform == "douyin":
                    return await self._finalise_douyin(
                        page, new_cookies, new_cookie_str, prev_data
                    )
                else:
                    return await self._finalise_generic(
                        new_cookies, new_cookie_str, prev_data, missing
                    )

        except Exception as e:
            print(
                f"[{self.platform}] Browser refresh exception: {e}",
                flush=True,
            )
            traceback.print_exc()
            return False, False

    # ── helpers ─────────────────────────────────────────────────────

    def _build_seed_cookies(self, cookie_str: str) -> list[dict]:
        """Parse *cookie_str* into Playwright ``add_cookies`` format."""
        parsed = parse_cookie_string(cookie_str)
        seed = []
        for k, v in parsed.items():
            if v:
                seed.append(
                    {
                        "name": k,
                        "value": v,
                        "domain": self.cfg["domains"][0],
                        "path": "/",
                    }
                )
        return seed

    def _is_dead_url(self, url: str) -> bool:
        if self.platform == "douyin":
            return "passport" in url or "sso.douyin.com" in url
        if self.platform == "weibo":
            return (
                "passport.weibo.com" in url or "login.sina.com.cn" in url
            )
        if self.platform == "bilibili":
            return (
                "passport.bilibili.com" in url
                or "login.bilibili.com" in url
            )
        return False

    def _missing_critical(self, cookies: dict) -> list[str]:
        return [
            k for k in self.cfg["critical_cookies"] if k not in cookies
        ]

    def _determine_health(
        self, cookies: dict, missing: list[str]
    ) -> str:
        if not missing:
            return "ok"
        # Bilibili: SESSDATA present but bili_jct missing → degraded
        if (
            self.platform == "bilibili"
            and "SESSDATA" in cookies
            and "bili_jct" in missing
        ):
            return "degraded"
        if self.platform == "weibo" and "SUB" in missing:
            return "expired"
        return "degraded"

    async def _test_cookie(self, cookie_str: str) -> bool:
        """Validate the cookie against the platform API."""
        return await self.keepalive.check(cookie_str)

    # ── Telegram message formatting ────────────────────────────────────

    @staticmethod
    def _fmt_expiry(cookie_str: str) -> str:
        """Parse session expiry from *cookie_str* for display.

        Supports:
        - Weibo ``ALF`` — unix timestamp embedded as ``ALF=<ts>`` or
          ``ALF=<ts>_<nonce>``.
        - Douyin ``sid_guard`` — URL-encoded pipe-delimited string where
          the 4th field is the expiry date (``Sat, 01-Aug-2026 ...``).

        Only returns a value when the expiry is in the future (positive
        days remaining).  Stale / past expiry is silently ignored since
        some platforms do not update the field on every page visit.
        """
        import urllib.parse

        # ── Try ALF (Weibo) ─────────────────────────────────────────
        for part in cookie_str.split(";"):
            part = part.strip()
            if part.startswith("ALF="):
                try:
                    val = part.split("=", 1)[1]
                    ts = int(val.split("_")[-1])
                    dt = datetime.fromtimestamp(ts)
                    remaining = dt - datetime.now()
                    days = remaining.days
                    if days < 0:
                        break  # stale ALF, fall through to sid_guard
                    label = f"{dt.strftime('%Y-%m-%d')} ({days}d)"
                    if days < 7:
                        label += " ⚠️"
                    return label
                except Exception:
                    break

        # ── Try sid_guard (Douyin) ──────────────────────────────────
        for part in cookie_str.split(";"):
            part = part.strip()
            if part.startswith("sid_guard="):
                try:
                    val = part.split("=", 1)[1]
                    decoded = urllib.parse.unquote(val)
                    fields = decoded.split("|")
                    if len(fields) >= 4:
                        date_str = fields[3].replace("+", " ").strip()
                        # "Sat, 01-Aug-2026 08:14:14 GMT" → parse
                        import re as _re
                        m = _re.search(
                            r"(\d+)-(\w+)-(\d+)", date_str
                        )
                        if m:
                            day, mon_str, year = (
                                m.group(1), m.group(2), m.group(3)
                            )
                            months = {
                                "Jan": "01", "Feb": "02", "Mar": "03",
                                "Apr": "04", "May": "05", "Jun": "06",
                                "Jul": "07", "Aug": "08", "Sep": "09",
                                "Oct": "10", "Nov": "11", "Dec": "12",
                            }
                            mon = months.get(mon_str[:3], "00")
                            dt = datetime(int(year), int(mon), int(day))
                            remaining = dt - datetime.now()
                            days = remaining.days
                            if days < 0:
                                return ""  # stale
                            label = f"{dt.strftime('%Y-%m-%d')} ({days}d)"
                            if days < 7:
                                label += " ⚠️"
                            return label
                except Exception:
                    pass
                break  # only one sid_guard

        return ""

    @staticmethod
    def _fmt_cookies_list(cookie_str: str, critical_keys: list[str]) -> str:
        """Build a compact status string like ``SUB ✅ SCF ✅ XSRF ✅``."""
        parsed = parse_cookie_string(cookie_str)
        parts = []
        for k in critical_keys:
            if k in parsed and parsed[k]:
                parts.append(f"{k} ✅")
            else:
                parts.append(f"{k} ❌")
        return "  ".join(parts)

    @staticmethod
    def _detect_critical_change(
        new_cookie_str: str, old_cookie_str: str, critical_keys: list[str]
    ) -> list[str]:
        """Return list of critical cookies whose value changed (session renewed)."""
        changed = []
        for key in critical_keys:
            old_val = extract_cookie_value(old_cookie_str, key)
            new_val = extract_cookie_value(new_cookie_str, key)
            if new_val and new_val != old_val:
                changed.append(key)
        return changed

    def _fmt_extra_cookies(self, cookie_str: str) -> str:
        """Platform-specific extra cookie status."""
        if self.platform == "douyin":
            ttwid = extract_cookie_value(cookie_str, "ttwid")
            odin = extract_cookie_value(cookie_str, "odin_tt")
            sessionid = extract_cookie_value(cookie_str, "sessionid")
            parts = []
            if ttwid:
                parts.append("ttwid ✅")
            if odin:
                parts.append("odin_tt ✅")
            if sessionid:
                parts.append("sessionid ✅")
            # Append session expiry from sid_guard if available
            expiry = self._fmt_expiry(cookie_str)
            if expiry:
                parts.append(f"expires {expiry}")
            return "  ".join(parts)
        if self.platform == "bilibili":
            sess = extract_cookie_value(cookie_str, "SESSDATA")
            bili_jct = extract_cookie_value(cookie_str, "bili_jct")
            buvid3 = extract_cookie_value(cookie_str, "buvid3")
            parts = []
            if sess:
                parts.append("SESSDATA ✅")
            if bili_jct:
                parts.append("bili_jct ✅")
            if buvid3:
                parts.append("buvid3 ✅")
            return "  ".join(parts)
        return ""

    def _format_refresh_msg(
        self,
        success: bool,
        test_ok: bool,
        method: str = "keepalive",
    ) -> str:
        """Build a detailed Telegram message for the refresh result."""
        ri = self._refresh_info
        ts = datetime.now().strftime("%H:%M")

        if method == "keepalive":
            # Enhanced keepalive message — include SSR login info and expiry
            if self.platform == "douyin":
                ri = self._refresh_info
                ssr_login = ri.get("ssr_login", False) if ri else False
                ssr_nick = ri.get("ssr_nickname", "") if ri else ""
                expiry = self._fmt_expiry(
                    ri.get("new_cookie_str", "") if ri else ""
                )
                nick_tag = f" {ssr_nick}" if ssr_nick else ""
                ssr_tag = " ✅" if ssr_login else " ⚠️"
                expiry_tag = f" expires {expiry}" if expiry else ""
                return (
                    f"🟢 {self.platform}{nick_tag} cookie valid"
                    f"{ssr_tag}{expiry_tag} [{ts}]"
                )
            return f"🟢 {self.platform} cookie still valid [{ts}]"

        if not success:
            return (
                f"🔴 {self.platform} cookie refresh FAILED [{ts}]\n"
                f"Session dead — check logs"
            )

        # Success with browser refresh
        expiry = self._fmt_expiry(ri.get("new_cookie_str", ""))
        ck = self._fmt_cookies_list(
            ri.get("new_cookie_str", ""), self.cfg["critical_cookies"]
        )
        extra = self._fmt_extra_cookies(ri.get("new_cookie_str", ""))
        count = ri.get("cookie_count", 0)
        critical_changed = ri.get("critical_changed", [])
        odin_changed = ri.get("odin_tt_changed", False)

        icon = "🟢" if test_ok else "🟡"

        # Build session-renewed tags
        renewed_tags = []
        for k in critical_changed:
            renewed_tags.append(f"{k} renewed")
        if odin_changed and self.platform == "douyin":
            renewed_tags.append("odin_tt rotated")
        if self.platform == "weibo" and "SUB" in critical_changed:
            renewed_tags.append("SUB renewed")
        renewed_tag = " | " + " | ".join(renewed_tags) if renewed_tags else ""

        expiry_line = f"\n  Expires: {expiry}" if expiry else ""
        extra_line = f"\n  Extras: {extra}" if extra else ""

        return (
            f"{icon} {self.platform} refreshed{renewed_tag} [{ts}]\n"
            f"  Cookies: {count} ({ck}){extra_line}{expiry_line}"
        )

    def _clean_profile(self):
        """Layer 3 — delete the browser profile and recreate.

        Handles the most common failure mode: a stale / corrupted
        browser profile that triggers CAPTCHAs.  The old profile is
        *moved* (not deleted) so an operator can inspect it later.
        """
        profile_dir = os.path.join(
            self.cfg["base_dir"], self.cfg["profile_dir"]
        )
        if os.path.exists(profile_dir):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"{profile_dir}.corrupted.{stamp}"
            shutil.move(profile_dir, backup)
            print(
                f"[{self.platform}] Corrupt profile moved → {backup}",
                flush=True,
            )
        os.makedirs(profile_dir, exist_ok=True)
        print(
            f"[{self.platform}] Fresh profile created at {profile_dir}",
            flush=True,
        )

    # ── Douyin-specific post-processing ────────────────────────────

    async def _fetch_ms_token(self) -> str:
        """Fetch a real ``msToken`` from Douyin's ``/sdk_token`` endpoint.

        **FIX:** The old code generated a random 107-character token
        locally.  msToken is a *server-issued* value — a client-side
        random string is guaranteed to be rejected by Douyin's API.
        """
        import aiohttp

        try:
            headers = {
                "User-Agent": self.cfg["user_agent"],
                "Referer": "https://www.douyin.com/",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://www.douyin.com/sdk_token",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        j = await resp.json()
                        token = j.get("data", {}).get("token", "") or ""
                        if token:
                            print(
                                "[Douyin] Fetched real msToken "
                                f"({len(token)} chars)",
                                flush=True,
                            )
                            return token
        except Exception as e:
            print(
                f"[Douyin] msToken fetch failed: {e}", flush=True
            )
        return ""

    async def _finalise_douyin(
        self,
        page,
        new_cookies: dict[str, str],
        new_cookie_str: str,
        prev_data: dict,
    ) -> tuple[bool, bool]:
        """Douyin post-step: localStorage keys, DouyinAuth, msToken."""

        # s_v_web_id alert
        if "s_v_web_id" not in new_cookies:
            msg = (
                "douyin s_v_web_id cookie MISSING — "
                "live.douyin.com may have changed"
            )
            print(f"[Douyin] WARNING: {msg}", flush=True)
            if self.notifier:
                self.notifier.send(msg, state="s_v_web_id_missing")

        # localStorage signing keys
        try:
            keys_str = (
                await page.evaluate(
                    'localStorage["security-sdk/s_sdk_crypt_sdk"]'
                )
                or ""
            )
        except Exception:
            keys_str = ""

        # Replace fake msToken with a real one
        if "msToken" not in new_cookies:
            real_token = await self._fetch_ms_token()
            if real_token:
                new_cookies["msToken"] = real_token
                new_cookie_str = build_cookie_str(new_cookies)

        # Try DouyinAuth for full derived fields
        saved = False
        try:
            sys.path.insert(
                0,
                os.path.abspath(
                    os.path.join(
                        self.cfg["base_dir"], "..", "Douyin_Spider"
                    )
                ),
            )
            from builder.auth import DouyinAuth  # type: ignore

            auth = DouyinAuth()
            auth.perepare_auth("", "", keys_str)
            auth.cookie = new_cookies
            auth.cookie_str = new_cookie_str

            save_data = {
                "cookie_str": auth.cookie_str,
                "cookie_dict": new_cookies,
                "private_key": auth.private_key or "",
                "ticket": auth.ticket or "",
                "ts_sign": auth.ts_sign or "",
                "client_cert": auth.client_cert or "",
                "ree_public_key": auth.ree_public_key or "",
                "uid": auth.uid or "",
                "refresh_count": prev_data.get("refresh_count", 0) + 1,
            }

            # Test BEFORE save (FIX: old code always set health="ok")
            test_ok = await self._test_cookie(auth.cookie_str)
            save_data["health"] = "ok" if test_ok else "expired"
            if not test_ok:
                print(
                    f"[Douyin] Post-refresh test FAILED — marking expired",
                    flush=True,
                )
            self.manager.save(save_data)
            saved = True

        except Exception as e:
            print(
                f"[Douyin] DouyinAuth unavailable ({e}) — "
                f"saving raw cookies",
                flush=True,
            )
            test_ok = await self._test_cookie(new_cookie_str)
            health = "ok" if test_ok else "expired"
            self.manager.save(
                {
                    "cookie_str": new_cookie_str,
                    "cookie_dict": new_cookies,
                    "private_key": prev_data.get("private_key", ""),
                    "ticket": prev_data.get("ticket", ""),
                    "ts_sign": prev_data.get("ts_sign", ""),
                    "client_cert": prev_data.get("client_cert", ""),
                    "ree_public_key": prev_data.get(
                        "ree_public_key", ""
                    ),
                    "uid": prev_data.get("uid", ""),
                    "health": health,
                    "refresh_count": prev_data.get("refresh_count", 0)
                    + 1,
                }
            )
            saved = True

        print(
            f"[Douyin] Refresh {'OK' if saved else 'FAIL'}"
            f" test={'PASS' if test_ok else 'FAIL'}"
            f" (#{prev_data.get('refresh_count', 0) + 1})",
            flush=True,
        )
        return saved, test_ok

    async def _finalise_generic(
        self,
        new_cookies: dict[str, str],
        new_cookie_str: str,
        prev_data: dict,
        missing: list[str],
    ) -> tuple[bool, bool]:
        """Weibo / Bilibili post-step: test-before-save.

        **FIX:** The old Bilibili refresher called ``save()`` *before*
        ``_test_cookie()`` — if the test failed, the good cookie was
        already overwritten.  We now test first and only save on PASS.

        For **Weibo**, HTTP keepalive is impossible (all endpoints redirect
        to login for non-browser requests), so ``_test_cookie()`` is
        skipped.  Browser-level checks (no passport redirect + SUB present)
        are sufficient validation — ``_is_dead_url`` and
        ``_missing_critical`` already handle this upstream.
        """
        # Weibo: trust browser extraction — HTTP test is impossible
        if self.platform == "weibo":
            test_ok = True
        else:
            test_ok = await self._test_cookie(new_cookie_str)

        if test_ok:
            health = self._determine_health(new_cookies, missing)
            self.manager.save(
                {
                    "cookie_str": new_cookie_str,
                    "health": health,
                    "refresh_count": prev_data.get("refresh_count", 0)
                    + 1,
                }
            )
            print(
                f"[{self.platform}] Refresh SUCCESS "
                f"(#{prev_data.get('refresh_count', 0) + 1}) "
                f"test=PASS  health={health}",
                flush=True,
            )
        else:
            print(
                f"[{self.platform}] Extracted cookies FAILED test — "
                f"keeping previous working cookie",
                flush=True,
            )

        return True, test_ok


# ──────────────────────────────────────────────────────────────────────
# Section G — PM2 entry points
# ──────────────────────────────────────────────────────────────────────


async def _refresher_loop(platform: str, interval: int | None = None):
    """Standalone loop run by each PM2 process.

    *interval* overrides the platform default (used when PM2 passes
    an argument like ``21600`` via ``sys.argv``).

    Polls cookie health every 60s so that when another process (e.g.
    ``streammonitor``) marks the cookie as ``"expired"`` via
    ``mark_unhealthy()``, the refresher reacts within a minute instead
    of waiting for the next full interval.
    """
    cfg = PLATFORM[platform]
    if interval is None:
        interval = cfg["refresh_interval"]

    try:
        from telegram_notifier import TelegramNotifier

        notifier = TelegramNotifier()
    except Exception:
        notifier = None

    refresher = UnifiedCookieRefresher(platform, notifier)

    print(
        f"[{platform}] Starting — interval={interval}s "
        f"({interval / 3600:.1f}h)",
        flush=True,
    )

    # Run immediately so we don't wait N hours for initial data
    print(f"[{platform}] Running initial refresh...", flush=True)
    await refresher.refresh()

    POLL = 60          # wake every 60s to check health + keepalive
    KA_FAILS = 0       # consecutive keepalive failures (guard against flapping)
    KA_FAIL_LIMIT = 3  # trigger refresh only after N consecutive failures

    while True:
        # Sleep in short increments so we can detect health=expired
        # between the long scheduled intervals.
        for _ in range(interval // POLL):
            await asyncio.sleep(POLL)
            data = refresher.pool.get_active()
            cookie_str = data.get("cookie_str", "")
            health = data.get("health", "")

            # Active keepalive check — test the cookie against the
            # platform's API.  This catches silent expiry faster than
            # waiting for streammonitor or a posting failure.
            # Uses a consecutive-failure threshold to avoid triggering
            # an expensive Playwright refresh on transient network blips.
            if cookie_str:
                alive = await refresher.keepalive.check(cookie_str)
                if not alive:
                    KA_FAILS += 1
                    if KA_FAILS >= KA_FAIL_LIMIT:
                        print(
                            f"[{platform}] Keepalive failed "
                            f"{KA_FAILS}x consecutively — refreshing",
                            flush=True,
                        )
                        KA_FAILS = 0
                        refresher.manager.mark_unhealthy()
                        await refresher.refresh()
                        break  # restart the interval timer
                else:
                    KA_FAILS = 0  # reset on success

            # Passive check — streammonitor or another process marked
            # the cookie expired via mark_unhealthy().
            if health == "expired":
                print(
                    f"[{platform}] Health=expired detected — "
                    f"forcing immediate refresh",
                    flush=True,
                )
                KA_FAILS = 0
                await refresher.refresh()
                break  # restart the interval timer from zero
        else:
            # Normal scheduled refresh (no emergency trigger this cycle)
            print(
                f"\n[{platform}] Scheduled refresh at "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                flush=True,
            )
            await refresher.refresh()


def main_douyin():
    asyncio.run(_refresher_loop("douyin"))


def main_weibo():
    asyncio.run(_refresher_loop("weibo"))


def main_bilibili():
    asyncio.run(_refresher_loop("bilibili"))


# ──────────────────────────────────────────────────────────────────────
# Section H — Backward-compatible aliases
# ──────────────────────────────────────────────────────────────────────
# These match the old class names and method signatures so that
# ``main.py`` and other existing code can import from the old module
# paths without modification.


class _CompatCookieManager:
    """Wraps ``CookieManager`` with a fixed platform, like the old class.

    Subclassed below per platform so the old import paths still work:
    ``cookie_manager.CookieManager``, ``weibo_cookie_manager.WeiboCookieManager``,
    ``bilibili_cookie_manager.BilibiliCookieManager``.
    """

    def __init__(self, platform: str, cookies_file: str | None = None):
        self._mgr = _UnifiedCookieManager(platform, cookies_file)
        self._platform = platform

    def load(self) -> dict:
        return self._mgr.load()

    def save(self, data: dict):
        self._mgr.save(data)

    def get_cookie_str(self) -> str:
        return self._mgr.get_cookie_str()

    def mark_healthy(self):
        self._mgr.mark_healthy()

    def mark_unhealthy(self):
        self._mgr.mark_unhealthy()


class DouyinCookieManager(_CompatCookieManager):
    """Backward-compat: ``cookie_manager.CookieManager``."""

    def __init__(self, cookies_file: str | None = None):
        super().__init__("douyin", cookies_file)

    def get_auth_data(self) -> dict:
        return self._mgr.load()

class WeiboCookieManager(_CompatCookieManager):
    """Backward-compat: ``weibo_cookie_manager.WeiboCookieManager``."""

    def __init__(self, cookies_file: str | None = None):
        super().__init__("weibo", cookies_file)

    @staticmethod
    def extract_xsrf(cookie_str: str) -> str:
        """Extract ``XSRF-TOKEN`` (Weibo CSRF protection)."""
        return extract_cookie_value(
            cookie_str, "XSRF-TOKEN", case_insensitive=True
        )


class BilibiliCookieManager(_CompatCookieManager):
    """Backward-compat: ``bilibili_cookie_manager.BilibiliCookieManager``."""

    def __init__(self, cookies_file: str | None = None):
        super().__init__("bilibili", cookies_file)

    @staticmethod
    def extract_csrf(cookie_str: str) -> str:
        """Extract ``bili_jct`` (Bilibili CSRF token)."""
        return extract_cookie_value(cookie_str, "bili_jct")


# Alias for the old Douyin-specific ``CookieManager`` that ``main.py`` imports
CookieManager = DouyinCookieManager  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# Section I — Standalone entry (replaces old shim files)
# ═══════════════════════════════════════════════════════════════════════
# Usage from PM2:
#   cookies.py douyin [interval]
#   cookies.py weibo  [interval]
#   cookies.py bilibili [interval]

if __name__ == "__main__":
    platform = sys.argv[1] if len(sys.argv) > 1 else "douyin"
    if platform not in PLATFORM:
        print(f"Unknown platform '{platform}'. Choose: {', '.join(PLATFORM)}", flush=True)
        sys.exit(1)
    if len(sys.argv) > 2:
        try:
            interval = int(sys.argv[2])
        except ValueError:
            interval = PLATFORM[platform]["refresh_interval"]
    else:
        interval = PLATFORM[platform]["refresh_interval"]
    asyncio.run(_refresher_loop(platform, interval))
