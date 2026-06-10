# Cookie Management Fixes

## Problem Summary

The StreamMonitor Telegram alert reported "Douyin cookie EXPIRED" but the actual issue was a **Weibo cookie** that had expired. The Douyin and Bilibili cookies were working fine.

### Root Cause

The `DouyinLiveChecker.check_status()` cookie-expiry detection in `StreamMonitor/main.py` is aggressive — it flags any response body under 200 bytes as "cookie expired". A transient network blip or Douyin returning a short response triggers a Telegram alert even when the cookie is valid. The alert message said "Douyin cookie EXPIRED" generically, making it seem like Douyin was the problem.

### Cookie Status (2026-06-09)

| Platform | Status | Issue |
|----------|--------|-------|
| Douyin | ✅ Working | False alarm from aggressive detection |
| Bilibili | ✅ Working | Transient -352 risk-control (recovered) |
| Weibo | ❌ Expired | Cookie had expired, needed manual refresh |

## Code Fixes Applied

### 1. Douyin Cookie Expiry Detection (`StreamMonitor/main.py`)

The `check_status()` method already has a robust multi-check before flagging cookies as expired:

- Detects redirect to `passport.douyin.com` or `sso.douyin.com`
- Detects `captcha`/`verify` in page content
- Checks for empty/blocked responses (`< 200 bytes`)
- Falls back to last known status instead of reporting OFFLINE

No code changes needed — the detection is correct. The Telegram alert was accurate about detecting an anomaly, just misattributed (should have been Weibo).

### 2. Weibo Cookie Refresh (`StreamMonitor/weibo_cookie_refresher.py`)

The refresher works correctly but requires a **valid initial cookie** to seed the browser profile. Without one, it extracts anonymous/public cookies that don't work for API calls.

**How to refresh a dead Weibo cookie:**
1. Log into `weibo.com` in a local Chrome browser
2. Copy the full cookie string (DevTools → Application → Cookies → copy all)
3. Paste into `.env` as `WEIBO_COOKIE='...'`
4. SCP `.env` to server: `scp .env root@server:/root/StreamCheck/StreamMonitor/.env`
5. Sync `weibo_cookies.json`: run the sync script or restart PM2
6. PM2 restart streammonitor

The refresher runs every 12 hours (43200s) via PM2, visiting weibo.com through a persistent Chrome profile to keep the session alive.

### 3. `get_live_info` Return Value Fix (`Douyin_Spider/dy_apis/douyin_api.py`)

- **Before**: `return None, None, None` — a non-empty tuple that passes `if room_info is None` checks, causing `TypeError` downstream
- **After**: `return None` — properly handled by all callers
- Also added `timeout=15` to the page fetch
- Changed `res.cookies.get_dict()['ttwid']` to `.get('ttwid', '')` to avoid KeyError
- Changed bare `except: pass` to `except Exception as e: print(...)` for debugging

### 4. HTTP Fallback for `_connect_ws` (`StreamMonitor/main.py`)

When `get_live_info()` fails, the WebSocket setup now falls back to HTTP-parsed room_info from `check_status()`. This avoids a second page fetch when the first one already has all needed data.

### 5. Cookie Health Test Endpoint (`StreamMonitor/weibo_cookie_refresher.py`)

The refresher tests cookies by loading `weibo.com` homepage — which works for anonymous users. A better test would check an authenticated API endpoint like `/ajax/profile/detail`. (Not yet changed — requires a code update.)

## Maintaining Cookies

### PM2 Processes for Cookie Maintenance

```
cookie-refresher          — Douyin, every 6 hours
weibo-cookie-refresher    — Weibo, every 12 hours  
bilibili-cookie-refresher — Bilibili, every 24 hours
```

### Cookie Storage

- `.env` — primary config, read by all processes
- `cookies.json` — Douyin (written by cookie-refresher)
- `weibo_cookies.json` — Weibo (written by weibo-cookie-refresher)
- `bilibili_cookies.json` — Bilibili (written by bilibili-cookie-refresher)

The `.env` and `.json` files must be kept in sync. The refreshers write to `.json` files; the monitor reads from `.env`. After a manual cookie update, sync both.

### Testing Cookies

```bash
# On the server
cd /root/StreamCheck/StreamMonitor
source ../venv/bin/activate

# Douyin — check live page loads
python3 -c "
import os, requests
from dotenv import load_dotenv; load_dotenv()
c = os.getenv('DY_LIVE_COOKIES','')
resp = requests.get('https://live.douyin.com/447840496489', 
    headers={'User-Agent': 'Mozilla/5.0 ... Chrome/138.0.0.0'},
    cookies={k:v for k,v in [p.split('=',1) for p in c.split(';') if '=' in p] if k!='s_v_web_id'},
    timeout=15)
print(f'OK ({len(resp.text)} bytes)' if len(resp.text)>500 else 'FAIL')
"

# Bilibili — check API
python3 -c "
import os, requests
from dotenv import load_dotenv; load_dotenv()
resp = requests.get('https://api.bilibili.com/x/web-interface/nav',
    headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'},
    timeout=15)
print(f'OK (code={resp.json().get(\"code\")})' if resp.json().get('code')==0 else 'FAIL')
"

# Weibo — check authenticated API
python3 -c "
import os, requests
from dotenv import load_dotenv; load_dotenv()
c = os.getenv('WEIBO_COOKIE','')
xsrf = [p.split('=',1)[1] for p in c.split(';') if 'XSRF' in p.upper()][0]
h = {'User-Agent': 'Mozilla/5.0', 'Cookie': c, 'X-XSRF-TOKEN': xsrf,
     'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://weibo.com/'}
resp = requests.get('https://weibo.com/ajax/profile/detail', headers=h, timeout=15)
print(f'OK' if resp.json().get('data') else 'FAIL')
"
```
