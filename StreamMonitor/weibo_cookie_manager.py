#!/usr/bin/env python3
# coding=utf-8
"""Weibo cookie state management with atomic file I/O.

Single source of truth for Weibo web cookies shared between the
StreamMonitor WeiboPoster (reader), Bilibili checker WeiboPoster (reader),
and WeiboCookieRefresher (writer).
"""

import json
import os
from datetime import datetime

from dotenv import load_dotenv

WEIBO_COOKIES_FILE = os.path.join(os.path.dirname(__file__), "weibo_cookies.json")


class WeiboCookieManager:
    """Read/write weibo_cookies.json with atomic writes and bootstrap from .env."""

    def __init__(self, cookies_file=None):
        self.file = cookies_file or WEIBO_COOKIES_FILE

    # ── defaults ────────────────────────────────────────────────────────
    @staticmethod
    def _defaults():
        return {
            "cookie_str": "",
            "health": "unknown",
            "updated_at": "",
            "refresh_count": 0,
        }

    # ── load / save ─────────────────────────────────────────────────────
    def load(self):
        """Return cookie data dict.  Never raises — returns defaults on failure."""
        defaults = self._defaults()
        try:
            if os.path.exists(self.file):
                with open(self.file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                defaults.update(data)
        except (json.JSONDecodeError, IOError) as e:
            print(
                f"[WeiboCookieManager] Failed to load {self.file}: {e}", flush=True
            )
        return defaults

    def save(self, data):
        """Atomic write via temp file + os.replace.

        The monitor never sees a half-written file because os.replace
        is atomic on Linux (the deployment target).
        """
        tmp = self.file + ".tmp"
        try:
            data.setdefault("updated_at", datetime.now().isoformat())
            data.setdefault("refresh_count", 0)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.file)
        except Exception as e:
            print(f"[WeiboCookieManager] Failed to save {self.file}: {e}", flush=True)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    # ── convenience accessors ───────────────────────────────────────────
    def get_cookie_str(self):
        return self.load().get("cookie_str", "")

    # ── health helpers ──────────────────────────────────────────────────
    def mark_unhealthy(self):
        data = self.load()
        if data.get("health") != "expired":
            data["health"] = "expired"
            self.save(data)

    def mark_healthy(self):
        data = self.load()
        if data.get("health") != "ok":
            data["health"] = "ok"
            self.save(data)

    # ── bootstrap ───────────────────────────────────────────────────────
    @staticmethod
    def bootstrap_from_env():
        """Seed weibo_cookies.json from .env WEIBO_COOKIE on first run.

        Returns the loaded data dict.  Safe to call on every startup —
        only writes if weibo_cookies.json does not exist.
        """
        if os.path.exists(WEIBO_COOKIES_FILE):
            return WeiboCookieManager().load()

        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)

        cookie_str = os.getenv("WEIBO_COOKIE", "")
        if not cookie_str:
            print(
                "[WeiboCookieManager] No WEIBO_COOKIE in .env, "
                "weibo_cookies.json will be empty",
                flush=True,
            )
            return WeiboCookieManager().load()

        data = WeiboCookieManager._defaults()
        data.update(
            {
                "cookie_str": cookie_str,
                "health": "ok",
                "refresh_count": 0,
            }
        )
        mgr = WeiboCookieManager()
        mgr.save(data)
        print(
            f"[WeiboCookieManager] Bootstrapped weibo_cookies.json from .env "
            f"({len(cookie_str)} chars)",
            flush=True,
        )
        return data

    # ── XSRF helper ─────────────────────────────────────────────────────
    @staticmethod
    def extract_xsrf(cookie_str):
        """Extract XSRF-TOKEN value from a Weibo cookie string."""
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip().upper()
                if k in ("XSRF-TOKEN", "XSRF_TOKEN"):
                    return v.strip()
        return ""
