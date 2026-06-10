#!/usr/bin/env python3
# coding=utf-8
"""Bilibili cookie state management with atomic file I/O.

Single source of truth for Bilibili web cookies shared between the
Bilibili checker (reader) and BilibiliCookieRefresher (writer).
"""

import json
import os
from datetime import datetime

from dotenv import load_dotenv

BILIBILI_COOKIES_FILE = os.path.join(os.path.dirname(__file__), "bilibili_cookies.json")


class BilibiliCookieManager:
    """Read/write bilibili_cookies.json with atomic writes and bootstrap from .env."""

    def __init__(self, cookies_file=None):
        self.file = cookies_file or BILIBILI_COOKIES_FILE

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
                f"[BilibiliCookieManager] Failed to load {self.file}: {e}", flush=True
            )
        return defaults

    def save(self, data):
        """Atomic write via temp file + os.replace.

        The checker never sees a half-written file because os.replace
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
            print(f"[BilibiliCookieManager] Failed to save {self.file}: {e}", flush=True)
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
        """Seed bilibili_cookies.json from .env BILI_COOKIE on first run.

        Returns the loaded data dict.  Safe to call on every startup —
        only writes if bilibili_cookies.json does not exist.
        """
        if os.path.exists(BILIBILI_COOKIES_FILE):
            return BilibiliCookieManager().load()

        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)

        cookie_str = os.getenv("BILI_COOKIE", "")
        if not cookie_str:
            print(
                "[BilibiliCookieManager] No BILI_COOKIE in .env, "
                "bilibili_cookies.json will be empty",
                flush=True,
            )
            return BilibiliCookieManager().load()

        data = BilibiliCookieManager._defaults()
        data.update(
            {
                "cookie_str": cookie_str,
                "health": "ok",
                "refresh_count": 0,
            }
        )
        mgr = BilibiliCookieManager()
        mgr.save(data)
        print(
            f"[BilibiliCookieManager] Bootstrapped bilibili_cookies.json from .env "
            f"({len(cookie_str)} chars)",
            flush=True,
        )
        return data

    # ── CSRF helper ─────────────────────────────────────────────────────
    @staticmethod
    def extract_csrf(cookie_str):
        """Extract bili_jct (CSRF token) value from a Bilibili cookie string."""
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == "bili_jct":
                    return v.strip()
        return ""
