# Cookie Management System — Complete Architecture

> **Last updated:** 2026-06-11 (v2 — unified module refactor, all bugs fixed)
> **Scope:** All cookie management across StreamMonitor, WeiboClient, Douyin_Spider
> **Platforms:** Douyin, Weibo, Bilibili

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Cookie Lifecycle](#2-cookie-lifecycle)
3. [Storage Layer](#3-storage-layer)
4. [Refresh Layer](#4-refresh-layer)
5. [Consumption Layer](#5-consumption-layer)
6. [Platform Details](#6-platform-details)
7. [Code Quality Issues](#7-code-quality-issues)
8. [Security Analysis](#8-security-analysis)
9. [Monitoring & Alerting](#9-monitoring--alerting)
10. [Troubleshooting Guide](#10-troubleshooting-guide)

---

## 1. System Overview

### 1.1 Purpose

Three platforms each need persistent login sessions for API access:

| Platform | Purpose | Session Duration | Refresh Interval |
|----------|---------|-----------------|------------------|
| **Douyin** | Live status detection, WebSocket stats, gift tracking | Days | 6 hours |
| **Weibo** | Post live/offline notifications to Weibo | Weeks | 12 hours |
| **Bilibili** | Check Bilibili live status | Weeks | 24 hours |

### 1.2 Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PM2 Process Manager                               │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                    StreamMonitor/cookies.py                           │  │
│  │                                                                       │  │
│  │  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────────┐  │  │
│  │  │ KeepaliveChecker  │  │   CookiePool     │  │    CookieManager   │  │  │
│  │  │ (Layer 1, HTTP)   │  │ (Layer 4, backup)│  │  (atomic I/O)      │  │  │
│  │  └────────┬─────────┘  └────────┬─────────┘  └─────────┬──────────┘  │  │
│  │           │                     │                       │             │  │
│  │  ┌────────┴─────────────────────┴───────────────────────┴──────────┐  │  │
│  │  │              UnifiedCookieRefresher (Layers 2-6)                 │  │  │
│  │  │  ┌────────────┐  ┌──────────────┐  ┌────────────────────────┐  │  │  │
│  │  │  │ Playwright  │  │ Clean-profile│  │  Real msToken fetch    │  │  │  │
│  │  │  │ refresh     │  │ retry (L3)   │  │  (L5, /sdk_token)      │  │  │  │
│  │  │  └────────────┘  └──────────────┘  └────────────────────────┘  │  │  │
│  │  └────────────────────────────────────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│               │ writes all 3               ▲ reads all 3                  │
│               ▼                            │                              │
│  ┌──────────────┐  ┌─────────────────┐  ┌──────────────────┐              │
│  │ cookies.json │  │ weibo_cookies   │  │ bilibili_cookies │              │
│  │ (+backup)    │  │ .json (+backup) │  │ .json (+backup)  │              │
│  └──────────────┘  └─────────────────┘  └──────────────────┘              │
│                                                                             │
│  Old files (→ thin shims → cookies.py):                                    │
│    cookie_manager.py  weibo_cookie_manager.py  bilibili_cookie_manager.py   │
│    cookie_refresher.py  weibo_cookie_refresher.py  bilibili_cookie_refresher.py
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────┐     │
│  │                     StreamMonitor (main.py)                       │     │
│  │  ┌───────────────┐  ┌───────────────┐  ┌──────────────────────┐  │     │
│  │  │DouyinLive     │  │ LiveStats     │  │ WeiboPoster           │  │     │
│  │  │Checker        │  │ Recorder      │  │ (imports via shims)   │  │     │
│  │  └───────────────┘  └───────────────┘  └──────────────────────┘  │     │
│  └───────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 File Inventory

| File | Role | Status |
|------|------|--------|
| `StreamMonitor/cookies.py` | **Unified module** — CookieManager, CookiePool, KeepaliveChecker, UnifiedCookieRefresher, entry points, compat aliases | **NEW (all logic)** |
| `StreamMonitor/cookie_manager.py` | Backward-compat shim → imports `DouyinCookieManager` from `cookies.py` | Shim (Douyin) |
| `StreamMonitor/weibo_cookie_manager.py` | Backward-compat shim → imports `WeiboCookieManager` from `cookies.py` | Shim (Weibo) |
| `StreamMonitor/bilibili_cookie_manager.py` | Backward-compat shim → imports `BilibiliCookieManager` from `cookies.py` | Shim (Bilibili) |
| `StreamMonitor/cookie_refresher.py` | Backward-compat shim → calls `_refresher_loop("douyin")` from `cookies.py` | Shim (Douyin) |
| `StreamMonitor/weibo_cookie_refresher.py` | Backward-compat shim → calls `_refresher_loop("weibo")` from `cookies.py` | Shim (Weibo) |
| `StreamMonitor/bilibili_cookie_refresher.py` | Backward-compat shim → calls `_refresher_loop("bilibili")` from `cookies.py` | Shim (Bilibili) |
| `StreamMonitor/main.py` | Monitor daemon (cookie consumer — imports via shims) | Unchanged |
| `StreamMonitor/cookies.json` | Douyin cookie data file (primary) | Unchanged |
| `StreamMonitor/cookies_backup.json` | Douyin cookie data file (backup, written by CookiePool) | NEW |
| `StreamMonitor/weibo_cookies.json` | Weibo cookie data file (primary) | Unchanged |
| `StreamMonitor/weibo_cookies_backup.json` | Weibo cookie data file (backup) | NEW |
| `StreamMonitor/bilibili_cookies.json` | Bilibili cookie data file (primary) | Unchanged |
| `StreamMonitor/bilibili_cookies_backup.json` | Bilibili cookie data file (backup) | NEW |
| `ecosystem.config.js` | PM2 config — now includes all 3 refresher processes | Updated |
| `WeiboClient/weibo/cookie.py` | Weibo cookie generator (legacy, unused) | Unchanged |
| `Douyin_Spider/utils/cookie_util.py` | Douyin Playwright cookie extraction (legacy, unused) | Unchanged |
| `inject_weibo_cookie.py` | Manual cookie injection tool (ops utility) | Unchanged |
| `check_weibo_cookie.py` | Weibo cookie diagnostic script | Unchanged |
| `check_env_cookie.py` | .env cookie validity checker | Unchanged |

---

## 2. Cookie Lifecycle

### 2.1 Bootstrap (first run)

```
.env variable ──► bootstrap_from_env() ──► cookies.json
```

1. Process starts → checks if `cookies.json` exists
2. If **not** exists → reads from `.env` (`DY_LIVE_COOKIES`, `WEIBO_COOKIE`, `BILI_COOKIE`)
3. Parses the semicolon-joined cookie string into a dict
4. Writes to `cookies.json` with initial health `"ok"`
5. If **already exists** → reads from file (`.env` is ignored)

**Design decision:** The JSON file is the single source of truth once created. This allows the refresher to update the file without touching `.env`.

### 2.2 Refresh (ongoing)

```
cookies.json ──► Playwright browser ──► websites ──► extract cookies ──► test ──► save
```

1. Load existing cookies from JSON file
2. Seed cookies into a persistent Chromium profile
3. Navigate to the target website (multiple pages)
4. Detect dead sessions (redirect to login = fail)
5. Extract cookies from browser context
6. **Optional:** Test the extracted cookies via HTTP
7. Save to JSON file with updated `refresh_count`

### 2.3 Consumption (real-time)

```
cookies.json ──► load() ──► cookie_str / cookie_dict ──► API calls
```

Consumers:
- `DouyinLiveChecker.check_status()` — uses `cookie_dict` for HTTP requests
- `LiveStatsRecorder` — uses `cookie_str` for WebSocket connections
- `WeiboPoster.post_tweet()` — uses `cookie_str` for posting requests
- `CookieRefresher` — uses `cookie_dict` to seed browser

### 2.4 Expiry Detection

```
API returns login redirect ──► mark_unhealthy() ──► cookies.json health="expired"
                                                      │
                                                      ▼
                                              Telegram alert
```

- `DouyinLiveChecker` detects expired cookies via redirect to `passport.douyin.com`
- Applies a **5-minute grace period** before forcing OFFLINE
- Calls `CookieManager().mark_unhealthy()` to persist the expired state
- `WeiboPoster.check_validity()` detects expired Weibo cookies
- Telegram alerts sent on expiry (deduplicated by state label)

---

## 3. Storage Layer

### 3.1 CookieManager Pattern

All three managers (`CookieManager`, `WeiboCookieManager`, `BilibiliCookieManager`) share a near-identical implementation:

#### Data Structure

```python
{
    "cookie_str": "",          # Semicolon-joined cookie string (all platforms)
    "cookie_dict": {},         # Parsed key-value dict (Douyin ONLY)
    "private_key": "",         # Douyin TLS fingerprint (Douyin only)
    "ticket": "",              # Douyin auth ticket
    "ts_sign": "",             # Timestamp signature
    "client_cert": "",         # Client certificate
    "ree_public_key": "",      # RE encryption key
    "uid": "",                 # User ID
    "health": "unknown",       # "ok" | "expired" | "degraded" | "unknown"
    "refresh_count": 0,        # Incremented on each successful refresh
    "updated_at": "2026-...",  # ISO 8601 timestamp
}
```

#### Atomic Write Pattern

```python
def save(self, data):
    tmp = self.file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, self.file)  # atomic on Linux (deployment target)
```

**Why this matters:** The monitor reads this JSON file every 30-60 seconds. Without atomic writes, a partial write from the refresher would cause a JSON parse error in the monitor. `os.replace()` is atomic when `tmp` and `self.file` are on the same filesystem (standard on Linux).

#### Graceful Load

```python
def load(self):
    defaults = self._defaults()
    try:
        with open(self.file, "r") as f:
            data = json.load(f)
        defaults.update(data)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[CookieManager] Failed to load: {e}")
    return defaults
```

`load()` **never raises** — returns default values on any failure. This is critical because the monitor runs 24/7 and a corrupt cookie file shouldn't crash the process.

### 3.2 Health States

| State | Meaning | Set By |
|-------|---------|--------|
| `"unknown"` | Initial state (never bootstrapped) | Default |
| `"ok"` | Cookie is valid and working | `mark_healthy()`, refresher |
| `"expired"` | Cookie detected as expired | `mark_unhealthy()`, DouyinLiveChecker |
| `"degraded"` | Core cookie present but CSRF missing | Bilibili refresher only |

**Health transition rules:**
- `mark_unhealthy()` only writes if current health is not already `"expired"` (no-write optimization)
- `mark_healthy()` only writes if current health is not already `"ok"`
- The refreshers always write `"health": "ok"` on successful save regardless of the test result (see **critical bug** below)

### 3.3 Platform-Specific Helpers

| Platform | Helper | Extracts | Used By |
|----------|--------|---------|---------|
| Weibo | `extract_xsrf()` | `XSRF-TOKEN` from cookie string | WeiboPoster |
| Bilibili | `extract_csrf()` | `bili_jct` from cookie string | Bilibili checker |

### 3.4 Bootstrap from .env

```python
@staticmethod
def bootstrap_from_env():
    if os.path.exists(COOKIES_FILE):
        return CookieManager().load()   # already seeded, skip
    cookie_str = os.getenv("DY_LIVE_COOKIES", "")
    if not cookie_str:
        return CookieManager().load()   # empty file, returns defaults
    # Parse and save...
```

**Env vars:**

| Platform | Env Var |
|----------|---------|
| Douyin | `DY_LIVE_COOKIES` (primary), `DY_COOKIES` (fallback) |
| Weibo | `WEIBO_COOKIE` |
| Bilibili | `BILI_COOKIE` |

---

## 4. Refresh Layer

### 4.1 Architecture (all three platforms)

All refreshers are **standalone PM2-managed processes** that share this flow:

```
1. Load existing cookies from JSON
2. Launch persistent Chromium profile
3. Seed existing cookies into the browser
4. Navigate to website pages
5. Dead session detection (check page URL)
6. Extract cookies from browser context
7. Cross-check critical cookies present
8. Optional: HTTP test
9. Save to JSON file
10. Send Telegram notification
```

### 4.2 Platform-Specific Differences

| Aspect | Douyin | Weibo | Bilibili |
|--------|--------|-------|----------|
| **Default interval** | 6 hours | 12 hours | 24 hours |
| **Profile directory** | `browser_profile/` | `weibo_browser_profile/` | `bilibili_browser_profile/` |
| **Critical cookies** | `s_v_web_id` | `SUB`, `SUBP` (core identity) | `SESSDATA` (session), `bili_jct` (CSRF) |
| **Auth integration** | Full `DouyinAuth` (private_key, ticket, etc.) | None | None |
| **Keepalive (Layer 1)** | ✅ All 3 platforms test via HTTP before Playwright | ✅ | ✅ |
| **Save-before-test** | ✅ **Fixed** — test first, only save if valid | ✅ | ✅ |
| **Clean-profile retry (L3)** | ✅ Auto-delete corrupt profile, retry once | ✅ | ✅ |
| **Backup cookie pool (L4)** | ✅ Primary + backup files | ✅ | ✅ |

### 4.3 Douyin Refresher Details

The Douyin refresher is the most complex because:
- It extracts `localStorage` signing keys (`security-sdk/s_sdk_crypt_sdk`)
- It integrates with `DouyinAuth` to rebuild derived auth fields
- It visits `live.douyin.com` specifically to get `s_v_web_id` (device fingerprint cookie)
- It has a fallback path that generates a fake `msToken` if DouyinAuth import fails

#### Page Visit Order (important!)

1. `https://www.douyin.com/` — main page, 8s wait for JS cookies
2. `https://www.douyin.com/discover` — triggers sliding session extension
3. `https://live.douyin.com/` — **only subdomain that sets `s_v_web_id`**

#### Dead Session Detection

```python
if "passport" in current_url or "sso.douyin.com" in current_url:
    return False, False  # session dead
```

#### s_v_web_id Alert

If `s_v_web_id` is missing from extracted cookies, a dedicated Telegram alert is sent:
```
"douyin s_v_web_id cookie MISSING — live.douyin.com may have changed its fingerprinting"
```

### 4.4 Weibo Refresher Details

**Fast path** (unique to Weibo):
```python
if old_cookie_str:
    is_valid = await self._test_cookie(old_cookie_str)
    if is_valid:
        return True, True  # skip browser entirely
```

This avoids the browser launch on every cycle, saving ~2-3s and reducing the risk of Weibo overwriting session cookies during navigation.

### 4.5 Bilibili Refresher Details

**CAPTCHA detection:**
```python
page_title = await page.title()
if "captcha" in page_title.lower() or "verify" in page_title.lower():
    return False, False
```

**Health granularity:**
- `"ok"` — both `SESSDATA` and `bili_jct` present
- `"degraded"` — only `SESSDATA` (session alive but CSRF missing)
- `"expired"` — neither present

---

## 5. Consumption Layer

### 5.1 How Each Consumer Uses Cookies

| Consumer | File | Cookie Source | Usage |
|----------|------|-------------|-------|
| `DouyinLiveChecker.check_status()` | `main.py` | `cookie_str` parsed to dict | `requests.get(..., cookies=dict)` |
| `LiveStatsRecorder._connect_ws()` | `main.py` | `cookie_str` | WebSocket `cookie=` header |
| `WeiboPoster.post_tweet()` | `main.py` | `cookie_str` | `"Cookie": cookie_str` in headers |
| `WeiboPoster.check_validity()` | `main.py` | `cookie_str` | HTTP GET with cookie header |
| `CookieRefresher.refresh()` | `cookie_refresher.py` | `cookie_dict` | `context.add_cookies(list)` |
| `WeiboCookieRefresher.refresh()` | `weibo_cookie_refresher.py` | `cookie_str` parsed | Same as above |
| `BilibiliCookieRefresher.refresh()` | `bilibili_cookie_refresher.py` | `cookie_str` parsed | Same as above |

### 5.2 Cookie String Parsing

The same parse pattern appears in **6+ locations** across the codebase:

```python
for part in cookie_str.split(";"):
    part = part.strip()
    if "=" in part:
        k, v = part.split("=", 1)
        result[k.strip()] = v.strip()
```

This is a candidate for extraction into a shared utility function.

### 5.3 Cookie Expiry Grace Period

In `DouyinLiveChecker` (main.py, line ~2337):

```python
_COOKIE_EXPIRY_GRACE_SECONDS = 300  # 5 minutes
```

When a cookie appears expired:
1. Mark the time of first detection
2. **Return cached status** (not OFFLINE) for 5 minutes
3. After 5 minutes, force OFFLINE so a real stream-end event is not lost

This prevents transient detection failures from falsely ending a live stream.

---

## 6. Platform Details

### 6.1 Douyin

**Cookie complexity:** Highest

Douyin uses multiple cookie-based auth mechanisms:
- Standard session cookies (set by `www.douyin.com`)
- `s_v_web_id` — device fingerprint (set by `live.douyin.com` only)
- `msToken` — anti-bot token (server-issued)
- `ttwid` — device identifier (from HTTP response)
- Derived auth fields: `private_key`, `ticket`, `ts_sign`, `client_cert`, `ree_public_key`

**Auth flow:**
1. Raw cookies → DouyinAuth object → `auth.perepare_auth(cookie_str, "", keys_str)`
2. DouyinAuth derives signing fields from the raw cookies + localStorage keys
3. These derived fields are used for API signature generation
4. Without them, API calls may be blocked or return CAPTCHA

**Key files:**
- `Douyin_Spider/builder/auth.py` — DouyinAuth class
- `Douyin_Spider/builder/header.py` — HeaderBuilder (headers + User-Agent)
- `Douyin_Spider/builder/params.py` — API request parameter builder

### 6.2 Weibo

**Cookie complexity:** Medium

Weibo cookies used for posting:
- `SUB` — primary session cookie (validated on every post)
- `SUBP` — secondary session cookie
- `XSRF-TOKEN` — CSRF protection token (required for POST requests)
- `WBPSESS` — login state
- `SCF`, `SINAGLOBAL`, `ULV` — tracking/fingerprinting

**Posting flow:**
1. `check_validity()` — HTTP GET to `m.weibo.cn/api/config`, checks `data.login === true`
2. `post_tweet()` — POST to `weibo.com/ajax/statuses/update` with cookie + XSRF headers
3. Error codes `100001`, `100005`, `21301`, `21315`, `21332` = auth/cookie expired

**Legacy code:** `WeiboClient/weibo/cookie.py` contains `CookieMaker` which attempts to generate Weibo visitor cookies via passport.weibo.com APIs. This is a **separate system** from the main StreamMonitor — it generates anonymous visitor tokens, not the authenticated session cookies the monitor needs.

### 6.3 Bilibili

**Cookie complexity:** Low

Bilibili cookies are straightforward:
- `SESSDATA` — core session cookie (required)
- `bili_jct` — CSRF token (required for mutations)
- `buvid3` — device identifier

**Test endpoint:**
```python
resp = requests.get(
    "https://api.bilibili.com/x/web-interface/nav",
    headers={"Cookie": cookie_str, "Referer": "https://www.bilibili.com/"},
)
# Response: {"code": 0, "data": {"uname": "..."}} = valid
#           {"code": -101} = not authenticated
```

Uses `api.bilibili.com` instead of `space.bilibili.com` because the latter returns HTTP 412 (anti-bot) from server IPs.

---

## 7. Code Quality Issues

### 🔴 Critical Bugs (all FIXED in v2)

#### Bug 1: Bilibili Refresher Saves Before Testing

**Status: ✅ FIXED in `StreamMonitor/cookies.py` (`_finalise_generic`)**

The old code saved the cookie *before* testing it — if the test failed, the good cookie was already overwritten. Now all platforms use **test-before-save**: the cookie is only written if the HTTP test passes.

#### Bug 2: Douyin Refresher Always Saves `health: "ok"`

**Status: ✅ FIXED in `StreamMonitor/cookies.py` (`_finalise_douyin`)**

The old code always set `health="ok"` regardless of the post-refresh test result. Now health reflects the test outcome: `"ok"` on pass, `"expired"` on fail.

### 🟡 Important Issues

#### Issue 1: Massive Code Duplication — ✅ RESOLVED

**Status: ✅ FIXED in `StreamMonitor/cookies.py`**

All 3 managers and 3 refreshers were consolidated into a single `StreamMonitor/cookies.py` (~1120 lines). Platform differences are driven by the `PLATFORM` config dict — no class hierarchy, no copy-paste.

The old 6 files remain as 1-line backward-compat shims so `main.py` and PM2 processes work without changes.

#### Issue 2: Duplicated Cookie String Parsing — ✅ RESOLVED

**Status: ✅ FIXED in `StreamMonitor/cookies.py`**

Three shared utility functions now exist in one place:
- `parse_cookie_string()` — split `"k=v; k2=v2"` into `dict`
- `build_cookie_str()` — join `dict` back into `"k=v; k2=v2"`
- `extract_cookie_value()` — extract a specific cookie by name

The old per-file parsing code is replaced by these shared calls.

#### Issue 3: Douyin Fallback Generates Fake msToken — ✅ FIXED

**Status: ✅ FIXED in `StreamMonitor/cookies.py` (`_fetch_ms_token`)**

The old code generated a random 107-character `msToken` locally — guaranteed to be rejected by Douyin's servers. Now fetches a real token from Douyin's `/sdk_token` endpoint via HTTP.

#### Issue 4: Inconsistent cookie_dict Storage — ⚠️ UNCHANGED

`cookie_dict` is still only stored for Douyin (for DouyinAuth integration). Weibo and Bilibili consumers parse the string on demand. Low priority since the shared `parse_cookie_string()` utility makes parsing trivially consistent.

#### Issue 5: `"degraded"` Health State — ✅ RESOLVED

**Status: ✅ FIXED in `StreamMonitor/cookies.py`**

Added `CookieManager.set_health()` and `mark_degraded()` methods. The `UnifiedCookieRefresher._determine_health()` method correctly sets `"degraded"` when Bilibili has `SESSDATA` but not `bili_jct`.

### 🔵 Minor Issues

#### Issue 6: Cross-Platform Mislabeling in Alerts

Telegram alerts say `"Douyin cookie EXPIRED"` even when the actual problem is a different platform's cookie. The alert comes from `DouyinLiveChecker` but the checking logic is generic enough that it might fire for network issues or anti-bot pages.

#### Issue 7: Grace Period Only for Douyin

`_COOKIE_EXPIRY_GRACE_SECONDS` only exists in `DouyinLiveChecker`. Weibo and Bilibili have no equivalent — an expired cookie immediately blocks operations.

#### Issue 8: No Startup Cookie Validation

The monitor loads cookies on startup but doesn't validate them until the first check cycle. If the server restarts with an expired cookie, it takes one full check cycle (30-60s) to detect and react.

#### Issue 9: Cookie Test May Hit Wrong Endpoint

The Weibo refresher's `_test_cookie()` was originally using `weibo.com/login` which **always** redirects automated requests to passport — a false positive. This was fixed to use `m.weibo.cn/api/config`, but the Douyin test (hitting `douyin.com/user/self`) still has occasional false positives when Douyin returns short responses for non-cookie reasons.

---

## 8. Security Analysis

### 8.1 Plaintext Storage

All cookie JSON files (`cookies.json`, `weibo_cookies.json`, `bilibili_cookies.json`) contain **full session tokens in plaintext**. If the server is compromised:
- All three platform sessions are immediately usable
- Attacker can post Weibo as the account, access Douyin live stats, modify Bilibili content

**Mitigations:**
- Restrict file permissions: `chmod 600 *.json` (read/write only for the process owner)
- Consider encrypting values at rest (e.g., system keyring, or encrypting `cookie_str` with a server secret)
- The cookies are equivalent to passwords — treat file access accordingly

### 8.2 .env File Exposure

The `.env` file also contains plaintext cookies. The bootstrap process copies from `.env` to JSON, then the JSON is the source of truth. However, `.env` is still present on disk.

### 8.3 Browser Profile Exposure

The persistent Chromium profiles (`browser_profile/`, `weibo_browser_profile/`, `bilibili_browser_profile/`) store:
- All cookies (in Chromium's encrypted storage)
- localStorage (signing keys for Douyin)
- Service Workers
- Browser fingerprint data

These profiles persist indefinitely and are not cleaned. If they accumulate stale data that triggers CAPTCHA, the only recovery is deleting and recreating them.

### 8.4 XSRF/CSRF Token Handling

- Weibo's `XSRF-TOKEN` is sent with every POST in custom headers (`X-XSRF-TOKEN`, `X-Requested-With`)
- Bilibili's `bili_jct` is used similarly
- Both are stored in the cookie file alongside the session token — this is standard for web APIs but means one compromise reveals everything

### 8.5 Telegram Notifications

Telegram alerts include:
- Cookie expiry status
- Platform identifiers
- Time of event

They do **not** include cookie values (by design). The `state` parameter is used for deduplication — this is safe.

---

## 9. Monitoring & Alerting

### 9.1 Telegram Notifications

| Event | Message | State Label | Sent By |
|-------|---------|-------------|---------|
| Douyin refresh success | `"douyin cookie refreshed (testing passed/failed) [HH:MM]"` | `None` | CookieRefresher |
| Douyin refresh failure | `"douyin cookie refresh FAILED — check server logs"` | `"dead"` | CookieRefresher |
| Douyin s_v_web_id missing | `"douyin s_v_web_id cookie MISSING ..."` | `"s_v_web_id_missing"` | CookieRefresher |
| Weibo refresh success | `"weibo cookie refreshed (testing passed/failed) [HH:MM]"` | `None` | WeiboCookieRefresher |
| Weibo refresh failure | `"weibo cookie refresh FAILED — check server logs"` | `"dead"` | WeiboCookieRefresher |
| Bilibili refresh success | `"bilibili cookie refreshed (testing passed/failed) [HH:MM]"` | `None` | BilibiliCookieRefresher |
| Bilibili refresh failure | `"bilibili cookie refresh FAILED — check server logs"` | `"dead"` | BilibiliCookieRefresher |
| Douyin cookie expired | `"Douyin cookie EXPIRED for room {id}"` | `"douyin_cookie_expired"` | DouyinLiveChecker |
| Weibo post failed (auth) | `"Weibo post FAILED after N retries — ..."` | `"weibo_cookie_expired"` | WeiboPoster |
| Weibo check invalid (live) | `"Weibo cookie invalid — skipping live post"` | `"weibo_cookie_invalid_live"` | handle_live() |
| Weibo check invalid (offline) | `"Weibo cookie invalid — skipping offline post"` | `"weibo_cookie_invalid_offline"` | handle_offline() |

### 9.2 Deduplication

`TelegramNotifier.send()` deduplicates by the `state` parameter. The last `<state, time>` pair is tracked in memory; if the same state fires within a cooldown window, the message is silently dropped.

Different state labels are used for the same cookie issue in different contexts (e.g., `weibo_cookie_invalid_live` vs `weibo_cookie_invalid_offline`) to ensure both events trigger notifications.

### 9.3 Health File

The `health` field in each JSON file is not currently read by any automated system for decision-making. It exists primarily as a diagnostic indicator:
- `check_weibo_cookie.py` reads and displays it
- The refreshers set it on save
- `mark_unhealthy()`/`mark_healthy()` update it

---

## 10. Troubleshooting Guide

### 10.1 Diagnosing Cookie Issues

```bash
# Check Weibo cookie health
cd /root/StreamCheck/StreamMonitor && python3 check_weibo_cookie.py

# Check .env source cookie
cd /root/StreamCheck/StreamMonitor && python3 check_env_cookie.py

# Direct API test: Douyin
python3 -c "
from cookie_manager import CookieManager
import requests
mgr = CookieManager()
data = mgr.load()
resp = requests.get('https://www.douyin.com/user/self',
    headers={'User-Agent': 'Mozilla/5.0 ...'},
    cookies={k:v for k,v in data.get('cookie_dict',{}).items() if k != 's_v_web_id'},
    timeout=15)
print(f'Status={resp.status_code}, URL={resp.url[:80]}, Body={len(resp.text)}')
"

# Direct API test: Bilibili
python3 -c "
import requests
resp = requests.get('https://api.bilibili.com/x/web-interface/nav',
    headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
data = resp.json()
print(f'Code={data.get(\"code\")}, User={data.get(\"data\",{}).get(\"uname\",\"?\")}'
      if data.get('code') == 0 else f'FAIL: code={data.get(\"code\")}')
"

# Check cookie JSON file contents
python3 -c "
import json
with open('cookies.json') as f: print(json.dumps(json.load(f), indent=2))
with open('weibo_cookies.json') as f: print(json.dumps(json.load(f), indent=2))
with open('bilibili_cookies.json') as f: print(json.dumps(json.load(f), indent=2))
"
```

### 10.2 Manual Cookie Injection

When cookie auto-refresh fails (CAPTCHA, profile corruption):

```bash
# Weibo
python3 inject_weibo_cookie.py

# Or manually update .env + delete JSON file to re-bootstrap:
rm weibo_cookies.json
# Edit .env with new WEIBO_COOKIE value
pm2 restart streammonitor weibo-cookie-refresher
```

### 10.3 Browser Profile Reset

If the persistent Chromium profile triggers CAPTCHAs:

```bash
# Stop refresher first
pm2 stop weibo-cookie-refresher

# Delete profile (will be recreated on next launch)
rm -rf weibo_browser_profile/

# Make sure cookies.json has valid cookies BEFORE restart
# (profile will be seeded from the JSON file)
pm2 restart weibo-cookie-refresher
```

### 10.4 PM2 Process Management

```bash
# View all processes
pm2 list

# View logs for a specific refresher
pm2 logs weibo-cookie-refresher

# Restart all cookie processes
pm2 restart cookie-refresher weibo-cookie-refresher bilibili-cookie-refresher

# Refresh intervals:
#   cookie-refresher:          21600s (6h)   Douyin
#   weibo-cookie-refresher:    43200s (12h)  Weibo
#   bilibili-cookie-refresher: 86400s (24h)  Bilibili
```

---

## Appendix: File Reference (v2 — post-refactor)

| File | Role | Key Contents |
|------|------|-------------|
| **`cookies.py`** | **Unified module** (~1120 lines) | `PLATFORM` configs, `parse_cookie_string()`, `build_cookie_str()`, `extract_cookie_value()`, `_UnifiedCookieManager`, `CookiePool`, `KeepaliveChecker`, `UnifiedCookieRefresher`, `_refresher_loop()`, `main_douyin/weibo/bilibili()`, `DouyinCookieManager`, `WeiboCookieManager`, `BilibiliCookieManager` (backward-compat aliases) |
| `cookie_manager.py` | Shim → `DouyinCookieManager` | 1-line re-export |
| `weibo_cookie_manager.py` | Shim → `WeiboCookieManager` | 1-line re-export |
| `bilibili_cookie_manager.py` | Shim → `BilibiliCookieManager` | 1-line re-export |
| `cookie_refresher.py` | Shim → `_refresher_loop("douyin")` | Parses sys.argv, calls cookies.py |
| `weibo_cookie_refresher.py` | Shim → `_refresher_loop("weibo")` | Same pattern |
| `bilibili_cookie_refresher.py` | Shim → `_refresher_loop("bilibili")` | Same pattern |
| `ecosystem.config.js` | PM2 config | All 3 refresher processes defined |
| `main.py` ~4000 | Consumers | `DouyinLiveChecker`, `LiveStatsRecorder`, `WeiboPoster` |
| `inject_weibo_cookie.py` | Ops utility | Manual cookie injection |
| `check_weibo_cookie.py` | Diagnostic | Weibo cookie health check |
| `check_env_cookie.py` | Diagnostic | .env cookie checker |
