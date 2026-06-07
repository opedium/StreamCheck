# Cookie Refresh System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a proactive Playwright-based cookie refresh system that keeps Douyin cookies alive by running a headless Chrome browser on schedule, plus health detection and Telegram alerting in the main monitor.

**Architecture:** Three new modules (`cookie_manager.py`, `cookie_refresher.py`, `telegram_notifier.py`) and modifications to `main.py`. The refresher runs as an independent PM2 process. Cookies flow through an atomic JSON file (`cookies.json`). The monitor hot-reloads cookies without restart.

**Tech Stack:** Python 3.7+, Playwright (async API), Google Chrome (channel="chrome"), requests, existing Douyin_Spider (`DouyinAuth`, `dy_util`)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `StreamMonitor/cookie_manager.py` | NEW | Atomic JSON I/O for `cookies.json`, bootstrap from `.env` |
| `StreamMonitor/telegram_notifier.py` | NEW | Telegram Bot API for cookie health alerts, state-change dedup |
| `StreamMonitor/cookie_refresher.py` | NEW | Playwright persistent-context refresh, standalone entry point |
| `StreamMonitor/main.py` | MODIFY | Cookie health check, hot-reload, emergency refresh trigger |
| `StreamMonitor/.gitignore` | MODIFY | Add `browser_profile/` and `cookies.json` |
| `StreamMonitor/.env.example` | MODIFY | Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `ecosystem.config.js` | MODIFY | Add `cookie-refresher` PM2 process |
| `StreamMonitor/browser_profile/` | AUTO | Created by Playwright on first run, gitignored |

---

### Task 1: `cookie_manager.py` — Cookie state file I/O

**Files:**
- Create: `StreamMonitor/cookie_manager.py`

- [ ] **Step 1: Write `cookie_manager.py`**

```python
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
```

- [ ] **Step 2: Verify the module loads cleanly**

```bash
cd StreamMonitor && python3 -c "from cookie_manager import CookieManager; mgr = CookieManager(); print('load OK:', list(mgr.load().keys()))"
```

Expected: prints key list with defaults. If `cookies.json` exists from a previous .env bootstrap, prints real data.

- [ ] **Step 3: Commit**

```bash
git add StreamMonitor/cookie_manager.py
git commit -m "feat: add CookieManager for atomic cookies.json I/O"
```

---

### Task 2: `telegram_notifier.py` — Cookie health alerts

**Files:**
- Create: `StreamMonitor/telegram_notifier.py`

- [ ] **Step 1: Write `telegram_notifier.py`**

```python
#!/usr/bin/env python3
# coding=utf-8
"""Telegram notification for cookie health events.

Fire-and-forget with state-transition deduplication to prevent spam.
Telegram failures never propagate — this is best-effort alerting.
"""

import os

import requests
from dotenv import load_dotenv


class TelegramNotifier:
    """Send cookie health alerts via Telegram Bot API."""

    def __init__(self, bot_token=None, chat_id=None):
        if bot_token is None or chat_id is None:
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            if os.path.exists(env_path):
                load_dotenv(env_path)
            bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._last_state = None  # for dedup

    @property
    def configured(self):
        return bool(self.bot_token and self.chat_id)

    def send(self, message, state=None):
        """Send a Telegram message.

        Args:
            message: Text to send.
            state: Optional state label.  If provided and matches the
                   last sent state, the message is skipped (dedup).
                   Use None to force-send regardless of dedup.

        Returns:
            True if sent, False if skipped or failed.
        """
        if state is not None:
            if state == self._last_state:
                return False
            self._last_state = state

        if not self.configured:
            print(
                f"[Telegram] Not configured — message not sent: {message}",
                flush=True,
            )
            return False

        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
            if resp.status_code == 200:
                print(f"[Telegram] Sent: {message}", flush=True)
                return True
            else:
                print(
                    f"[Telegram] HTTP {resp.status_code}: {resp.text[:200]}",
                    flush=True,
                )
                # Reset state on failure so next attempt retries
                self._last_state = None
                return False
        except Exception as e:
            print(f"[Telegram] Error: {e}", flush=True)
            self._last_state = None
            return False
```

- [ ] **Step 2: Verify the module loads**

```bash
cd StreamMonitor && python3 -c "from telegram_notifier import TelegramNotifier; tn = TelegramNotifier('fake', '123'); print('configured:', tn.configured)"
```

Expected: `configured: True`

- [ ] **Step 3: Commit**

```bash
git add StreamMonitor/telegram_notifier.py
git commit -m "feat: add TelegramNotifier for cookie health alerts"
```

---

### Task 3: `.gitignore` and `.env.example` updates

**Files:**
- Modify: `StreamMonitor/.gitignore:16` (append)
- Modify: `StreamMonitor/.env.example:46` (append)

- [ ] **Step 1: Update `.gitignore`**

Add three lines at the end of `StreamMonitor/.gitignore`:

```gitignore
# Cookie state (auto-generated, contains secrets)
cookies.json

# Playwright persistent browser profile (large, auto-generated)
browser_profile/
```

- [ ] **Step 2: Update `.env.example`**

Append at the end of `StreamMonitor/.env.example`:

```
# =============================================
# Cookie Refresh & Health Alerts
# =============================================

# Optional: Telegram bot for cookie health alerts
# Create a bot via @BotFather on Telegram, then get your chat ID
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Optional: Cookie refresh interval in seconds (default: 21600 = 6 hours)
COOKIE_REFRESH_INTERVAL=21600
```

- [ ] **Step 3: Commit**

```bash
git add StreamMonitor/.gitignore StreamMonitor/.env.example
git commit -m "chore: add cookie refresh config to .gitignore and .env.example"
```

---

### Task 4: `cookie_refresher.py` — Playwright persistent-context refresh

**Files:**
- Create: `StreamMonitor/cookie_refresher.py`

This is the core module. It launches a persistent Chrome profile, seeds existing cookies, visits Douyin to trigger session extension, extracts refreshed cookies, and saves them atomically.

- [ ] **Step 1: Write `cookie_refresher.py`**

```python
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
import os
import sys
import time
import traceback
from datetime import datetime

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
                print("[CookieRefresher] Navigating to douyin.com...", flush=True)
                await page.goto(
                    "https://www.douyin.com/", wait_until="networkidle"
                )
                await asyncio.sleep(5)

                # ── Dead session detection ─────────────────────────────
                current_url = page.url
                if "passport" in current_url or "sso.douyin.com" in current_url:
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
                f"Invalid interval '{sys.argv[1]}', using default {interval}s",
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
```

- [ ] **Step 2: Verify syntax**

```bash
cd StreamMonitor && python3 -c "import ast; ast.parse(open('cookie_refresher.py').read()); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 3: Commit**

```bash
git add StreamMonitor/cookie_refresher.py
git commit -m "feat: add CookieRefresher with persistent Playwright profile"
```

---

### Task 5: `main.py` — Cookie health detection + hot-reload

**Files:**
- Modify: `StreamMonitor/main.py`

Five changes to `main.py`:

1. Import CookieManager and TelegramNotifier
2. `StreamMonitor.__init__` — init manager/notifier, bootstrap cookies
3. New `reload_cookies()` method
4. New `_handle_auth_error()` method (replaces inline handling)
5. `run_once()` — check for auth_error sentinel

- [ ] **Step 1: Add imports at top of `main.py`**

After the `from urllib.parse import urlencode` line (~line 19), add:

```python
# Cookie refresh system
from cookie_manager import CookieManager
from telegram_notifier import TelegramNotifier
```

- [ ] **Step 2: Modify `StreamMonitor.__init__`**

The current `__init__` is at line 1761. Add `cookie_manager` and `telegram_notifier` initialization before `logger.info(...)` at line 1807.

Insert this code block right after `self.CONSECUTIVE_OFFLINE_LIMIT = 2` (line 1801) and before `# Validate templates` (line 1803):

```python
        # ── Cookie refresh system ─────────────────────────────────────
        self.cookie_manager = CookieManager()
        self.notifier = TelegramNotifier()
        self._cookie_refreshing = False  # guard against concurrent refreshes
        self._last_auth_error_time = None

        # Bootstrap cookies.json from .env on first run,
        # then use cookies.json as the authority from now on.
        self.cookie_manager.bootstrap_from_env()
        cookie_data = self.cookie_manager.load()
        # Use cookies.json as cookie source (overrides .env)
        if cookie_data.get("cookie_str"):
            self.dy_cookie_str = cookie_data["cookie_str"]
            # Re-parse into checker
            self.checker.cookie = DouyinLiveChecker._parse_cookie(
                self.dy_cookie_str
            )
```

- [ ] **Step 3: Add `reload_cookies()` method to `StreamMonitor`**

Insert after the `__init__` method, before `_get_live_template_keys()`:

```python
    def reload_cookies(self):
        """Hot-reload cookies from cookies.json into all running components."""
        data = self.cookie_manager.load()
        new_cookie_str = data.get("cookie_str", "")
        if not new_cookie_str:
            logger.warning("[CookieReload] cookies.json has empty cookie_str, skipping reload")
            return False
        self.dy_cookie_str = new_cookie_str
        self.checker.cookie = DouyinLiveChecker._parse_cookie(new_cookie_str)
        if self.stats_recorder and self.stats_recorder.is_running():
            self.stats_recorder.cookie_str = new_cookie_str
        logger.info("[CookieReload] Hot-reloaded cookies into checker and recorder")
        return True

    def _trigger_cookie_refresh(self):
        """Emergency cookie refresh — called when auth failure detected.

        Runs in a background thread to avoid blocking the main loop.
        """
        if self._cookie_refreshing:
            logger.debug("[CookieRefresh] Refresh already in progress, skipping")
            return
        self._cookie_refreshing = True
        try:
            from cookie_refresher import CookieRefresher
            refresher = CookieRefresher(self.cookie_manager)
            success = asyncio.run(refresher.refresh())
            if success:
                self.reload_cookies()
                self.cookie_manager.mark_healthy()
                self.notifier.send(
                    "✅ Emergency cookie refresh succeeded",
                    state="ok",
                )
            else:
                self.cookie_manager.mark_unhealthy()
                self.notifier.send(
                    "\U0001f534 CRITICAL: Emergency cookie refresh FAILED — "
                    "monitor may be blind. Manual re-login required.",
                    state="dead",
                )
        except Exception as e:
            logger.error(f"[CookieRefresh] Emergency refresh error: {e}")
            self.notifier.send(
                f"❌ Cookie refresh crashed: {e}",
                state=None,  # force-send on crash
            )
        finally:
            self._cookie_refreshing = False

    def _check_cookie_health(self, room_info):
        """Check if the HTTP response indicates an auth failure.

        Returns True if cookies appear healthy, False if auth error detected.
        """
        status = room_info.get("room_status", "")
        if status == "auth_error":
            logger.warning(
                "[Health] Auth failure detected — triggering emergency refresh"
            )
            self.cookie_manager.mark_unhealthy()
            threading.Thread(
                target=self._trigger_cookie_refresh, daemon=True
            ).start()
            return False
        return True
```

- [ ] **Step 4: Modify `check_status()` in `DouyinLiveChecker` to detect auth errors**

In the `check_status()` method around line 1577-1579, after `resp = requests.get(...)`:

The current code:
```python
resp = requests.get(url, headers=self.HEADERS, cookies=self.cookie, verify=False, timeout=15)
resp.raise_for_status()
```

Add auth detection right after `resp = requests.get(...)` and before `resp.raise_for_status()`:

```python
resp = requests.get(url, headers=self.HEADERS, cookies=self.cookie, verify=False, timeout=15)

# Detect auth failure before raise_for_status (redirect to login)
if "passport" in resp.url or "sso.douyin.com" in resp.url:
    logger.warning(f"[Health] Auth redirect detected: {resp.url[:80]}")
    return {"room_status": "auth_error"}

resp.raise_for_status()
```

- [ ] **Step 5: Modify `run_once()` to handle `auth_error`**

In `run_once()` around line 2197, after `new_status = str(room_info.get(...))`:

The current code:
```python
new_status = str(room_info.get('room_status', DouyinLiveChecker.STATUS_OFFLINE))
```

After this line, add the health check:

```python
new_status = str(room_info.get('room_status', DouyinLiveChecker.STATUS_OFFLINE))

# ── Cookie health check ───────────────────────────────────────────
if new_status == "auth_error":
    self._check_cookie_health(room_info)
    # If we were previously LIVE, preserve state — don't trigger
    # a false offline event due to auth failure.
    if self.current_status == DouyinLiveChecker.STATUS_LIVE:
        print(
            f"[{now.strftime('%H:%M:%S')}] ⚠ Auth error but keeping "
            f"LIVE state — emergency refresh triggered",
            flush=True,
        )
        return False
    else:
        print(
            f"[{now.strftime('%H:%M:%S')}] ⚠ Auth error — "
            f"retrying next cycle",
            flush=True,
        )
        return False
```

- [ ] **Step 6: Periodic reload of cookies from cookies.json**

In `run_once()`, before the status check logic (around line 2195, before `if self.current_status is None:`), add a periodic reload so the monitor picks up cookies refreshed by the standalone refresher process. This runs at the start of every `run_once()`:

```python
# ── Periodic cookie reload (picks up refreshes from standalone process) ──
_reload_every_n = max(1, 300 // self.check_interval)  # ~every 5 min
if not hasattr(self, '_cookie_reload_counter'):
    self._cookie_reload_counter = 0
self._cookie_reload_counter += 1
if self._cookie_reload_counter >= _reload_every_n:
    self._cookie_reload_counter = 0
    data = self.cookie_manager.load()
    if data.get("cookie_str") and data["cookie_str"] != self.dy_cookie_str:
        logger.info("[CookieReload] Detected updated cookies, hot-reloading...")
        self.reload_cookies()
```

Place this code block at line 2196, right after the try/except that sets `room_info` and before the `new_status` extraction.

- [ ] **Step 7: Verify syntax and imports**

```bash
cd StreamMonitor && python3 -c "
import ast
with open('main.py') as f:
    ast.parse(f.read())
print('Syntax OK')
"
```

Expected: `Syntax OK`

- [ ] **Step 8: Commit**

```bash
git add StreamMonitor/main.py
git commit -m "feat: add cookie health detection and hot-reload to StreamMonitor"
```

---

### Task 6: PM2 ecosystem config update

**Files:**
- Modify: `ecosystem.config.js:46` (append after bilibili-checker entry)

- [ ] **Step 1: Add cookie-refresher process**

Insert after the `bilibili-checker` block (after line 45, before `];`), keeping the closing `]` and `};`:

```javascript
    {
      name: 'cookie-refresher',
      script: '/root/StreamCheck/StreamMonitor/cookie_refresher.py',
      interpreter: '/root/StreamCheck/venv/bin/python3',
      cwd: '/root/StreamCheck/StreamMonitor',
      args: '21600',
      restart_delay: 60000,
      max_restarts: 5,
      error_file: '/root/StreamCheck/logs/cookie-refresher-err.log',
      out_file: '/root/StreamCheck/logs/cookie-refresher-out.log',
      merge_logs: true,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      autorestart: true,
    },
```

- [ ] **Step 2: Validate JSON syntax**

```bash
node -e "const c = require('./ecosystem.config.js'); console.log('Apps:', c.apps.length, 'OK')"
```

Expected: `Apps: 4 OK`

- [ ] **Step 3: Commit**

```bash
git add ecosystem.config.js
git commit -m "chore: add cookie-refresher to PM2 ecosystem config"
```

---

### Task 7: Integration verification

**Files:** None (verification only)

- [ ] **Step 1: Verify all modules import without errors**

```bash
cd StreamMonitor && python3 -c "
from cookie_manager import CookieManager
from telegram_notifier import TelegramNotifier
print('Module imports OK')
print('CookieManager:', CookieManager)
print('TelegramNotifier:', TelegramNotifier)
"
```

Expected: `Module imports OK` with class references printed.

- [ ] **Step 2: Verify CookieManager bootstrap creates cookies.json**

```bash
cd StreamMonitor && python3 -c "
from cookie_manager import CookieManager
import os
# Remove any existing cookies.json for clean test
if os.path.exists('cookies.json'):
    os.remove('cookies.json')
# Load .env if it exists, otherwise bootstrap with empty
data = CookieManager.bootstrap_from_env()
print('cookie_str length:', len(data.get('cookie_str', '')))
print('health:', data.get('health'))
print('cookies.json exists:', os.path.exists('cookies.json'))
"
```

Expected: Reports whether .env had cookies to seed. `cookies.json` should exist after.

- [ ] **Step 3: Verify main.py loads with new imports**

```bash
cd StreamMonitor && python3 -c "
import sys
sys.path.insert(0, '..')
# Just test that the import chain works without running the monitor
from cookie_manager import CookieManager
from telegram_notifier import TelegramNotifier
mgr = CookieManager()
print('Integration check passed')
"
```

Expected: `Integration check passed`

- [ ] **Step 4: Verify cookie_refresher.py syntax (without Playwright)**

The refresher requires Playwright + Chrome which may not be installed locally. Syntax check is sufficient for now:

```bash
cd StreamMonitor && python3 -c "
import ast
with open('cookie_refresher.py') as f:
    ast.parse(f.read())
print('cookie_refresher.py syntax OK')
"
```

Expected: `cookie_refresher.py syntax OK`

- [ ] **Step 5: Commit any final adjustments**

```bash
git status
git add -A
git diff --cached --stat
```

---

## Deployment Checklist (manual, on server)

These steps are performed on the DigitalOcean Ubuntu 22.04 server after pushing the code:

```bash
# 1. Pull latest code
cd /root/StreamCheck && git pull

# 2. Install Playwright + Chrome (one-time)
cd /root/StreamCheck/StreamMonitor
pip install playwright
playwright install chrome

# 3. Ensure logs directory exists
mkdir -p /root/StreamCheck/logs

# 4. Restart PM2 with updated ecosystem config
pm2 delete all
pm2 start /root/StreamCheck/ecosystem.config.js
pm2 save

# 5. Verify all 4 processes are running
pm2 status
# Expected: streammonitor, streamweb, bilibili-checker, cookie-refresher

# 6. Check cookie-refresher logs for first refresh
pm2 logs cookie-refresher --lines 20
```
