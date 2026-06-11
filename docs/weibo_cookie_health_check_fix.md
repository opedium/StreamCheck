# Fix: Weibo cookie health checks & Telegram alerts

## Problem

The monitor silently skipped all Weibo posts — every entry in `notification_log.csv` showed `success=0`, and no Telegram alert was sent when the cookie expired.

## Root causes (3 bugs)

### Bug 1: `check_validity()` used wrong endpoint

`WeiboPoster.check_validity()` hit `weibo.com/login` to verify the cookie. However, Weibo's `/login` endpoint **always** redirects automated (non-browser) requests to `passport.weibo.com` regardless of cookie validity — it's an anti-automation measure. So `check_validity()` returned `False` every single time, blocking all posts with false positives.

**Affected files:** `StreamMonitor/main.py` — `WeiboPoster.check_validity()`
**Fix:** Changed to use `m.weibo.cn/api/config` (mobile API) which returns a clear `"login": true/false` field:

```python
resp = requests.get("https://m.weibo.cn/api/config", headers={
    "User-Agent": "Mozilla/5.0 (iPhone; ...)",
    "Cookie": cookie_str,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://m.weibo.cn/",
})
data = resp.json()
return data.get("data", {}).get("login") is True
```

### Bug 2: No Telegram alert on check failure

Even though `check_validity()` was called, its `False` return path in `handle_live()` and `handle_offline()` only logged an error — it never called any Telegram alert function. The only Telegram alert for Weibo issues was inside `post_tweet()` (triggered on auth error codes from the API), but `post_tweet()` was never reached because `check_validity()` blocked it first.

**Affected files:** `StreamMonitor/main.py` — `handle_live()` and `handle_offline()`
**Fix:** Added `self._send_health_alert()` calls in both methods with dedup-safe state labels (`weibo_cookie_invalid_live` / `weibo_cookie_invalid_offline`).

### Bug 3: `weibo_cookie_refresher._test_cookie()` used same broken endpoint

The cookie refresher's post-refresh test also used `weibo.com/login`, always reporting "testing failed" via Telegram even when Playwright successfully extracted fresh cookies.

**Affected files:** `StreamMonitor/weibo_cookie_refresher.py` — `_test_cookie()`
**Fix:** Same mobile API endpoint — checks `data.login` instead of `/login` redirect.

**Note:** This was cosmetic only — the saved cookie in `weibo_cookies.json` was already correct because the Playwright-based save happens before the test runs, and the test result doesn't affect the saved data.

## Complete file changes

### `StreamMonitor/main.py`

| Location | Change |
|----------|--------|
| `WeiboPoster.check_validity()` | Endpoint changed: `weibo.com/login` → `m.weibo.cn/api/config` |
| `handle_live()` | Added `check_validity()` guard + Telegram alert on failure |
| `handle_offline()` | Added `check_validity()` guard + Telegram alert on failure |

### `StreamMonitor/weibo_cookie_refresher.py`

| Location | Change |
|----------|--------|
| `_test_cookie()` | Endpoint changed: `weibo.com/login` → `m.weibo.cn/api/config` |

## Why keep the guard?

`post_tweet()` already handles auth errors with retries + Telegram alerts, but checking validity first prevents:
- Wasting API calls on expired cookies (3 retries × 15s timeout = 45s per post)
- Confusing log spam from repeated retry failures
- Delayed offline summary (the retries hold up the stream-end cleanup)

## Telegram alert dedup

`TelegramNotifier.send()` deduplicates by state label. Using different states (`weibo_cookie_invalid_live` vs `weibo_cookie_invalid_offline`) ensures both events trigger a notification rather than the second being silently dropped as a duplicate.

## How to deploy

```bash
# Copy both updated files to server
scp StreamMonitor/main.py root@167.99.73.192:/root/StreamCheck/StreamMonitor/main.py
scp StreamMonitor/weibo_cookie_refresher.py root@167.99.73.192:/root/StreamCheck/StreamMonitor/weibo_cookie_refresher.py

# Restart monitor and refresher
ssh root@167.99.73.192 "pm2 restart streammonitor"
ssh root@167.99.73.192 "pm2 restart weibo-cookie-refresher"
```
