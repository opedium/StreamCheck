# Session Summary — 2026-06-08

## Server
- **Host**: `167.99.73.192` (root)
- **PM2 processes**: streammonitor, cookie-refresher, bilibili-checker, bilibili-cookie-refresher, weibo-cookie-refresher, streamweb, weiboclient
- **Code paths**: `/root/StreamCheck/` (running) vs `C:\Users\xinyi\streamcheck` (local repo)
- **Note**: Server path is `StreamCheck` (capital S,C), local is `streamcheck` (lowercase). They have diverged.

## Problem 1: "Could not find room info in page HTML" (FIXED)

### Root Cause
Douyin's `live.douyin.com` returns **0-byte empty responses** (HTTP 200, `Content-Type: application/json`, `Content-Length: 0`) when the `s_v_web_id` device-fingerprint cookie is sent from a non-browser HTTP client (Python `requests` library). 

Douyin detects the TLS/HTTP fingerprint mismatch: the cookie refresher uses Playwright (real Chromium) to obtain `s_v_web_id`, but `check_status()` uses `requests.get()`. When both touch `live.douyin.com` with the same cookies from different client fingerprints, Douyin blocks with empty responses.

**Discovery**: Tested without cookies → page loads fine. With full cookies → 0 bytes. Binary-searched cookies → `s_v_web_id` alone triggers the block. Without `s_v_web_id` → full 983KB page with roomId.

### Fix Applied (2 files on server)

**`/root/StreamCheck/StreamMonitor/main.py` — `check_status()` (line ~1688)**:
```python
# Strip s_v_web_id from page request cookies
page_cookies = {
    k: v for k, v in self.cookie.items()
    if k != "s_v_web_id"
}
resp = requests.get(url, headers=self.HEADERS, cookies=page_cookies, verify=False, timeout=15)
```
`s_v_web_id` stays in `self.dy_cookie_str` for `DouyinAuth` API calls — only stripped from the HTML page fetch.

**`/root/StreamCheck/Douyin_Spider/dy_apis/douyin_api.py` — `get_live_info()` (line ~769)**:
Same fix — strip `s_v_web_id` from page request. This function is called by the WebSocket recorder's `_connect_ws()` to get room info (including ttwid). Without this fix, the WebSocket connection fails with `KeyError: 'ttwid'`.

### Result
- ✅ No more "Could not find room info in page HTML" warnings
- ✅ `check_status()` returns all 9 keys (room_id, user_id, anchor_id, sec_uid, ttwid, room_status, room_title, anchor_nickname)
- ✅ Stream correctly detected as LIVE/OFFLINE
- ✅ WebSocket connected, GIFT/MEMBER/LIKE events flowing
- ✅ Weibo preview generating correctly

---

## Problem 2: Weibo start notification not posted (BY DESIGN)

The stream started at **20:39** but the monitor was in a crash loop (from Problem 1) until the fix was applied at **21:05**. When the fix restored service, the monitor detected LIVE but saw `live_stats.json` had been written within 120 seconds → treated it as a "restart onto already-live stream" → skipped the start Weibo post to avoid duplicates.

**`_stats_json_indicates_restart()`** at `main.py:1850` — checks `live_stats.json` `last_update` within 120s. Intentional tradeoff: avoid spam vs missing a notification.

The **offline summary** (场观/点赞/最高在线/新增粉丝/新增粉丝团/点亮灯牌) WILL still post when the stream ends — the `_summary_posted` guard resets properly.

---

## Problem 3: Missing stats for first 25 minutes (FIXED via seed_override)

The monitor's `LiveStatsRecorder` started at 21:05, but the stream started at 20:39. The monitor missed:
- **Peak viewers**: 21,242 (monitor only saw 14,746)
- **Followers gained**: ~955 (monitor only counted 159)
- **Light badges**: 2,764 (monitor only counted 807)
- **Fanclub joins**: ~510 type-2 joins (monitor only counted 1-2)

### Fix: `seed_override.json` mechanism

The local repo (`main.py:933-984`) has a `seed_override.json` mechanism. It was **NOT present** in the deployed server code. Added it:

1. **Added ~30 lines** to `_write_live_stats_json()` in deployed `main.py` at line ~1068 — reads `seed_override.json` on first write and applies baseline stats using `max()` semantics (never decreases cumulative values).

2. **Created `/root/StreamCheck/StreamMonitor/seed_override.json`** with stats from DouyinBarrage logs (`C:\Users\xinyi\DouyinBarrage\data\447840496489\20260608_2040\*.csv`):
```json
{
  "stream_start_time": "2026-06-08T20:39:00",
  "peak_viewers": 21242,
  "follower_before": 8079569,
  "light_badges": 2764,
  "fan_club_start_count": 767524,
  "fan_club_end_count": 768100,
  "fan_club_joins": 510,
  "follower_after": 8080586
}
```

3. **Additional seed fields added** to seed processing code:
   - `fan_club_end_count` (with `max()`)
   - `fan_club_joins` (with `max()`)
   - `follower_after` (in seed JSON)

4. **Fixed WebSocket handler** at line 1354: changed `self.fan_club_end_count = m['total_members']` to `self.fan_club_end_count = max(self.fan_club_end_count, m['total_members'])` — prevents per-tier count from overwriting global total from seed.

### Source of corrected stats
DouyinBarrage CSV files at `C:\Users\xinyi\DouyinBarrage\data\447840496489\20260608_2040\`:
- `stats.csv`: peak viewers 21,242, cumulative views
- `like.csv`: cumulative likes
- `fansclub.csv`: 564 total events, ~510 type-2 joins, member_count range 767,524→768,050
- `gift.csv`: 2,764 light badges (粉丝团灯牌 2741 + 点点星光 6 + 闪烁星河 17)
- `member.csv`: 13,459 member join events

### Final corrected Weibo preview
```
迅猛龙 特蕾莎 直播结束
场观：75万
点赞：426万
最高在线：2.12万
平均在线：7163
新增粉丝：1017
新增粉丝团：513
点亮灯牌：2779
直播时长：53分钟
```

---

## Problem 4: Frontend viewer count frozen (FIXED)

`WebcastRoomStatsMessage` (ROOMSTATS) stopped arriving regularly from Douyin's WebSocket. `current_viewers` in `live_stats.json` froze at the last received value (e.g., 6998 for ~5 minutes). `member_count` from `MemberMessage` kept updating every second.

### Fix
Changed `_write_live_stats_json` at lines 1139 and 1177:
```python
# Before
"current_viewers": self.current_viewers,
# After
"current_viewers": max(self.current_viewers, self.member_count),
```

Also changed status line display at line 2465 to use `max()` for viewers display.

### DouyinBarrage log location
- Local: `C:\Users\xinyi\DouyinBarrage\logs\barrage.log`
- Data: `C:\Users\xinyi\DouyinBarrage\data\447840496489\20260608_2040\*.csv`

---

## All files modified on server

| File | Change |
|------|--------|
| `/root/StreamCheck/StreamMonitor/main.py` | `check_status()` — strip `s_v_web_id` from page request |
| `/root/StreamCheck/Douyin_Spider/dy_apis/douyin_api.py` | `get_live_info()` — strip `s_v_web_id` from page request |
| `/root/StreamCheck/StreamMonitor/main.py` | Added `seed_override.json` mechanism in `_write_live_stats_json()` |
| `/root/StreamCheck/StreamMonitor/main.py` | `fan_club_end_count` uses `max()` (line ~1354) |
| `/root/StreamCheck/StreamMonitor/main.py` | `current_viewers` uses `max(current_viewers, member_count)` in JSON output (×2) |
| `/root/StreamCheck/StreamMonitor/main.py` | Status line viewers uses `max()` fallback (line ~2465) |
| `/root/StreamCheck/StreamMonitor/seed_override.json` | Created with corrected barrage stats |

## Local file modified

| File | Change |
|------|--------|
| `StreamMonitor/main.py` | `check_status()` — strip `s_v_web_id` from page request |

## Current state
- Monitor running, detecting LIVE correctly
- Stats tracking with seed-corrected baselines
- Frontend should show non-frozen viewer count
- Weibo offline summary will post correct stats when stream ends
- Cookie refresher working normally (76 cookies, health: ok)
- Stream: 迅猛龙 特蕾莎, live.douyin.com/447840496489, started 20:39

## Important: Server vs local divergence
Server at `/root/StreamCheck/` has modifications NOT in local repo:
- `seed_override.json` mechanism
- `fan_club_end_count` max() fix
- `current_viewers` max() fallback

These should be synced to local repo to avoid lost work on next deploy.
