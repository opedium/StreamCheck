# StreamMonitor — Complete Feature Documentation

> **Project:** Douyin Live Stream Monitor  
> **File:** `StreamMonitor/` directory  
> **Language:** Python 3.11+  
> **Architecture:** Multi-process PM2-managed 24/7 monitoring system  

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Core Live-Status Detection (DouyinLiveChecker)](#2-core-live-status-detection-douyinlivechecker)
3. [Weibo Notification System (WeiboPoster)](#3-weibo-notification-system-weiboposter)
4. [Main Monitoring Loop (StreamMonitor)](#4-main-monitoring-loop-streammonitor)
5. [Live Statistics Recorder (LiveStatsRecorder)](#5-live-statistics-recorder-livestatsrecorder)
6. [Web Dashboard (web_server.py)](#6-web-dashboard-web_serverpy)
7. [Cookie Management System](#7-cookie-management-system)
8. [Cookie Refreshers (Playwright-based)](#8-cookie-refreshers-playwright-based)
9. [Telegram Notifications (telegram_notifier.py)](#9-telegram-notifications-telegram_notifierpy)
10. [Data Persistence (CSV/JSON)](#10-data-persistence-csvjson)
11. [Crash Recovery & Seed Override System](#11-crash-recovery--seed-override-system)
12. [Gift Deduplication & Memory Management](#12-gift-deduplication--memory-management)
13. [Protobuf Wire Format Parsing](#13-protobuf-wire-format-parsing)
14. [Deployment & Process Management](#14-deployment--process-management)
15. [Reliability Guide & Troubleshooting](#15-reliability-guide--troubleshooting)

---

## 1. System Overview

### 1.1 Purpose

StreamMonitor is a **24/7 live-stream monitoring daemon** that:

1. **Detects** when a Douyin stream goes LIVE and OFFLINE
2. **Posts** Weibo notifications on state transitions (with customizable templates)
3. **Records** detailed live statistics via WebSocket (gifts, likes, follows, viewers, fan club, light badges)
4. **Serves** a real-time web dashboard (HTML + Chart.js + REST API)
5. **Survives** crashes, network failures, and cookie expiry via multi-layered recovery

### 1.2 Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PM2 / systemd Manager                        │
│                                                                     │
│  ┌──────────────────────┐   ┌──────────────────────────────┐       │
│  │   StreamMonitor      │   │   CookieRefresher (Douyin)   │       │
│  │   (main.py)          │   │   (cookie_refresher.py)      │       │
│  │                      │   │                              │       │
│  │  ┌────────────────┐  │   │   BilibiliCookieRefresher   │       │
│  │  │DouyinLiveChecker│  │   │   (bilibili_cookie_        │       │
│  │  │  (HTTP polling) │  │   │    refresher.py)            │       │
│  │  └────────────────┘  │   │                              │       │
│  │                      │   │   WeiboCookieRefresher      │       │
│  │  ┌────────────────┐  │   │   (weibo_cookie_            │       │
│  │  │LiveStatsRecorder│  │   │    refresher.py)            │       │
│  │  │  (WebSocket)    │  │   └──────────────────────────────┘       │
│  │  └────────────────┘  │                                           │
│  │                      │   ┌──────────────────────────────┐       │
│  │  ┌────────────────┐  │   │   Web Server (Flask)        │       │
│  │  │WeiboPoster     │  │   │   (web_server.py)           │       │
│  │  │  (HTTP+API)    │  │   │   Port 5000                 │       │
│  │  └────────────────┘  │   └──────────────────────────────┘       │
│  └──────────────────────┘                                           │
└─────────────────────────────────────────────────────────────────────┘
```

**Key design principle:** All data flows through **shared JSON files** with atomic writes (temp + `os.replace`). No inter-process communication, no database.

### 1.3 Data Files

| File | Purpose | Written by | Read by |
|------|---------|------------|---------|
| `live_stats.json` | Real-time stream stats for dashboard | `LiveStatsRecorder._write_live_stats_json()` | `web_server.py` / API |
| `stats_timeseries.csv` | Time-series log (1 row/min) | `LiveStatsRecorder._write_stats_csv()` | Crash recovery, analytics |
| `notification_log.csv` | Weibo post history | `StreamMonitor.log_notification()` | Web dashboard status |
| `cookies.json` | Douyin cookies (shared state) | `CookieRefresher` / `CookieManager` | `StreamMonitor._reload_cookies()` |
| `weibo_cookies.json` | Weibo cookies (shared state) | `WeiboCookieRefresher` | `StreamMonitor._reload_cookies()` |
| `bilibili_cookies.json` | Bilibili cookies (shared state) | `BilibiliCookieRefresher` | (separate checker process) |
| `seed_override.json` | Manual stats corrections | Admin via SSH | `_write_live_stats_json()` — one-shot |

---

## 2. Core Live-Status Detection (`DouyinLiveChecker`)

### 2.1 What It Does

Polls `https://live.douyin.com/{live_id}` via HTTP GET every N seconds (default 10) to determine if the stream is LIVE (`"2"`) or OFFLINE (`"4"`).

### 2.2 Detection Logic (Step by Step)

**Phase 1 — HTML page fetch:**
```
GET https://live.douyin.com/{live_id}
Headers: Modern Chrome UA, full sec-ch-ua, standard request headers
Cookies: ALL except s_v_web_id (stripped — it triggers empty responses on page fetches)
```

**Phase 2 — Cookie expiry detection (critical guard):**
Before parsing the HTML, the code checks if the response indicates a dead cookie:
- Final URL contains `passport.douyin.com`, `sso.douyin.com`, or `login` → redirected to login
- Response body contains `captcha` or `verify` in first 2000 chars → CAPTCHA challenge
- Response body < 200 characters → empty/blocked response

If cookie is expired:
1. Marks the cookie unhealthy via `CookieManager().mark_unhealthy()`
2. Sends a Telegram health alert
3. **Does NOT return OFFLINE** — returns the last known status instead (prevents false offline detection from expired cookies)
4. If no last known status, falls back to OFFLINE

**Phase 3 — Primary parsing (inline JSON in HTML):**
Searches `<script[nonce]>` tags for inline JSON containing:
- `room_status` → `"2"` (live) or `"4"` (offline)
- `room_id`, `user_unique_id`, `anchor_id`, `sec_uid`
- `room_title`, `anchor_nickname`

**Phase 4 — Fallback: RENDER_DATA JSON blob:**
If step 3 fails, looks for `<script id="RENDER_DATA">` containing URL-decoded JSON. Navigates `app → initialState → roomStore → roomInfo → room` to extract the same fields.

**Phase 5 — Fallback: `window.__INITIAL_STATE__`:**
If RENDER_DATA fails, brace-counting parses `window.__INITIAL_STATE__` JSON (correctly handles nested objects, unlike naive regex).

**Phase 6 — Retry on timeout:**
If the HTTP request times out, retries up to 3 times with 2-second delay. On final timeout, returns last known status (prevents transient network issues from flipping to OFFLINE).

**Key insight:** The checker maintains `_last_status` across calls. This means a single timeout or parse failure won't trigger a false OFFLINE → LIVE transition — the caller (`StreamMonitor.run_once`) compares `new_status` against the cached `current_status`.

### 2.3 Status Codes

| Code | Meaning | Source |
|------|---------|--------|
| `"2"` | LIVE | Douyin's inline JSON `status` field |
| `"4"` | OFFLINE | Douyin's inline JSON `status` field |
| Fallback | OFFLINE | When all parsing patterns fail |

### 2.4 Room Info Output

Returned dict:
```python
{
    "room_id": str,            # Internal room ID
    "user_id": str,            # Anchor's user ID
    "user_unique_id": str,     # Same as user_id for Douyin
    "anchor_id": str,          # Anchor's internal ID
    "sec_uid": str,            # Anchor's secure user ID (used for API calls)
    "ttwid": str,              # ttwid cookie value (device ID)
    "room_status": str,        # "2" or "4"
    "room_title": str,         # Live room title
    "anchor_nickname": str,    # Anchor's display name
}
```

---

## 3. Weibo Notification System (`WeiboPoster`)

### 3.1 What It Does

Posts plain-text messages to Weibo (Sina Weibo) via its internal AJAX API at `https://weibo.com/ajax/statuses/update`.

### 3.2 Authentication

Uses **cookie-based auth** (no OAuth):
- Cookie string is extracted from browser dev tools when logged into `weibo.com`
- `XSRF-TOKEN` is extracted from the cookie string (both `XSRF-TOKEN` and `xsrf-token` variants)
- Sent as `Cookie` header + `X-XSRF-TOKEN` header

### 3.3 Retry Logic

- Base retries: 3 attempts
- Rate-limit codes (100005, 100006, 100100): `wait = 30s × attempt_number`
- Network/timeout errors: `wait = 5s × attempt_number`
- After all retries fail with an auth code (100001, 100005, 21301, 21315, 21332): sends a Telegram alert

### 3.4 Emoji Handling

Before posting, `strip_emoji()` removes all emoji characters from the content via a comprehensive Unicode regex. This is necessary because Weibo's API rejects posts containing emoji characters.

### 3.5 Auth Error Detection

The class defines `_AUTH_CODES` — a set of error codes that indicate cookie expiry:
```
{"100001", "100005", "21301", "21315", "21332"}
```

If the final error matches one of these, a Telegram health alert is sent.

---

## 4. Main Monitoring Loop (`StreamMonitor`)

### 4.1 What It Does

The central orchestrator. Runs a polling loop that:
1. Checks live status via `DouyinLiveChecker`
2. On LIVE transition: posts Weibo "stream started" + starts `LiveStatsRecorder`
3. On OFFLINE transition: posts Weibo "stream ended summary" + stops recorder
4. Periodically reloads cookies from JSON files
5. Handles mid-stream WebSocket failures with recovery

### 4.2 State Machine

```
START
  │
  ├── current_status = None (first check)
  │     ├── LIVE → handle_live() (unless restart detection)
  │     └── OFFLINE → log "waiting for live"
  │
  │── loop every N seconds:
  │
  ├── new_status == current_status (no change)
  │     ├── Print live preview + stats summary
  │     ├── Refresh follower_after via HTTP every 60s
  │     ├── Check WS followCount stall (>5min → force HTTP refresh + WS restart)
  │     └── Write live_stats.json for dashboard
  │
  ├── new_status != current_status (transition)
  │     ├── LIVE → handle_live()
  │     └── OFFLINE → handle_offline()
  │
  └── new_status == LIVE but recorder is dead
        └── attempt_ws_recovery()
```

### 4.3 Restart Detection (`_stats_json_indicates_restart`)

When the process starts and finds the stream already LIVE:
1. Reads `live_stats.json`
2. If `last_update` is ≤ 120 seconds old AND `live_id` matches → this is a restart
3. Sets `_live_posted = True` (skip duplicate "stream started" Weibo)
4. Resumes recording without notification

### 4.4 `handle_live()` — Stream Start

Step-by-step:
1. **Stores anchor nickname** from HTTP page data as fallback
2. **Starts LiveStatsRecorder** in a background thread (WebSocket connection)
3. **Passes HTTP-parsed `room_info`** as fallback for WebSocket setup (avoids redundant API call)
4. **Sleeps 2 seconds** to let the recorder fetch anchor info from API
5. **Posts Weibo** "stream started" notification (only once per stream, guarded by `_live_posted`)

Template variables for LIVE template:
`{name}`, `{title}`, `{live_id}`, `{room_id}`

### 4.5 `handle_offline()` — Stream End Summary

Step-by-step:
1. Guards against double-posting (`_summary_posted`)
2. Sets `stream_end_time` on the recorder
3. **Follower post-snapshot** — the most complex section:

**Follower priority chain at stream end:**
1. **WS followCount** (newer than HTTP) → absolute precise uint64 from SocialMessage
2. **HTTP followCount** (newer than WS) → precise integer from API
3. **HTTP last known** (no timestamp, CSV recovery) → previously saved value
4. **API retry loop** (3 attempts with 10s increasing backoff) — but REJECTS 万-rounded values
5. If ALL fail AND `follower_after` = 0 → SKIPS the Weibo post entirely

**Post-snapshot rejection of 万-rounded values:**
The code explicitly checks `isinstance(_raw_fc, str) and '万' in _raw_fc`. If the API returns a 万-rounded string like `"8.1万"`, the follower count has ±500 precision loss and is rejected. This prevents the Weibo summary from showing a wildly inaccurate follower delta.

4. **Builds offline summary** from recorder data using `_build_offline_summary_values()`
5. **Formats the template** with all computed values
6. **Appends WS recovery warning** if the WebSocket disconnected mid-stream and HTTP fallback was used
7. **Posts Weibo**
8. **Stops the recorder** (kills background threads)

### 4.6 Consecutive OFFLINE Detection (WS Stuck Prevention)

**Problem:** Douyin's WebSocket sometimes stays connected indefinitely after the stream ends, preventing the OFFLINE event from firing.

**Solution:** Three-state detection:
1. First HTTP OFFLINE detection while WS is connected → warn, reset `current_status` back to LIVE
2. Second consecutive HTTP OFFLINE → warn again
3. Third consecutive HTTP OFFLINE → FORCE `handle_offline()` despite WS being connected

### 4.7 Periodic Cookie Reload

Every 5 minutes (300s):
1. Reads `cookies.json` via `CookieManager`
2. If the cookie string changed → updates `checker.cookie`, `poster.cookie`, and `stats_recorder.cookie_str`
3. Reads `weibo_cookies.json` via `WeiboCookieManager`
4. Same process for Weibo cookie

This allows the separate CookieRefresher process to write refreshed cookies, and the live monitor picks them up mid-stream without restart.

### 4.8 WS FollowCount Stall Recovery

Detected when WS `followCount` hasn't updated in >5 minutes:
1. **Forces HTTP refresh** immediately to get authoritative follower baseline
2. **Restarts the WebSocket** (max 5 restarts, 10-min cooldown per restart)
3. **Resets WS followCount counters** to 0 so the new session starts clean
4. The WS session reset detection in `_on_message` handles the case where the new WS session has lower followCount values

### 4.9 SIGTERM Graceful Shutdown

On `SIGTERM` (PM2 restart signal):
1. Writes `live=False` to `live_stats.json` (freezes dashboard at last known state)
2. Writes final CSV row
3. Stops the recorder
4. Exits cleanly

**Why this matters:** Without this, PM2's SIGKILL (1.6s after SIGTERM) would kill the process mid-write, corrupting the JSON file.

### 4.10 Template Validation

On startup, `_validate_template()` parses all `{placeholder}` patterns from the template and warns about unknown variables. This catches typos like `{veiws}` instead of `{views}` before the Weibo post fails at stream end.

---

## 5. Live Statistics Recorder (`LiveStatsRecorder`)

### 5.1 What It Does

Connects to Douyin's WebSocket (`wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/`) and parses protobuf messages to record every metric about the live stream.

### 5.2 WebSocket Connection Setup

**Step 1 — Auth and room info:**
```
DouyinAuth().perepare_auth(cookie_str, "", "")
DouyinAPI.get_live_info(auth, live_id)
→ extracts: room_id, user_id, ttwid
```

**Step 2 — Webcast detail bootstrap:**
```
DouyinAPI.get_webcast_detail(auth, user_id, room_id, url)
→ returns protobuf LiveResponse with cursor + internalExt
```

**Step 3 — Build WSS URL with signature:**
```
params = {app_name, version_code, webcast_sdk_version, compress, cursor, 
          internalExt, host, aid, live_id, user_unique_id, im_path, identity,
          room_id, signature, ...}
wss_url = f"wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/?{urlencode(params)}"
```

**Step 4 — Connect:**
```python
WebSocketApp(url=wss_url, 
             header={Pragma, Accept-Language, User-Agent, ...},
             cookie=auth.cookie_str,
             on_message, on_error, on_close, on_open)
```

### 5.3 Pre-Snapshot

On initial connect (not reconnects):
1. Fetches anchor nickname via `DouyinAPI.get_user_info()`
2. Logs the API follower count (but does NOT set `follower_before` from API — uses WS SocialMessage for that)
3. Sets `_pre_snapshot_done` event (so `run_once()` can wait for it with 15s timeout)

**Why follower_before is NOT set from API:** The API follower_count may be 万-rounded (±500 precision loss). WS SocialMessage.followCount is a precise uint64 and is authoritative.

### 5.4 WebSocket Message Handling

The `_on_message` method handles these message types:

| Method | Protobuf Type | Fields Parsed | Notes |
|--------|---------------|---------------|-------|
| `WebcastLikeMessage` | `LikeMessage` | `total` (cumulative likes) | `max()` so likes never decrease |
| `WebcastSocialMessage` | `SocialMessage` | `followCount` (absolute) | Tracks `ws_follow_first/last` with session reset detection |
| `WebcastMemberMessage` | `MemberMessage` | `memberCount` (max) | `new_members` counter for events |
| `WebcastGiftMessage` | `GiftMessage` | `gift.name`, dedup key | See Gift Dedup section |
| `WebcastRoomStatsMessage` | `RoomStatsMessage` | `displayValue` (current viewers), `total` (cumulative views) | Per-minute viewer samples |
| `WebcastFansclubMessage` | (manual protobuf parse) | `type`, `content`, `total_members` | Fan club join detection |
| `WebcastRoomUserSeqMessage` | (manual protobuf parse) | `totalPvForAnchor` (cumulative views) | Secondary views source |

### 5.5 Like Tracking

```python
# From WebcastLikeMessage
self.total_likes = max(self.total_likes, msg.total)
```

Logic: `msg.total` is the cumulative total of likes for the stream. Using `max()` ensures we never decrease the counter if a later message has a lower total (which happens on WS reconnect — new connection streams from a different point in time).

### 5.6 Follower Tracking (Dual-Source)

**WS SocialMessage followCount (primary):**
- `msg.followCount` is a uint64 — the anchor's absolute follower count at that moment
- On first event: `ws_follow_first = fc`, `follower_before = fc`
- On subsequent events: `ws_follow_last = max(last, fc)` → monotonic
- `ws_follow_last_time` only updates when the VALUE changes, not when messages arrive (prevents "message spam with same value" from masking a stalled followCount)

**WS session reset detection:**
If a new WS session's followCount is LOWER than the old `ws_follow_first`:
```python
if self.ws_follow_first > 0 and fc < self.ws_follow_first:
    # WS session was reset — old values don't carry over
    self.ws_follow_first = 0
    self.ws_follow_last = 0
```
This handles the case where crash recovery restored a high `ws_follow_first` but the new WS session starts from a lower baseline.

**HTTP API followCount (authoritative baseline):**
- Fetched every 60 seconds via `fetch_cumulative_via_http()`
- `http_follow_first` = first precise integer value (overwrites stale CSV-recovered values)
- `http_follow_last` = highest precise integer seen
- Only records precise integer values (rejects 万-rounded strings)

**Computed follower delta (`_get_new_follows()`):**
```python
http_delta = max(0, http_follow_last - http_follow_first)
ws_delta = max(0, ws_follow_last - ws_follow_first)  # only if not stalled
return max(http_delta, ws_delta)
```

### 5.7 Viewer Tracking

**RoomStatsMessage processing:**
```python
self.current_viewers = msg.displayValue       # concurrent viewers
self._last_viewer_update = datetime.now()     # for staleness detection
# Peak tracking
if current_viewers > self.peak_viewers:
    self.peak_viewers = current_viewers
    self.peak_viewer_time = datetime.now().strftime('%H:%M:%S')
```

**Per-minute viewer sampling:**
```python
if (now - self._last_minute_sample).total_seconds() >= 60:
    self.viewer_samples.append(current_viewers)
    self._last_minute_sample = now
```
Capped at 1440 samples (24 hours). Average = `sum(samples) / len(samples)`.

**Viewer staleness fallback:**
```python
def _get_current_viewers(self):
    if self._last_viewer_update is None:
        return self._fresh_member_count  # MemberMessage.memberCount
    staleness = (datetime.now() - self._last_viewer_update).total_seconds()
    if staleness > 30:
        return self._fresh_member_count  # fallback to memberCount
    return max(self.current_viewers, 0)
```
If RoomStatsMessage hasn't arrived in >30 seconds (common after stream ends), falls back to the latest `memberCount` from MemberMessage (which Douyin uses as concurrent viewer equivalent).

### 5.8 Cumulative Views (Two Sources)

**Source 1 — RoomUserSeqMessage (primary):**
```python
pv = parse_room_user_seq_pv(payload)  # field 11 = totalPvForAnchor (string)
if pv > self.cumulative_views:
    self.cumulative_views = pv
```
This is a manual protobuf parser that extracts `totalPvForAnchor` from field 11 of RoomUserSeqMessage. The value is a Chinese number string like `"381.2万"` or `"3811912"`.

**Source 2 — RoomStatsMessage.total (secondary):**
```python
if msg.total > current_viewers:
    self.cumulative_views = max(self.cumulative_views, msg.total)
```
RoomStatsMessage.total is the cumulative view count, but only when it's higher than `displayValue` (concurrent). Using `max()` prevents it from ever decreasing.

### 5.9 Fan Club Tracking

**FansclubMessage parsing (manual protobuf):**
```python
# Fields:
# 1: commonInfo (sub-message, skip)
# 2: type (int32) — 1=upgrade, 2=join
# 3: content (string)
# 4: user (sub-message, skip)
```

`total_members` is extracted from the content string via regex patterns:
```python
r'第\s*(\d+)\s*名',       # 第289687名
r'第\s*(\d+)\s*位',       # 第528位
r'团成员\s*(\d+)',         # 团成员 12345
r'(\d+)\s*名\s*成员',      # 5667名成员 (brand name between 名 and 成员)
```

**Computed joins:**
```python
def _get_fan_club_joins(self):
    if self.fan_club_start_count > 0 and self.fan_club_end_count > 0:
        return max(0, fan_club_end_count - fan_club_start_count)
    return max(fan_club_joins, fan_club_gift_joins)
```
- **Primary:** Delta from content-based `total_members` (immune to duplicate WS messages)
- **Fallback:** Event counter or gift-card counter (when start_count wasn't captured due to reconnect gap)

**Gift-based fan club joins:**
Gifts containing `'入团卡'` or `'团卡'` increment `fan_club_gift_joins`. These serve as a reliable fallback when FansclubMessage protobuf parsing misses events during WS reconnection gaps.

### 5.10 Light Badge Tracking

**Badge gifts** (system-action gifts that represent fan badge light-up):
- `点点星光` (8 diamonds)
- `粉丝团灯牌` (1 diamond)
- `闪烁星河` (98 diamonds)
- `点亮粉丝团` (99 diamonds)

**Dedup rule: one badge per user per natural day (Beijing time):**
```python
today = _beijing_now().strftime('%Y%m%d')  # UTC+8 date
if getattr(self, '_light_badge_day', '') != today:
    # Midnight boundary — evict entries from previous days
    self._light_badge_users = {(u, d) for (u, d) in self._light_badge_users if d == today}
    self._light_badge_day = today
if (uid, today) not in self._light_badge_users:
    self._light_badge_users.add((uid, today))
    self.light_badges += 1
```

**Why this matters:** A user can contribute only ONE badge per calendar day (midnight-to-midnight Beijing time). After 12:00 AM UTC+8, the same user can contribute another. Using `uid` + `date_str` dedup prevents spam/bot badge farming.

### 5.11 Periodic Summary Print

Every 60 seconds, the recorder prints a formatted stream summary to stdout:
- Current viewers, cumulative views, likes
- Peak/average viewers
- New followers (delta)
- New fan club joins
- Light badges
- Top 3 hot gifts (excluding action gifts)

This is the primary real-time monitoring output for PM2 logs.

### 5.12 WebSocket Ping/Keepalive

Every 5 seconds, sends a protobuf `PushFrame` with `payloadType = "hb"` (heartbeat). Also triggers `_write_live_stats_json()` every 5 seconds to keep the dashboard fresh.

---

## 6. Web Dashboard (`web_server.py`)

### 6.1 What It Does

A Flask web server (port 5000) that:
1. Serves a real-time HTML dashboard (`templates/index.html`)
2. Exposes JSON REST APIs for live stats
3. Provides health check endpoints for monitoring

### 6.2 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | HTML dashboard page |
| `/api/health` | GET | Service health check (uptime, version, host) |
| `/api/live-stats` | GET | Latest stream stats from `live_stats.json` |
| `/api/status` | GET | Monitor process status + recent notifications |

### 6.3 Live Stats API Response

When a stream is being tracked:
```json
{
  "live": true,
  "live_id": "447840496489",
  "anchor_nickname": "主播名",
  "total_likes": 48830000,
  "new_follows": 1160,
  "follower_before": 7960000,
  "follower_after": 7961160,
  "fan_club_joins": 42,
  "fan_club_delta": 42,
  "fan_club_event_joins": 40,
  "fan_club_gift_joins": 2,
  "light_badges": 89,
  "current_viewers": 5236,
  "peak_viewers": 15200,
  "cumulative_views": 48830000,
  "member_count": 15200,
  "stream_start_time": "2026-06-10T21:00:00",
  "stream_duration_seconds": 3600,
  "ws_connected": true,
  "gift_summary": [["嘉年华", 5], ["跑车", 12], ...],
  "last_update": "2026-06-10T22:00:00"
}
```

### 6.4 Optional Session Auth

Set `STREAM_WEB_USERNAME` and `STREAM_WEB_PASSWORD` in `.env` to protect all routes with a login page + signed Flask session cookie. Without these, the dashboard is open-access.

### 6.5 Status API

The `/api/status` endpoint checks if the main monitor process is running:
1. On Linux: tries `pm2 info streammonitor` first, then `pgrep -fa "StreamMonitor|main.py"`
2. On Windows: skips process check (returns `None` for running status)
3. Also reads `notification_log.json` for the last 10 events

### 6.6 Frontend Dashboard (`templates/index.html`)

A single-page HTML dashboard with:

- **Hero stats row:** Current viewers, cumulative views, total likes, peak viewers (gradient cards with animated value transitions)
- **Secondary stats:** New followers, fan club joins, light badges, live duration (real-time clock)
- **Viewer trend chart:** Chart.js line chart with selectable time ranges (5m, 10m, 30m, 1h, 2h, all)
- **Gift leaderboard:** Top gifts with rank styling (gold/silver/bronze)
- **WebSocket status indicator:** Live dot + text
- **Live/offline overlay:** Dims stats when stream ends
- **Auto-refresh:** Polls `/api/live-stats` every 5 seconds
- **Animated number transitions:** `easeOutCubic` animation when values change
- **Pulse ring effect:** Card border pulses when the value updates

---

## 7. Cookie Management System

### 7.1 Architecture

Three parallel cookie management systems, each with the same structure:

| Platform | Manager Class | File | Refresher Class |
|----------|--------------|------|-----------------|
| Douyin | `CookieManager` | `cookies.json` | `CookieRefresher` |
| Weibo | `WeiboCookieManager` | `weibo_cookies.json` | `WeiboCookieRefresher` |
| Bilibili | `BilibiliCookieManager` | `bilibili_cookies.json` | `BilibiliCookieRefresher` |

### 7.2 CookieManager Pattern (Shared by all)

All three managers follow the same pattern:

**Atomic writes:**
```python
def save(self, data):
    tmp = self.file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, self.file)  # atomic on Linux
```
The reader never sees a half-written file because `os.replace` is atomic on Linux.

**Default data structure:**
```python
{
    "cookie_str": "",       # Semicolon-joined cookie string
    "cookie_dict": {},      # Parsed key-value pairs (Douyin only)
    "private_key": "",      # Douyin TLS fingerprint key
    "ticket": "",           # Douyin auth ticket
    "ts_sign": "",          # Timestamp signature
    "client_cert": "",      # Client certificate
    "ree_public_key": "",   # RE encryption public key
    "uid": "",              # User ID
    "health": "unknown",    # "ok" | "expired" | "degraded"
    "refresh_count": 0,     # How many times refreshed
    "updated_at": "",       # ISO timestamp
}
```

**Health marking:**
- `mark_unhealthy()` → sets `health = "expired"` ← triggers refresher
- `mark_healthy()` → sets `health = "ok"` ← after successful refresh

**Bootstrap from .env:**
On first run, if the JSON file doesn't exist, reads the cookie from `.env` and writes it. Safe to call on every startup — only writes if the file doesn't exist.

### 7.3 Shared File Pattern

The JSON file is the **single source of truth** shared between:
- **Writer:** CookieRefresher (separate PM2 process) writes refreshed cookies
- **Readers:** StreamMonitor checker + stats recorder read cookies periodically

This avoids needing IPC, a database, or mutex locks.

---

## 8. Cookie Refreshers (Playwright-based)

### 8.1 Purpose

Douyin/Weibo/Bilibili cookies expire after hours to days. The refreshers are **standalone PM2-managed processes** that use Playwright (headless Chrome) to:

1. Launch a **persistent browser profile** (preserves localStorage, Service Workers, browser fingerprint)
2. Seed existing cookies from the JSON file
3. Visit the target website
4. Let the page load and JS execute (triggers cookie churn/refresh)
5. Extract all cookies from the browser context
6. Test the refreshed cookie via HTTP
7. Save to the JSON file

### 8.2 Douyin CookieRefresher

**Refresh cycle:**
1. Launch persistent Chrome profile at `browser_profile/`
2. Seed existing cookies from `cookies.json`
3. Visit `https://www.douyin.com/` → wait 8s for JS to set cookies
4. Check for dead session: if redirected to `passport.douyin.com` or `sso.douyin.com`, return failure
5. Visit `https://www.douyin.com/discover` → cookie churn browse
6. Visit `https://live.douyin.com/` → **critical for `s_v_web_id`** (this is the ONLY subdomain that sets this cookie)
7. Extract all cookies via `context.cookies()`
8. Check `s_v_web_id` presence — missing means live.douyin.com changed its fingerprinting
9. Extract localStorage signing keys via `localStorage["security-sdk/s_sdk_crypt_sdk"]`
10. Try `DouyinAuth` for full derived fields (private_key, ticket, etc.)
11. Fallback: save raw cookies directly from Playwright
12. Test the cookie via HTTP GET to `https://www.douyin.com/user/self`

**Why `s_v_web_id` matters:**
This is the device fingerprint cookie sourced from `live.douyin.com` subdomain. It's required for every Douyin API call (as the `fp` parameter). Without it, many API endpoints return CAPTCHA or empty responses.

**Why we visit multiple pages:**
Visiting additional pages triggers Douyin's sliding session extension and may rotate short-lived tokens. The order matters — `live.douyin.com` sets `s_v_web_id`, which `www.douyin.com` alone does not.

**DouyinAuth integration:**
After extracting raw cookies, the refresher attempts to rebuild the full DouyinAuth object (private_key, ticket, ts_sign, client_cert, ree_public_key). These derived fields are used by the Douyin_Spider API calls for signature generation. If DouyinAuth import fails (e.g. protobuf version mismatch), raw cookies are saved — still valid for the monitor's immediate needs.

**Default interval:** 6 hours (21600s)

### 8.3 Weibo CookieRefresher

**Refresh cycle:**
1. Launch persistent Chrome profile at `weibo_browser_profile/`
2. Seed cookies from `weibo_cookies.json`
3. Visit `https://weibo.com/` → wait 5s
4. Check for dead session: redirected to `passport.weibo.com` or `login.sina.com.cn`
5. Visit `https://weibo.com/hot` → cookie churn browse
6. Re-check for dead session after browsing
7. Extract cookies
8. Cross-check: must have `SUB=` cookie (core identity cookie)
9. Health: `"ok"` if SUB present, `"degraded"` if missing
10. Test: HTTP GET to `https://weibo.com/login` — should NOT redirect to passport

**Default interval:** 12 hours (43200s)

### 8.4 Bilibili CookieRefresher

**Refresh cycle:**
1. Launch persistent Chrome profile at `bilibili_browser_profile/`
2. Seed cookies from `bilibili_cookies.json`
3. Visit `https://www.bilibili.com/` → wait 5s
4. Check for dead session: redirected to `passport.bilibili.com` or `login.bilibili.com`
5. Check for CAPTCHA via page title
6. Visit `https://space.bilibili.com/` → triggers Bilibili API auth checks
7. Re-check for dead session after browsing
8. Extract cookies
9. Cross-check: must have `SESSDATA=` (core session) and `bili_jct=` (CSRF token)
10. Health: `"ok"` if both present, `"degraded"` if SESSDATA only, `"expired"` if neither
11. Test: HTTP GET to `https://space.bilibili.com/`

**Default interval:** 24 hours (86400s)

### 8.5 First-Run Immediate Refresh

All three refreshers run their first refresh **immediately on startup** rather than waiting for the full interval. This ensures cookies are fresh right away.

### 8.6 Telegram Notification on Refresh

Each refresh cycle sends a Telegram notification:
- Success: `"douyin cookie refreshed (testing passed) [HH:MM]"`
- Failure: `"douyin cookie refresh FAILED — check server logs"`

---

## 9. Telegram Notifications (`telegram_notifier.py`)

### 9.1 Purpose

Fire-and-forget health alerts for critical events.

### 9.2 State-Transition Deduplication

```python
def send(self, message, state=None):
    if state is not None:
        if state == self._last_state:
            return False  # dedup: same state, skip
        self._last_state = state
```

If `state` is provided and matches the last sent state, the message is skipped. This prevents spam: e.g., "douyin cookie expired" won't repeat every check cycle.

### 9.3 Configuration

Via `.env`:
```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

The `configured` property returns `True` only if both are set. If unconfigured, messages are silently logged but not sent.

### 9.4 Events That Trigger Telegram Alerts

| Event | State | Source |
|-------|-------|--------|
| Douyin cookie expired | `douyin_cookie_expired` | `DowyinLiveChecker.check_status()` |
| Weibo cookie auth failure | `weibo_cookie_expired` | `WeiboPoster.post_tweet()` |
| WS recovery failed | `ws_recovery_failed` | `StreamMonitor.run_once()` |
| Follower fetch failed | `follower_fetch_failed` | `StreamMonitor.handle_offline()` |
| s_v_web_id missing | `s_v_web_id_missing` | `CookieRefresher.refresh()` |
| Cookie refresh result | `dead` or `None` | `CookieRefresher` / `WeiboCookieRefresher` / `BilibiliCookieRefresher` |

---

## 10. Data Persistence (CSV/JSON)

### 10.1 `live_stats.json` — Real-time Dashboard Data

**Written:**
- Every 5 seconds during stream (from heartbeat ping)
- Every 60 seconds (from periodic summary)
- On stream end (with `live: False`)
- On SIGTERM (graceful shutdown)

**Atomic write pattern:**
```python
tmp_path = STATS_FILE + '.tmp'
with open(tmp_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp_path, STATS_FILE)  # atomic
```

**Seed override applied here** (see Crash Recovery section 11).

### 10.2 `stats_timeseries.csv` — Time-Series Log

**Written:** Every 60 seconds, one row with all current metrics.

**Format (25 columns):**
```
timestamp, live_id, anchor_nickname, current_viewers, peak_viewers, 
cumulative_views, total_likes, follower_before, follower_after, 
follower_delta, ws_follow_first, ws_follow_last, 
http_follow_first, http_follow_last, 
fan_club_start_count, fan_club_end_count, fan_club_joins,
fan_club_gift_joins, light_badges, light_badge_day,
gift_event_count, gift_top1_name, gift_top1_count, ...
ws_connected, stream_duration_s, stream_start_time, member_count,
viewer_sample_count, viewer_sample_sum
```

**Header migration:** If the file exists with a different header format (e.g., after adding columns), the code detects the mismatch and rewrites with the new header while preserving all data rows.

**Used for:** Crash recovery, historical analysis, detecting stream boundaries.

### 10.3 `notification_log.csv` — Weibo Post History

**Format:**
```
timestamp, live_id, event_type, content_preview, success
```

Where `event_type` is one of:
- `live_start` — Stream start notification posted
- `live_end` — Stream end summary posted
- `live_end_skipped_no_sec_uid` — Skipped because sec_uid missing
- `live_end_skipped_no_snapshot` — Skipped because follower_after = 0
- `live_end_skipped_unreliable` — Skipped because WS recovery failed entirely

**Atomic append:** Uses temp file + os.replace for crash safety (not append-only, which can leave partial lines on crash).

---

## 11. Crash Recovery & Seed Override System

### 11.1 CSV Crash Recovery

When `LiveStatsRecorder` starts, it calls `_recover_from_csv(live_id)`:

1. Opens `stats_timeseries.csv`
2. Reads the last row
3. **Freshness guard:** Row must be ≤ 10 minutes old AND have the same `live_id`
4. Recovers these fields (all `max()` of current + recovered):
   - `peak_viewers`, `follower_before`, `follower_after`
   - `ws_follow_first`, `ws_follow_last`
   - `http_follow_first`, `http_follow_last`
   - `fan_club_start_count`, `fan_club_end_count`
   - `fan_club_joins`, `fan_club_gift_joins`, `light_badges`, `member_count`
5. Recovers top-5 gift names+counts
6. Reconstructs synthetic viewer samples: if the CSV says `viewer_sample_count=100` and `viewer_sample_sum=500000`, creates 100 samples each of `500000/100 = 5000` (preserves the overall average)

**Critical note:** `total_likes` and `cumulative_views` are intentionally NOT recovered. They are per-stream cumulative values from WebSocket messages (LIKE/ROOMSTATS/USERSEQ). Recovering yesterday's all-time totals would block today's lower per-stream values due to `max()` semantics — the new stream would show yesterday's million likes from the very first second.

### 11.2 CSV Clear on New Stream

When a new stream starts (last CSV row > 2 hours old), the CSV file is deleted. This prevents crash recovery from leaking data from a previous stream.

### 11.3 Seed Override System

**Purpose:** Allow manual correction of stats mid-stream when the automatic numbers are wrong (e.g., follower delta undercounted).

**Mechanism:** Admin creates `seed_override.json` via SSH, then restarts the process. The file is read once during the next `_write_live_stats_json()` call.

**Seed fields:**
```json
{
    "stream_start_time": "2026-06-10T21:00:00",
    "follower_before": 8078494,
    "follower_after": 8079122,
    "fan_club_start_count": 12345,
    "peak_viewers": 15000,
    "cumulative_views": 48000000,
    "total_likes": 45000000,
    "light_badges": 80,
    "fan_club_end_count": 12387,
    "fan_club_joins": 42,
    "avg_override": 5000,
    "seed_viewer_samples": [5000, 5100, 5200, ...]
}
```

**Application rules:**
1. One-shot: seed applies only on the first `_write_live_stats_json()` call after process start
2. Staleness guard: if `stream_start_time` in seed is > 6 hours old, the seed is discarded (prevents yesterday's seed from polluting today's new stream)
3. `max()` semantics: cumulative counters use `max(seed, live_value)` so live events never decrease the counter
4. Viewer samples: seed samples are **prepended** to the live samples so the average includes both the gap period and the live period
5. After application, the seed file is deleted atomically

### 11.4 Restart Detection

When the monitor starts and finds the stream already LIVE:
1. Reads `live_stats.json` 
2. If `last_update` is recent (≤ 120s) and `live_id` matches → this is a restart
3. Sets `_live_posted = True` → skips duplicate "stream started" Weibo
4. The recorder's CSV recovery fills in stats from before the restart

---

## 12. Gift Deduplication & Memory Management

### 12.1 The Duplicate Problem

Douyin sends **two** WebcastGiftMessage per gift event sharing the same `group_id`:
- First message: `repeat_end=0` (or absent)
- Second message: `repeat_end=1`

For combo gifts, `repeat_count` increments (1, 2, 3...) with dupes at each step.

### 12.2 Delta-Method Dedup (`_should_count_gift`)

```python
key = (group_id, gift_name, user_id)
prev = self._gift_dedup.get(key, 0)
if repeat_count > prev:
    self._gift_dedup[key] = repeat_count
    return True
return False
```

**How it handles each case:**

| Scenario | First msg | Second msg | Third (combo) | Fourth (combo dupe) |
|----------|-----------|------------|---------------|---------------------|
| Single-shot | rc=1, prev=0 → ACCEPT | rc=1, prev=1 → SKIP | — | — |
| Combo 3× | rc=1, prev=0 → ACCEPT | rc=1, prev=1 → SKIP | rc=2, prev=1 → ACCEPT | rc=2, prev=2 → SKIP |

### 12.3 Dedup Dictionary Cleanup

Every 10 minutes, stale entries are evicted via a two-phase approach:

1. **Phase 1 (cleanup start):** Take a snapshot of all entries with their current `repeat_count`
2. **Phase 2 (next cleanup):** Entries whose `repeat_count` hasn't changed since the snapshot are evicted; entries that changed are kept

This is safe because Douyin never reuses `group_id` within a stream. The two-phase approach avoids the previous problem of full-dict clears causing double-counting on message replay after WS reconnection.

### 12.4 Action Gift Exclusion

Gifts that represent system actions (not viewer-chosen gifts) are excluded from the popular-gifts ranking:

```python
_ACTION_GIFT_PATTERNS = (
    '粉丝团灯牌',   # badge light-up action
    '点点星光',     # badge light-up action  
    '闪烁星河',     # badge light-up action
    '入团卡',       # fan-club join card
    '点亮粉丝团',   # fan-club light/join action
)
```

These are already reflected in the stats cards (light_badges, fan_club_joins). Showing them as "gifts" double-counts and confuses users.

### 12.5 Gift Event Memory Cap

```python
MAX_GIFT_EVENTS = 20000
if len(self.gift_events) > MAX_GIFT_EVENTS:
    self.gift_events = self.gift_events[-MAX_GIFT_EVENTS:]
```

Prevents unbounded memory growth on long streams. The top-N gift summary is preserved via CSV persistence.

### 12.6 Protobuf Dump (Debug Tool)

Enabled via `PROTOBUF_DUMP=1` environment variable. Dumps all protobuf fields for the first 20 unique gift types to stdout for debugging protobuf field layout. Default off because each dump is 6-17KB of protobuf bytes per gift type.

---

## 13. Protobuf Wire Format Parsing

### 13.1 Why Manual Parsing?

The LiveStatsRecorder receives **raw protobuf bytes** from the WebSocket. Some messages have fields that the auto-generated Python protobuf classes (`Live_pb2.py`) don't define (unknown field numbers). The code manually parses these "undefined" fields:

1. **`RoomUserSeqMessage.totalPvForAnchor`** (field 11) — the cumulative view count string
2. **`FansclubMessage.type/content`** (fields 2/3) — fan club join events
3. **Gift dedup key** (fields 5/7/11: `repeat_count`, `user.id`, `group_id`)

### 13.2 Varint Decoding

```python
def _parse_varint(data, offset):
    """Decode a protobuf varint at the given offset, return (value, new_offset)."""
    value = 0
    shift = 0
    while True:
        byte = data[offset]
        value |= (byte & 0x7F) << shift
        shift += 7
        offset += 1
        if not (byte & 0x80):
            break
    return value, offset
```

Protobuf varints use the MSB (most significant bit) as a continuation flag. The lower 7 bits carry the value. This loop reconstructs the integer byte by byte.

### 13.3 Tag/Wire Type Handling

```python
tag, offset = _parse_varint(data, offset)
field_num = tag >> 3    # upper bits = field number
wire_type = tag & 0x7   # lower 3 bits = wire type

# Wire types:
# 0 = Varint (int32, int64, uint32, uint64, bool, enum)
# 1 = 64-bit (fixed64, sfixed64, double)
# 2 = Length-delimited (string, bytes, sub-message)
# 5 = 32-bit (fixed32, sfixed32, float)
```

### 13.4 RoomUserSeqMessage Parser

Extracts `totalPvForAnchor` (field 11, wire type 2 = string):
```python
tag = (11 << 3) | 2 = 90 = 0x5A
# Read string length + bytes
str_len, offset = _parse_varint(payload, offset)
str_bytes = payload[offset:offset + str_len]
str_val = str_bytes.decode('utf-8')
# Parse Chinese number: "381.2万" → 3812000
return parse_chinese_number(str_val)
```

### 13.5 Gift Dedup Key Parser

Extracts three fields from the GiftMessage payload:
- **Field 5** = `repeat_count` (uint64) — combo position
- **Field 7** = `user` (sub-message) → Field 1 = `id` (uint64)
- **Field 11** = `group_id` (uint64) — shared between dupe messages

```python
# Must parse ALL THREE fields — cannot break after finding any single field
# because protobuf serializes in field-number order (5 → 7 → 11)
```

### 13.7 DisplayLong Pattern Parsing

`RoomStatsMessage.displayLong` is a Chinese string like:
```
"点赞: 4883.0万  观看: 97.2万  在线: 5236  最高在线: 1.5万  粉丝团: 2.3万  灯牌: 567"
```

Parsed via regex patterns in `DISPLAY_PATTERNS`:
```python
'点赞': r'(?:点赞|like|赞)\s*[:：]?\s*([\d.]+)万?',
'观看': r'(?:观看|累计观看|view)\s*[:：]?\s*([\d.]+)万?',
'在线': r'(?:在线|在线人数|当前在线)\s*[:：]?\s*([\d.]+)万?',
# ...
```

These provide a secondary data source when the primary protobuf fields are unavailable.

---

## 14. Deployment & Process Management

### 14.1 systemd Service (`streammonitor.service`)

```
[Unit]
Description=StreamMonitor - Douyin Live Stream Notifier

[Service]
Type=simple
WorkingDirectory=/opt/StreamCheck/StreamMonitor
ExecStart=/usr/bin/python3 /opt/StreamCheck/StreamMonitor/main.py --record-stats
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 14.2 PM2 Ecosystem (`ecosystem.config.js`)

```
module.exports = {
  apps: [
    { name: 'streammonitor', 
      script: 'StreamMonitor/main.py',
      args: '--record-stats --verbose',
      interpreter: 'python3',
      ... 
    },
    { name: 'cookie-refresher',
      script: 'StreamMonitor/cookie_refresher.py',
      args: '21600',
      interpreter: 'python3',
      ...
    },
    // ... Weibo, Bilibili refreshers
  ]
}
```

### 14.3 Deploy Script (`deploy.sh`)

Automated deployment for Linux:
1. Installs Python dependencies
2. Copies project files to `/opt/StreamCheck/StreamMonitor/`
3. Copies Douyin_Spider dependency alongside
4. Creates log directory at `/var/log/streammonitor/`
5. Installs systemd service
6. Enables on boot
7. Starts the service

### 14.4 Critical Operations Rules

From the reliability guide:
1. **Always use PM2 for restarts:** `pm2 restart streammonitor`
   - Direct `kill` → PM2 immediately starts a new instance → TWO processes writing the same JSON → data corruption
2. **Don't deploy during a live stream** — gift data and cumulative counters reset on process restart
3. **Check for duplicate processes:** `ps aux | grep 'main.py.*record-stats'` — should be exactly 1

---

## 15. Reliability Guide & Troubleshooting

### 15.1 Known Issues & Fixes

| Issue | Root Cause | Fix |
|-------|------------|-----|
| Followers not increasing | WS followCount stalls | HTTP API baseline with `max(http_delta, ws_delta)` |
| Gift data lost on restart | `gift_events` is in-memory only | CSV top-5 persistence + crash recovery |
| Double process writes JSON | PM2 + manual `python3 main.py &` | Always use PM2 |
| No Weibo summary after stream | Cookie expired / sec_uid missing | Check logs for specific error code |
| WS frequent disconnects | OOM on 1GB VPS / Douyin rate limiting | `attempt_ws_recovery()` with 5 retries |
| Cookie expiry | 6-hour Douyin session limit | `CookieRefresher` auto-refresh every 6h |

### 15.2 Debugging Commands

```bash
# Check WS connection status
cat live_stats.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['ws_connected'])"

# View follower tracking
grep '\[FOLLOW\]' /tmp/streammonitor.log | tail -10

# Check for cookie expiry
grep -i 'cookie\|auth\|expir' /tmp/streammonitor.log

# Check CSV data consistency
tail -3 stats_timeseries.csv | awk -F',' '{print "time:",$1,"follow_delta:",$10,"viewers:",$4}'

# Force cookie re-mark for refresher
python3 -c "from cookie_manager import CookieManager; CookieManager().mark_unhealthy()"
```

### 15.3 Manual Stats Correction

Create `seed_override.json` to fix follower counts mid-stream:
```python
ssh root@server 'python3 -c "
import json
seed = {
    \"follower_before\": 8078494,
    \"follower_after\": 8079122,
    \"stream_start_time\": \"2026-06-10T21:00:00\",
}
with open(\"/opt/StreamCheck/StreamMonitor/seed_override.json\", \"w\") as f:
    json.dump(seed, f, ensure_ascii=False, indent=2)
" && pm2 restart streammonitor'
```

---
