#!/usr/bin/env python3
# coding=utf-8
"""Cookie state management with atomic file I/O.

Single source of truth for Douyin cookies shared between the
StreamMonitor (reader) and CookieRefresher (writer).
"""

import json
import os
from datetime import datetime

from dotenv import load_dotenv

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.json")


class CookieManager:
    """Read/write cookies.json with atomic writes and bootstrap from .env."""

    def __init__(self, cookies_file=None):
        self.file = cookies_file or COOKIES_FILE

    # ── defaults ────────────────────────────────────────────────────────
    @staticmethod
    def _defaults():
        return {
            "cookie_str": "",
            "cookie_dict": {},
            "private_key": "",
            "ticket": "",
            "ts_sign": "",
            "client_cert": "",
            "ree_public_key": "",
            "uid": "",
            "updated_at": "",
            "health": "unknown",
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
                f"[CookieManager] Failed to load {self.file}: {e}", flush=True
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
            print(f"[CookieManager] Failed to save {self.file}: {e}", flush=True)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    # ── convenience accessors ───────────────────────────────────────────
    def get_cookie_str(self):
        return self.load().get("cookie_str", "")

    def get_auth_data(self):
        return self.load()

    # ── health helpers ──────────────────────────────────────────────────
    def mark_unhealthy(self):
        data = self.load()
        if data.get("health") != "expired":  # only touch if changed
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
        """Seed cookies.json from .env DY_LIVE_COOKIES on first run.

        Returns the loaded data dict.  Safe to call on every startup —
        only writes if cookies.json does not exist.
        """
        if os.path.exists(COOKIES_FILE):
            return CookieManager().load()

        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)

        cookie_str = os.getenv("DY_LIVE_COOKIES", "") or os.getenv(
            "DY_COOKIES", ""
        )
        if not cookie_str:
            print(
                "[CookieManager] No DY_LIVE_COOKIES in .env, "
                "cookies.json will be empty",
                flush=True,
            )
            return CookieManager().load()

        # Parse cookie string into dict
        cookie_dict = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookie_dict[k.strip()] = v.strip()

        data = CookieManager._defaults()
        data.update(
            {
                "cookie_str": cookie_str,
                "cookie_dict": cookie_dict,
                "health": "ok",
                "refresh_count": 0,
            }
        )
        mgr = CookieManager()
        mgr.save(data)
        print(
            f"[CookieManager] Bootstrapped cookies.json from .env "
            f"({len(cookie_dict)} cookies)",
            flush=True,
        )
        return data
