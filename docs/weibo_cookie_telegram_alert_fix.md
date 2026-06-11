# Fix: Weibo posting blocked by broken cookie validity check

## Problem

The monitor silently skipped all Weibo posts — every entry in `notification_log.csv` shows `success=0`, and no Telegram alert was sent.

## Root cause

Two separate bugs:

### Bug 1: `check_validity()` always returns False

`WeiboPoster.check_validity()` hit `weibo.com/login` to verify the cookie. However, Weibo's `/login` endpoint **always** redirects automated (non-browser) requests to `passport.weibo.com` regardless of cookie validity — it's an anti-automation measure. So `check_validity()` returned `False` every single time, blocking all posts.

### Bug 2: No Telegram alert on check failure

Even if `check_validity()` had worked correctly, its `False` return path in `handle_live()` and `handle_offline()` only logged an error — it **never called any Telegram alert function**. The only Telegram alert for Weibo issues was inside `post_tweet()` (triggered on auth error codes from the API), but `post_tweet()` was never reached because `check_validity()` blocked it first.

Compare with **Douyin cookie expiry** in `check_status()` — that path correctly sent a Telegram alert via `self._send_health_alert()` at line 2141.

## Changes made in `StreamMonitor/main.py`

### 1. Removed `check_validity()` guard from `handle_live()`

**Before:**
```python
else:
    if not self.poster.check_validity():
        logger.error("Weibo cookie invalid — skipping live notification")
        success = False
    else:
        success = self.poster.post_tweet(content)
```

**After:**
```python
else:
    success = self.poster.post_tweet(content)
```

### 2. Removed `check_validity()` guard from `handle_offline()`

**Before:**
```python
else:
    if not self.poster.check_validity():
        logger.error("Weibo cookie invalid — skipping offline summary")
        success = False
    else:
        success = self.poster.post_tweet(content)
```

**After:**
```python
else:
    success = self.poster.post_tweet(content)
```

### 3. Deprecated `check_validity()` method

Now always returns `True` since the `/login` endpoint is unreliable for automated checks. Actual cookie validation happens inside `post_tweet()` which gets real error codes from the Weibo API and already has retry logic + Telegram alerts for auth failures.

### Verification is now in `post_tweet()`

The `post_tweet()` method already handles all failure cases:
- Retries up to 3 times with backoff
- Detects auth error codes (`100001`, `100005`, `21301`, `21315`, `21332`)
- Sends Telegram alert via `_send_weibo_health_alert()` when auth fails
- Rate-limit detection with longer backoff

So removing the `check_validity()` guard is safe — `post_tweet()` handles everything.

## Deploy

```bash
# Copy updated main.py to server
scp StreamMonitor/main.py root@167.99.73.192:/root/StreamCheck/StreamMonitor/main.py

# Restart monitor (only when stream is offline to avoid notification skip)
ssh root@167.99.73.192 "pm2 restart streammonitor"
```

## Related

- Also updated `weibo_cookies.json` and `.env` on server with fresh cookie
- XSRF token extraction from cookie string is working correctly in `_extract_xsrf()`
- Cookie refresher process (`weibo-cookie-refresher`) is stopped — restart if auto-refresh needed
