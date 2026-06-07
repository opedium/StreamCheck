# Cookie Refresh System — Design Spec

**Date:** 2026-06-08
**Status:** Approved
**Scope:** StreamMonitor — Douyin cookie management

## Problem

Douyin login cookies expire every few days. The current system stores cookies as a static string in `.env`. When they expire, the StreamMonitor silently fails — live status checks return empty, WebSocket stats stop, and gift/member protobuf data is lost. Recovery requires manually extracting cookies from a browser and updating `.env`, causing monitoring gaps.

## Solution Overview

A proactive Playwright-based cookie refresh system that runs alongside the StreamMonitor. It loads existing cookies into a headless Chromium browser, visits Douyin to keep the session alive, and extracts refreshed tokens. The monitor hot-reloads cookies without restarting. A Telegram notifier alerts on failure.

## Decision: Playwright Session Refresh (Approach A)

**Why this approach:**
- Douyin's session cookies have sliding expiration — each valid request from a real browser extends the window
- The existing codebase already has Playwright-based cookie extraction (`cookie_util.py`, `login_api.py`) — we're wrapping it, not reinventing
- Running the refresher every 6–12 hours should keep cookies alive for weeks/months instead of days
- Separating refresher from monitor means Playwright crashes don't take down the main service

**Trade-off accepted:** Requires Chromium on the server (~300MB). Headless operation may occasionally trigger CAPTCHAs; when it does, the Telegram alert tells the operator to manually re-login.

## Architecture

```
┌─────────────────────────┐
│  cookie_refresher.py    │  PM2-managed, runs every N hours
│  (separate process)     │
│                         │
│  1. Reads cookies.json  │
│  2. Launches Chromium   │
│     with existing       │
│     cookies seeded      │
│  3. Visits douyin.com   │
│     browses pages       │
│  4. Detects dead        │
│     session (redirect)  │
│  5. Extracts fresh      │
│     cookies + keys      │
│  6. Atomic write to     │
│     cookies.json        │
│  7. Sends Telegram      │
│     status notification │
└───────────┬─────────────┘
            │ atomic write
            ▼
┌─────────────────────────┐
│  cookies.json           │  Single source of truth
│  (shared state file)    │  {cookie_str, cookie_dict,
│                         │   private_key, ticket, ...}
└───────────┬─────────────┘
            │ reads each cycle
            ▼
┌─────────────────────────┐
│  main.py                │  StreamMonitor (existing)
│  (modified)             │
│                         │
│  - CookieManager.load() │
│    on each check cycle  │
│  - Health check:        │
│    detects "passport"   │
│    redirect or auth     │
│    error → marks        │
│    cookies unhealthy    │
│  - Triggers emergency   │
│    refresh on auth fail │
│  - Falls back to last-  │
│    good cookies during  │
│    refresh window       │
└─────────────────────────┘
```

## Components

### 1. `cookie_manager.py` (NEW — ~80 lines)

Shared cookie file I/O with atomic writes. Single class: `CookieManager`.

**File path:** `StreamMonitor/cookie_manager.py`

**Responsibilities:**
- `load()` — read and parse `cookies.json`, return dict with defaults on missing/corrupt file
- `save(data)` — atomic write via temp file + `os.replace()` (same pattern as `_write_live_stats_json`)
- `get_cookie_str()` — convenience accessor
- `get_auth_data()` — return full dict for `DouyinAuth.perepare_auth()`
- `mark_unhealthy()` / `mark_healthy()` — toggle health flag for monitoring
- Bootstrap: on first run, reads `DY_LIVE_COOKIES` from `.env` and writes to `cookies.json`

**Shared state format (`cookies.json`):**
```json
{
  "cookie_str": "ttwid=...; msToken=...; passport_csrf_token=...; s_v_web_id=...; ...",
  "cookie_dict": {"ttwid": "...", "msToken": "...", ...},
  "private_key": "...",
  "ticket": "...",
  "ts_sign": "...",
  "client_cert": "...",
  "ree_public_key": "...",
  "uid": "...",
  "updated_at": "2026-06-08T14:30:00+08:00",
  "health": "ok",
  "refresh_count": 42
}
```

**Key design choices:**
- All writes are atomic (`write tmp → os.replace`) — monitor never sees half-written file
- Monitor reads only, refresher reads-then-writes, no contention
- No database dependency — just a JSON file on disk

### 2. `cookie_refresher.py` (NEW — ~140 lines)

Playwright-based refresh logic. Reuses `DouyinAuth` from the existing `Douyin_Spider`.

**File path:** `StreamMonitor/cookie_refresher.py`

**Core method: `CookieRefresher.refresh()`**

Pseudocode:
```
1. Load existing cookies from CookieManager
2. Launch headless Chromium via Playwright
3. Create browser context, seed with existing cookies (KEY: this keeps the session alive)
4. Navigate to douyin.com
5. Wait for page load
6. Detect dead session: if URL contains "passport" → session expired, return False
7. Browse discover page for extra cookie churn
8. Extract all cookies from context
9. Extract localStorage["security-sdk/s_sdk_crypt_sdk"] for signature keys
10. Build DouyinAuth with fresh cookies + keys → derives private_key, ticket, etc.
11. Save to CookieManager
12. Return True
```

**Key design choices:**
- Seeding existing cookies into the browser context is the critical difference from `cookie_util.py` (which starts fresh) — this is what keeps the session alive rather than creating a new one
- Dead session detection: if Douyin redirects to `sso.douyin.com` or the URL contains `passport`, the session is gone
- Chromium args include `--disable-blink-features=AutomationControlled` (already used in `cookie_util.py`)
- `asyncio.run()` as the bridge between the async Playwright API and the synchronous caller

**Entry point (scheduling loop):**
```python
# cookie_refresher.py can be run as a standalone script
async def main():
    mgr = CookieManager()
    notifier = TelegramNotifier(...)
    refresher = CookieRefresher(mgr)
    interval = int(sys.argv[1])  # seconds, e.g. 21600 for 6h

    while True:
        success = await refresher.refresh()
        if success:
            notifier.send("✅ Cookies refreshed successfully", state="ok")
        else:
            notifier.send("🚨 Cookie refresh FAILED — session dead, manual re-login needed", state="dead")
        await asyncio.sleep(interval)
```

### 3. `telegram_notifier.py` (NEW — ~50 lines)

Minimal Telegram bot integration for out-of-band alerts.

**File path:** `StreamMonitor/telegram_notifier.py`

**Responsibilities:**
- `send(message)` — POST to Telegram Bot API
- `send_if_state_changed(message, state)` — deduplicates: only sends if state transitioned (ok→dead, dead→ok). Prevents spam on repeated failures.
- Config from `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

**Design choices:**
- Fire-and-forget — Telegram failures don't affect cookie refresh
- Rate limited by state transitions, not time intervals — won't spam
- 10s timeout on HTTP calls

### 4. StreamMonitor Modifications (`main.py` — ~70 lines added)

**Additions to `load_env_config()`:**
- Read `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` from `.env`
- Read `COOKIE_REFRESH_INTERVAL` (default: 21600 = 6 hours)

**New method: `StreamMonitor._check_cookie_health()`**

Called at the start of `run_once()`. Examines the HTTP response from `check_status()`:
- If `resp.url` contains `passport` or `resp.status_code == 302` → auth failure
- Sets `room_status` to `"auth_error"` sentinel
- Triggers immediate background cookie refresh attempt
- Uses last-known-good cookies for current cycle to avoid false offline detection

**New method: `StreamMonitor.reload_cookies()`**

Hot-reloads cookies into running objects without restart:
```python
def reload_cookies(self):
    data = self.cookie_manager.load()
    self.dy_cookie_str = data["cookie_str"]
    self.checker.cookie = DouyinLiveChecker._parse_cookie(data["cookie_str"])
    if self.stats_recorder and self.stats_recorder.is_running():
        self.stats_recorder.cookie_str = data["cookie_str"]
```

**New method: `StreamMonitor._trigger_cookie_refresh()`**

Background thread that runs the Playwright refresh inline (for emergency auth failures):
```python
def _trigger_cookie_refresh(self):
    refresher = CookieRefresher(self.cookie_manager, headless=True)
    success = asyncio.run(refresher.refresh())
    if success:
        self.reload_cookies()
        self.notifier.send(...)
    else:
        self.notifier.send(...)  # alerts via Telegram
```

**Modifications to `run_once()`:**
- After `check_status()`, check for `"auth_error"` sentinel
- If auth error and not already refreshing → trigger background refresh
- If `new_status == "auth_error"` and `current_status == "2"` (was live) → preserve live state, don't trigger offline

**Modifications to `__init__()`:**
- Accept `telegram_token` and `telegram_chat_id` params
- Initialize `CookieManager` and `TelegramNotifier`
- Bootstrap cookies.json from `.env` on first run

### 5. PM2 Configuration Update

Add the cookie refresher as a separate PM2 process in `ecosystem.config.js`:

```javascript
{
  name: 'cookie-refresher',
  script: '/root/StreamCheck/StreamMonitor/cookie_refresher.py',
  interpreter: '/root/StreamCheck/venv/bin/python3',
  cwd: '/root/StreamCheck/StreamMonitor',
  args: '21600',  // refresh every 6 hours
  restart_delay: 60000,
  max_restarts: 5,
  error_file: '/root/StreamCheck/logs/cookie-refresher-err.log',
  out_file: '/root/StreamCheck/logs/cookie-refresher-out.log',
  merge_logs: true,
  autorestart: true,
}
```

### 6. `.env` Changes

New optional variables:
```
# Telegram bot for cookie health alerts
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=987654321

# Cookie refresh interval in seconds (default: 21600 = 6 hours)
COOKIE_REFRESH_INTERVAL=21600
```

`DY_LIVE_COOKIES` remains as the seed value for first-time bootstrap. After `cookies.json` exists, `.env` is no longer the cookie source.

## Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| First run, no cookies.json | Bootstrap from `.env` DY_LIVE_COOKIES |
| cookies.json is corrupt | Log warning, fall back to `.env`, retry creation |
| Playwright refresh succeeds | Save, mark healthy, reload into monitor |
| Playwright refresh fails (dead session) | Mark unhealthy, Telegram alert "manual re-login needed" |
| Auth failure detected mid-cycle | Trigger emergency refresh in background, preserve live state |
| Emergency refresh also fails | Telegram "CRITICAL: monitor blind", continue with last known cookies |
| Telegram API unreachable | Log error, continue — Telegram is best-effort |
| Refresher process crashes | PM2 restarts it independently — monitor unaffected |
| Monitor reads during refresher write | Atomic `os.replace` guarantees complete read |
| Chromium not installed | Refresher logs error, PM2 restarts, Telegram alerts |
| Douyin shows CAPTCHA | Refresher fails, marks unhealthy, Telegram alerts |

## What This Does NOT Do

- **Does not** handle full re-login (SMS/QR) — when the session is fully dead, manual intervention is needed. SMS bridge (Approach B) is a future enhancement.
- **Does not** refresh Weibo or Bilibili cookies — those expire much less frequently and are out of scope.
- **Does not** modify the Douyin protobuf parsing or WebSocket logic — purely a cookie management layer.

## Files Changed

| File | Action | Est. Lines |
|---|---|---|
| `StreamMonitor/cookie_manager.py` | NEW | ~80 |
| `StreamMonitor/cookie_refresher.py` | NEW | ~140 |
| `StreamMonitor/telegram_notifier.py` | NEW | ~50 |
| `StreamMonitor/main.py` | Modify | +~70 |
| `StreamMonitor/.env.example` | Modify | +~8 |
| `ecosystem.config.js` | Modify | +~12 |

## Dependencies

- `playwright` — already in `Douyin_Spider/requirements.txt`
- Chromium browser — `playwright install chromium` (one-time setup on server)
- `requests` — already available
- No new Python packages needed
