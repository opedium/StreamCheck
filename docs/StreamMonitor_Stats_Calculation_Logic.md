# StreamMonitor — Stats Calculation Logic

> Every metric in the LiveStatsRecorder, traced from raw data to final value.  
> This document explains **what** each stat is, **where** it comes from, **how** it's computed, and **why** it's designed that way.

---

## Table of Contents

1. [How to Read This Document](#1-how-to-read-this-document)
2. [Viewer Stats](#2-viewer-stats)
3. [Like Stats](#3-like-stats)
4. [Follower Stats (Most Complex)](#4-follower-stats-most-complex)
5. [Fan Club Stats](#5-fan-club-stats)
6. [Light Badge Stats](#6-light-badge-stats)
7. [Gift Stats](#7-gift-stats)
8. [Member Stats](#8-member-stats)
9. [Derived Stats](#9-derived-stats)
10. [Offline Summary Values](#10-offline-summary-values)
11. [Dashboard-Specific Stats](#11-dashboard-specific-stats)

---

## 1. How to Read This Document

Each stat section follows this structure:

```
## STAT NAME

**Source:** Where the raw data comes from (WebSocket message, HTTP API, computed)

**Formula:** The exact calculation

**Logic Flow Diagram:** How data moves from source to final value

**Fallback Chain:** What happens if primary source is unavailable

**Why this design:** The edge case or Douyin behavior that drove this design decision
```

---

## 2. Viewer Stats

### 2.1 `current_viewers`

**Source:** `WebcastRoomStatsMessage.displayValue` (protobuf uint64)

**Formula:**
```python
self.current_viewers = msg.displayValue          # from RoomStatsMessage
self._last_viewer_update = datetime.now()         # recency marker
```

**Getter with staleness fallback:**
```python
def _get_current_viewers(self):
    if self._last_viewer_update is None:
        return self._fresh_member_count          # fallback: MemberMessage.memberCount
    staleness = (datetime.now() - self._last_viewer_update).total_seconds()
    if staleness > 30:
        return self._fresh_member_count          # fallback: >30s stale
    return max(self.current_viewers, 0)
```

**Fallback chain:**
1. `RoomStatsMessage.displayValue` (primary, updates every ~5-10s)
2. `MemberMessage.memberCount` (fallback, updates less frequently but persists after stream ends)
3. `0` (last resort)

**Why this design:**
After a stream ends, RoomStatsMessage stops arriving but the WebSocket may stay connected for minutes. The 30-second staleness window means the dashboard shows "live" viewer counts during actual streaming, but falls back to the last known `memberCount` (which Douyin treats as equivalent to concurrent viewers) after the stream ends. This prevents the dashboard from showing `0` viewers while the summary is being generated.

**Why `_fresh_member_count` is different from `member_count`:**
- `member_count` = `max()` across the whole stream (never decreases) — used for "total room joins" in summary
- `_fresh_member_count` = latest value only (not max) — used for viewer fallback, resets on reconnect

---

### 2.2 `peak_viewers`

**Source:** `current_viewers` tracked over time

**Formula:**
```python
if current_viewers > self.peak_viewers:
    self.peak_viewers = current_viewers
    self.peak_viewer_time = datetime.now().strftime('%H:%M:%S')
```

**Logic:** Simple running maximum. Every time a RoomStatsMessage arrives, compare against the peak. If higher, update both the peak value and the time it occurred.

**Why no fallback:** Peak is always derived from live RoomStatsMessage data. If no messages arrive, peak stays at 0, which is correct (no viewers observed).

---

### 2.3 `viewer_samples` (Per-Minute Snapshots)

**Source:** `current_viewers` sampled at 1-minute intervals

**Formula:**
```python
now = datetime.now()
if (self._last_minute_sample is None or
    (now - self._last_minute_sample).total_seconds() >= 60):
    self.viewer_samples.append(current_viewers)
    self._last_minute_sample = now
```

**Cap:** 1,440 samples maximum (24 hours × 60 minutes)

**Why time-weighted sampling (not all messages):**
RoomStatsMessage arrives every ~5-10 seconds. Storing all of them would:
- Use excessive memory for a 10-hour stream (~3600-7200 points)
- Make the average meaningless (more frequent updates during peak periods would skew the mean)

One sample per minute gives each minute of the broadcast equal weight in the average. A stream that was 500 viewers for 30 minutes then 5000 for 30 minutes should average 2750, not be skewed by which period had more RoomStatsMessage updates.

**Crash recovery:** If the process restarts mid-stream, CSV recovery reconstructs synthetic samples:
```python
_vs_count = int(row.get('viewer_sample_count', 0))
_vs_sum = int(row.get('viewer_sample_sum', 0))
if _vs_count > 0 and _vs_sum > 0:
    _vs_avg = _vs_sum // _vs_count
    recovered['viewer_samples'] = [_vs_avg] * _vs_count
```
This preserves the overall average even though individual sample timestamps are lost.

---

### 2.4 `avg_viewers` (Computed)

**Source:** Computed from `viewer_samples`

**Formula:**
```python
avg_viewers = sum(self.viewer_samples) // len(self.viewer_samples) if self.viewer_samples else 0
```

**Computation context:**
- Printed in the periodic summary (every 60s)
- Used in the offline summary template as `{avg}`
- Displayed on the web dashboard

**Why integer division:** Viewer counts are whole numbers (people). Integer division avoids false precision decimals.

---

### 2.5 `cumulative_views` (Total Cumulative Views / "场观")

This is the **total number of unique viewers** who have entered the room during the stream — the most important metric in Chinese live streaming ("场观").

**Sources (Dual):**

**Source A — `WebcastRoomUserSeqMessage.totalPvForAnchor`** (field 11, string type):
```python
pv = parse_room_user_seq_pv(payload)
if pv > self.cumulative_views:
    self.cumulative_views = pv
```
This is a **manual protobuf parse** of field 11, which is a string value like `"381.2万"` or `"3811912"`. The manual parser (`parse_room_user_seq_pv`) walks the protobuf wire format looking for tag `(11 << 3) | 2 = 0x5A`, reads the length-delimited string, decodes it from UTF-8, then calls `parse_chinese_number()` to convert `"381.2万" → 3,812,000`.

**Source B — `WebcastRoomStatsMessage.total`:**
```python
if msg.total > current_viewers:
    self.cumulative_views = max(self.cumulative_views, msg.total)
```
RoomStatsMessage.total is the cumulative count, but only when it's strictly greater than `displayValue` (which is concurrent viewers). The `> current_viewers` guard prevents accidentally reading concurrent viewers as cumulative views.

**Formula:** `max(Source_A, Source_B)` — always use the higher value.

**Fallback chain:**
1. RoomUserSeqMessage `totalPvForAnchor` (primary, updates every ~30-60s)
2. RoomStatsMessage `total` (secondary, updates more frequently)
3. `displayLong` text parsing — regex extracts `"观看: 97.2万"` → `"97.2万"` → `fmt_wan()` → `"97.2万"` string (for display only, not used in cumulative_views counter)
4. CSV recovery: NOT recovered across crash (see below)

**Why NOT recovered from CSV:**
```python
# NOTE: total_likes and cumulative_views are NOT recovered.
# They are per-stream cumulative values reported by WebSocket
# messages (LIKE/ROOMSTATS/USERSEQ). Recovering yesterday's
# all-time totals would block today's lower per-stream values
# due to max() semantics in the message handlers.
```
If a stream had 5M views yesterday and the CSV says 5M, recovering that would `max()` over today's actual 2M views — the new stream would show 5M from the very first second. This is also why `total_likes` is not recovered.

**Why `max()` is used (not direct assignment):**
WebSocket reconnections can stream messages from a different point in time. A new WS connection might start with a `totalPvForAnchor` of 1M while the old connection had already reported 2M. Without `max()`, the counter would **decrease** on reconnect, which is worse than temporarily stagnant.

---

## 3. Like Stats

### 3.1 `total_likes`

**Source A — `WebcastLikeMessage.total` (primary):**
```python
msg = Live_pb2.LikeMessage()
msg.ParseFromString(payload)
self.total_likes = max(self.total_likes, msg.total)
```
`msg.total` is the cumulative total of all likes for this stream. Using `max()` prevents decrease on WS reconnect.

**Source B — `RoomStatsMessage.displayLong` text (secondary):**
```python
parsed = parse_display_long(msg.displayLong)
if '点赞' in parsed:
    like_val = int(val * 10000) if val < 1e6 else int(val)
    self.total_likes = max(self.total_likes, like_val)
```
The displayLong text contains `"点赞: 4883.0万"`. The regex extracts `4883.0`, detects the presence of `万` in the original string, and multiplies by 10000 → `48,830,000`. The `< 1e6` heuristic distinguishes 万-formatted values (`4883.0` = 48.83M) from raw integers.

**Formula:** `max(LikeMessage.total, displayLong_parsed_likes)`

**Fallback chain:**
1. `WebcastLikeMessage.total` — the authoritative cumulative counter
2. `RoomStatsMessage.displayLong` text parsing — fallback when WS LikeMessage doesn't arrive
3. `"?"` displayed in periodic summary if both fail

**Why dual source:**
WebcastLikeMessage arrives whenever anyone likes. But during lulls, it may not fire for minutes. RoomStatsMessage fires every ~5-10s regardless and carries the same cumulative total in its displayLong text. Using `max()` across both means likes never decrease regardless of which source updates when.

---

## 4. Follower Stats (Most Complex)

Follower tracking is the most nuanced part of the system because Douyin has multiple data sources with different precision characteristics.

### 4.1 Data Source Comparison

| Source | Precision | Scope | Reliability |
|--------|-----------|-------|-------------|
| WS `SocialMessage.followCount` | Exact uint64 | Anchor's total at event time | High while WS is connected; may stall (>5min) |
| HTTP API `follower_count` (integer) | Exact int64 | Current total | High, but may return 万-rounded string |
| HTTP API `follower_count` (万 string) | ±500 error | Current total | **Rejected** for delta calculation |

### 4.2 `ws_follow_first` / `ws_follow_last`

**Source:** `WebcastSocialMessage.followCount`

**Formula:**
```python
fc = msg.followCount  # uint64 — absolute follower count of anchor
if fc > 0:
    # WS session reset detection
    if self.ws_follow_first > 0 and fc < self.ws_follow_first:
        # New WS session has LOWER followCount than old saved value
        # → old session was a different session (crash recovery restored stale value)
        self.ws_follow_first = 0
        self.ws_follow_last = 0
    
    if self.ws_follow_first == 0:
        self.ws_follow_first = fc
        if self.follower_before == 0:
            self.follower_before = fc     # only set if not already set by seed/CSV
    
    if fc > self.ws_follow_last:
        self.ws_follow_last = fc
        self.ws_follow_last_time = datetime.now()  # VALUE-changed timestamp
```

**Logic flow:**
```
SocialMessage arrives
    ↓
fc = followCount
    ↓
fc > 0? ──No──→ Skip
    ↓ Yes
WS session reset check:
  ws_follow_first > 0 AND fc < ws_follow_first?
    ↓ Yes → Reset both ws_follow_first/last to 0 (new session)
    ↓ No  → Continue
    ↓
ws_follow_first == 0?
    ↓ Yes → Set ws_follow_first = fc
    ↓ No  → Continue
    ↓
fc > ws_follow_last?
    ↓ Yes → Update ws_follow_last AND ws_follow_last_time
    ↓ No  → Skip (value unchanged, don't update timestamp)
```

**Why `ws_follow_last_time` only updates on VALUE change (not message arrival):**
If the followCount stays at `8,079,120` for 10 minutes while SocialMessage events keep arriving (each new follow from a viewer), the timestamp would keep refreshing even though the absolute count hasn't increased. This would make the "stall detection" think WS is still healthy when it's actually stuck. By only updating the timestamp when the VALUE changes, we correctly detect a stalled followCount.

### 4.3 `follower_before`

**Purpose:** The follower count at the START of the stream — the baseline for computing the follower delta.

**Source priority chain:**
1. **WS SocialMessage** (first event, most precise) — set when `ws_follow_first` is captured
2. **CSV recovery** — recovered from last CSV row (crash restart within same stream)
3. **Seed override** — manually corrected value from `seed_override.json`
4. **HTTP API** — NOT used for baseline initialization (may be 万-rounded)

**Key rule:** `follower_before` is set exactly once per stream — on the first WS SocialMessage. Subsequent reconnections preserve it:
```python
# Only set follower_before from WS if it wasn't already set
if self.follower_before == 0:
    self.follower_before = fc
```

### 4.4 `follower_after`

**Purpose:** The follower count when we need to compute the delta. Updated multiple times during the stream.

**Sources (from most to least preferred at stream end):**
1. **WS `ws_follow_last`** — if WS timestamp is newer than HTTP timestamp
2. **HTTP `http_follow_last`** — if HTTP timestamp is newer than WS timestamp
3. **HTTP last known** — CSV recovery data without timestamp
4. **API retry** — direct API call with 万-rounded rejection

**Periodic refresh (every 60s via `fetch_cumulative_via_http`):**
```python
# During the stream (periodic summary), follower_after is always updated
# so the delta-based follow count grows in real time
self.follower_after = self.ws_follow_last or self.follower_before
```

### 4.5 `http_follow_first` / `http_follow_last`

**Source:** `DouyinAPI.get_user_info()` → `user.follower_count`

**Setup:**
```python
# First refresh this session: always overwrite http_follow_first
if not getattr(self, '_http_first_seen', False):
    self._http_first_seen = True
    self.http_follow_first = fc_int  # precise integer only

# Value decreases (rare — anchor lost followers): update baseline
if fc_int < self.http_follow_first:
    self.http_follow_first = fc_int

# Value increases (normal): update last
if fc_int > self.http_follow_last:
    self.http_follow_last = fc_int
```

**Why `http_follow_first` can decrease:** In theory, anchors don't lose followers during a stream. In practice, Douyin's follower count fluctuates due to caching, anti-bot measures, or actual unfollows. Allowing `http_follow_first` to decrease prevents `http_delta` from going negative (which would be confusing in the Weibo post).

### 4.6 `_get_new_follows()` — The Final Follower Delta

**Formula:**
```python
def _get_new_follows(self):
    http_delta = max(0, self.http_follow_last - self.http_follow_first)
    ws_delta = 0
    if not self._ws_follow_stalled():
        ws_delta = max(0, self.ws_follow_last - self.ws_follow_first)
    return max(http_delta, ws_delta)
```

**`_ws_follow_stalled()` check:**
```python
def _ws_follow_stalled(self):
    if self.ws_follow_last_time is None:
        return True  # never received a usable followCount
    staleness = (datetime.now() - self.ws_follow_last_time).total_seconds()
    return staleness > 300  # 5 min without any followCount update
```

**Logic:**
```
http_delta = http_follow_last - http_follow_first    (always >= 0)
ws_delta = ws_follow_last - ws_follow_first           (>= 0 ONLY if NOT stalled)

return max(http_delta, ws_delta)
```

**Example scenarios:**

| Scenario | WS first | WS last | HTTP first | HTTP last | WS stalled? | Result |
|----------|----------|---------|------------|-----------|-------------|--------|
| Healthy stream | 7,960,000 | 7,961,160 | 7,960,000 | 7,961,160 | No (recent) | `max(1160, 1160)` = **1160** |
| WS stalled early | 7,960,000 | 7,960,500 | 7,960,000 | 7,961,160 | Yes (>5min) | `max(1160, 0)` = **1160** (HTTP-only) |
| WS never fired | 0 | 0 | 7,960,000 | 7,961,160 | N/A (no data) | `max(1160, 0)` = **1160** (HTTP-only) |
| HTTP never refreshed | 7,960,000 | 7,961,160 | 0 | 0 | No | `max(0, 1160)` = **1160** (WS-only) |
| Both stuck | 7,960,000 | 7,960,000 | 7,960,000 | 7,960,000 | Yes | `max(0, 0)` = **0** |

**Why `max(http, ws)` and not `sum(http, ws)`:**
Both sources measure the same thing (anchor's absolute follower count), just at different times. `http_follow_last - http_follow_first` is the authoritative delta over the stream duration. WS supplements it with real-time updates. If WS says +500 and HTTP says +1160, the real answer is at least 1160 — the extra 500 WS events were already counted in the HTTP delta because HTTP polls the true current value. Adding them would **double-count**.

**Why WS is dropped when stalled:**
If WS followCount hasn't updated in 5+ minutes, the `ws_delta = ws_follow_last - ws_follow_first` is stale. It may show +500 while HTTP shows -200 (anchor actually lost followers, but WS events didn't reflect it). Dropping the stalled WS delta ensures we don't report a positive follow increase that didn't happen.

### 4.7 End-of-Stream Follower Capture

At stream end (`handle_offline`), the code chooses the newer data source:

```python
ws_fresh = r.ws_follow_last_time if r.ws_follow_last > 0 else None
http_fresh = r._last_http_refresh if r.http_follow_last > 0 else None
use_ws = False

if ws_fresh and http_fresh:
    use_ws = ws_fresh > http_fresh  # newer wins
elif ws_fresh:
    use_ws = True
# else: both None → retry via API

if use_ws:
    r.follower_after = r.ws_follow_last          # WS is newer — authoritative
elif http_fresh:
    r.follower_after = r.http_follow_last        # HTTP is newer — authoritative
elif r.http_follow_last > 0:
    r.follower_after = r.http_follow_last        # CSV recovery (no timestamp)
else:
    # API retry loop — 3 attempts with increasing backoff
    # REJECTS 万-rounded values
```

**Why timestamp comparison matters:**
The WS may have stalled 30 minutes ago showing `followCount = 8,079,120`, while HTTP refreshed 1 minute ago showing `8,081,200`. The HTTP value is correct; using the stale WS value would report only 120 new followers instead of 2200.

---

## 5. Fan Club Stats

### 5.1 `fan_club_start_count` / `fan_club_end_count`

**Source:** `WebcastFansclubMessage.content` parsed via regex

**Formula:**
```python
m = parse_fansclub_msg(payload)  # extracts type, content, total_members

# Track the member count range
if m['total_members'] > 0:
    if self.fan_club_start_count == 0:
        self.fan_club_start_count = m['total_members']   # first seen = baseline
    self.fan_club_end_count = max(self.fan_club_end_count, m['total_members'])
```

**Why content-parsed `total_members` is authoritative (not event counter):**
Each FansclubMessage carries the absolute total member count in its content string: `"恭喜 xxx 成为粉丝团第289687名成员"`. This is Douyin's own sequential membership number, monotonically increasing. Computing `end - start` gives the precise number of joins during this stream, immune to:
- Duplicate WS messages (same content → same `total_members`)
- Gap periods (WS reconnection misses some join events, but the next message has the current total)
- Midnight resets (membership count doesn't reset daily)

### 5.2 `fan_club_joins` (Event Counter)

**Source:** `WebcastFansclubMessage.type == 2`

```python
if m['type'] == 2:
    self.fan_club_joins += 1
```

A raw counter of every "join" type event. Used as fallback when content parsing fails.

### 5.3 `fan_club_gift_joins` (Gift-Based Counter)

**Source:** Gift events containing `'入团卡'` or `'团卡'`

```python
if '入团卡' in gift_name or '团卡' in gift_name:
    self.fan_club_gift_joins += combo
```

**Why a third counter:** FansclubMessage protobuf parsing can miss events during WebSocket reconnection gaps. The "join card" gift (`入团卡`) is a reliable orthogonal signal — if someone sends this gift, they joined the fan club. It's not subject to protobuf parsing failures.

### 5.4 `_get_fan_club_joins()` — The Computed Join Count

**Formula:**
```python
def _get_fan_club_joins(self):
    if self.fan_club_start_count > 0 and self.fan_club_end_count > 0:
        return max(0, self.fan_club_end_count - self.fan_club_start_count)
    return max(self.fan_club_joins, self.fan_club_gift_joins)
```

**Logic:**
```
Content-based delta available?
    ↓ Yes → return (end - start), never negative
    ↓ No  → return max(event_counter, gift_counter) as floor
```

**Why `max(event_counter, gift_counter)` and not `sum()`:**
Both counters measure the same thing. If a join event was captured by both the FansclubMessage and a gift, `sum()` would double-count. `max()` provides a safe floor.

### 5.5 `fan_club_delta` (Dashboard Field)

**Formula:**
```python
"fan_club_delta": max(0, self.fan_club_end_count - self.fan_club_start_count) 
                  if self.fan_club_start_count > 0 else 0
```

This is the dashboard-specific field, same formula as `_get_fan_club_joins()` but expressed inline for the JSON output.

---

## 6. Light Badge Stats

### 6.1 `light_badges`

**Source:** Gift events for badge-gifting actions

**Gifts that count as badge lights:**
```python
('点点星光', '粉丝团灯牌', '闪烁星河', '点亮粉丝团')
```

**Dedup formula (one per user per natural day):**
```python
today = _beijing_now().strftime('%Y%m%d')  # UTC+8, e.g. "20260610"

# Midnight boundary eviction
if getattr(self, '_light_badge_day', '') != today:
    self._light_badge_users = {(u, d) for (u, d) in self._light_badge_users if d == today}
    self._light_badge_day = today

# Per-user-per-day dedup
if (uid, today) not in self._light_badge_users:
    self._light_badge_users.add((uid, today))
    self.light_badges += 1
```

**Logic flow:**
```
Gift event for badge action arrives
    ↓
Extract uid from protobuf GiftMessage user sub-message
    ↓
Get today's date in Beijing time (UTC+8)
    ↓
Is this a new day vs last recorded badge day?
    ↓ Yes → Evict non-today entries from dedup set
    ↓ No  → Continue
    ↓
Has (uid, today) been seen before?
    ↓ Yes → Skip (already counted today)
    ↓ No  → Count it: light_badges += 1
```

**Why midnight boundary resets the dedup set:**
A user can light one badge per day. When the calendar day changes in Beijing time (UTC+8), the same user can light another. The dedup set is partially evicted (keeping only today's entries) to allow this while bounding memory.

**Why `_light_badge_day` is tracked separately:**
Without tracking which "today" we're on, every badge event after midnight would trigger a full set scan. `_light_badge_day` makes the midnight-transition check O(1).

**Why NOT using RoomStatsMessage displayLong "灯牌":**
The `displayLong` field contains the CHANNEL'S **all-day cumulative** light badge total, not the stream-specific count. Using `max()` from displayLong would carry over yesterday's badge count. The code explicitly documents this:
```python
# NOTE: displayLong "灯牌" is the CHANNEL'S ALL-DAY cumulative total.
# Do NOT max() it into the stream-specific light_badges counter.
```

---

## 7. Gift Stats

### 7.1 `gift_events` (Raw Event List)

**Source:** `WebcastGiftMessage` (after dedup)

**Dedup logic** (see full explanation in the main documentation):
```python
key = (group_id, gift_name, user_id)
prev = self._gift_dedup.get(key, 0)
if repeat_count > prev:
    self._gift_dedup[key] = repeat_count
    return True  # COUNT IT
return False     # SKIP (duplicate)
```

**Append logic:**
```python
self.gift_events.append({
    'user': msg.user.nickname,
    'gift': gift_name,
    'count': 1,  # always 1 per dedup'd event (comboCount is NOT used as multiplier)
})
```

**Why `comboCount` is NOT used as a multiplier:**
```python
# Each GiftMessage is ONE gift instance. comboCount is the
# cumulative position in a combo streak (1st, 2nd, 3rd…),
# NOT the number of gifts in this message. Using it as a
# multiplier would double-/triple-count combo gifts.
```
When someone sends a combo gift with 3 `repeat_count` increments, three GiftMessage arrive (rc=1, rc=2, rc=3). Each counts individually as `count=1`. Using `comboCount` (which is the rc value: 1, 2, 3) as a multiplier would produce `1 + 2 + 3 = 6` instead of `1 + 1 + 1 = 3`.

**Cap:** 20,000 events max (see main doc for details).

### 7.2 `gift_summary` (Top Gifts by Count)

**Source:** Computed from `gift_events`

**Formula:**
```python
# Build summary (excluding action gifts)
gift_summary = {}
for g in self.gift_events:
    name = g['gift']
    if self._is_action_gift(name):
        continue  # skip badge/join gifts
    gift_summary[name] = gift_summary.get(name, 0) + g['count']

# Take top 5 for dashboard JSON
top_gifts = sorted(gift_summary.items(), key=lambda x: -x[1])[:5]

# Return for CSV: [name1, count1, ..., name5, count5]
# Pads with empty strings if fewer than 5
```

**What's excluded:**
- `粉丝团灯牌` — badge light-up
- `点点星光` — badge light-up
- `闪烁星河` — badge light-up
- `入团卡` — fan-club join
- `点亮粉丝团` — fan-club light/join

These are system actions, not viewer-chosen gifts. They're already captured in `light_badges` and `fan_club_joins` stats. Showing them in the gift ranking double-counts the same user action.

### 7.3 Gift Diamond Value

**Priority chain:**
1. `GIFT_PRICE_OVERRIDES[name]` — manual overrides for skin variants
2. `GIFT_PRICES[name]` — `gift_prices.json` (loaded from Douyin API)
3. `parse_gift_diamond_count(payload)` — protobuf field (unreliable for skins)

**Why overrides come first:**
Protobuf's `diamond_count` field returns the **base gift price**. Skin variants of gifts (e.g. `至尊超跑` is a `跑车` skin) cost more than the base but protobuf returns the base price. Manual overrides in `GIFT_PRICE_OVERRIDES` correct this:
```python
GIFT_PRICE_OVERRIDES = {
    '至尊超跑': 12000,   # base 跑车 is 1200 × 10
    '烈焰跑车': 6000,    # 5× base
    '无界超跑': 36000,   # 30× base
    '青绿典藏版嘉年华': 35000,  # base 嘉年华 is 30000
    '钻石嘉年华': 36000,
    '520嘉年华': 33000,
}
```

### 7.4 `gift_event_count` (CSV field)

Simple integer count of `len(self.gift_events)` at CSV write time.

---

## 8. Member Stats

### 8.1 `member_count`

**Source:** `WebcastMemberMessage.memberCount`

```python
msg = Live_pb2.MemberMessage()
msg.ParseFromString(payload)
if msg.memberCount > self.member_count:
    self.member_count = msg.memberCount
```

**What it represents:** The cumulative number of people who have entered the room (room joins). Each MemberMessage carries Douyin's running total.

**Why `max()`:** Same reason as other cumulative counters — WS reconnect may produce a lower value.

### 8.2 `new_members`

```python
self.new_members += 1  # per MemberMessage event
```

A raw event counter. Every time a MemberMessage arrives, regardless of duplicate content, increment by 1. This is a lower bound on the true join count (each event = at least one join).

### 8.3 `_fresh_member_count`

```python
self._fresh_member_count = max(self._fresh_member_count, msg.memberCount)
```

Separate from `member_count` — used exclusively for the viewer staleness fallback. Updated with `max()` so it never decreases on reconnect.

---

## 9. Derived Stats

### 9.1 `stream_duration_seconds`

**Formula:**
```python
if self.stream_start_time:
    duration_seconds = int((datetime.now() - self.stream_start_time).total_seconds())
```

**Start time behavior:**
```python
# Preserved across WS reconnects
if self.stream_start_time is None:
    self.stream_start_time = datetime.now()
```

**Why preserved across reconnects:** If the WS disconnects and reconnects 30 seconds later, `stream_start_time` stays at the ORIGINAL start time. The duration reflects the full broadcast, not just the most recent connection.

**End time:**
```python
self.stream_end_time = datetime.now()  # set in handle_offline or _on_close
```

Only set on actual stream end (intentional stop or WS final close), never on mid-stream disconnect + reconnect.

### 9.2 `ws_connected`

**Formula:**
```python
# Set False in _on_close
self.ws_disconnected = True
self.ws_disconnect_time = datetime.now()

# Dashboard field:
"ws_connected": not self.ws_disconnected
```

**Intentional close vs unexpected disconnect:**
```python
def _on_close(self, ws, close_status_code, close_msg):
    if self._stop_event.is_set():
        return  # stop() was called — intentional
    self.ws_disconnected = True  # unexpected — mark for recovery
```

### 9.3 `stream_duration` Display String

Used in the offline summary template `{duration}`:
```python
delta = stream_end_time - stream_start_time
mins, secs = divmod(int(delta.total_seconds()), 60)
hours, mins = divmod(mins, 60)
if hours > 0:
    duration_str = f"{hours}小时{mins}分钟"
else:
    duration_str = f"{mins}分钟{secs}秒"
```

---

## 10. Offline Summary Values

When the stream ends and a Weibo summary is posted, `_build_offline_summary_values()` computes all template variables:

### 10.1 `views` (场观)

**Formula:**
```python
views_val = r.cumulative_views if r.cumulative_views > 0 else 0
if views_val == 0 and r.viewer_samples:
    views_val = r.peak_viewers  # fallback: use peak as floor

pv = (r._try_get_wan('观看')                     # 1. displayLong "观看"
      or (fmt_wan(views_val) if views_val > 0 else "")  # 2. cumulative_views
      or (fmt_wan(r.peak_viewers) if r.peak_viewers > 0 else ""))  # 3. peak_viewers
```

**Output:** Formatted string like `"97.2万"` or `""`

### 10.2 `likes`

**Formula:**
```python
likes = (fmt_wan(r.total_likes) if r.total_likes else  # 1. LikeMessage total
         r._try_get_wan('点赞') or "")                   # 2. displayLong "点赞"
```

### 10.3 `peak` (最高在线)

**Formula:**
```python
peak = (r._try_get_wan('最高在线')                          # 1. displayLong "最高在线"
        or (fmt_wan(r.peak_viewers) if r.peak_viewers > 0 else "")  # 2. peak_viewers counter
        or (fmt_wan(max(r.viewer_samples)) if r.viewer_samples else ""))  # 3. viewer_samples max
```

### 10.4 `avg` (平均在线)

**Formula:**
```python
if r.viewer_samples:
    avg = fmt_wan(sum(r.viewer_samples) // len(r.viewer_samples))
elif r.peak_viewers > 0:
    avg = fmt_wan(r.peak_viewers)  # fallback: peak = avg when no samples
else:
    avg = ""
```

### 10.5 `followers` (关注涨幅度)

**Formula:**
```python
fb = r.ws_follow_first if r.ws_follow_first > 0 else r.follower_before
fa = r.ws_follow_last if r.ws_follow_last > 0 else r.follower_after
delta = r._get_new_follows()

if fb > 0 and fa > 0:
    followers_str = f"{fmt_wan(fb)} → {fmt_wan(fa)}（+{fmt_wan(delta)}）"
elif delta > 0:
    followers_str = f"+{fmt_wan(delta)}"
else:
    followers_str = "0"
```

**Example output:** `"796.0万 → 796.1万（+1,160）"`

### 10.6 `members` (新增粉丝团)

**Formula:**
```python
joins = r._get_fan_club_joins()
if joins > 0:
    members_str = str(joins)
else:
    members_str = r._wan_to_raw_str(r._try_get_wan('粉丝团'))  # displayLong fallback
```

### 10.7 `badges` (点亮灯牌)

**Formula:**
```python
if r.light_badges > 0:
    badges_str = str(r.light_badges)
else:
    badges_str = r._wan_to_raw_str(r._try_get_wan('灯牌'))  # displayLong fallback
```

### 10.8 `gifts` (热门礼物)

**Formula:**
```python
gifts_str = ""
if r.gift_events:
    gift_summary = {}
    for g in r.gift_events:
        name = g['gift']
        if LiveStatsRecorder._is_action_gift(name):
            continue
        gift_summary[name] = gift_summary.get(name, 0) + g['count']
    top = sorted(gift_summary.items(), key=lambda x: -x[1])[:3]
    if top:
        gifts_str = " | ".join([f"{n}×{c}" for n, c in top])
```

**Example output:** `"嘉年华×5 | 跑车×12 | 热气球×3"`

### 10.9 `duration` (直播时长)

Already covered in section 9.3. Example outputs: `"1小时23分钟"` or `"45分钟30秒"`

---

## 11. Dashboard-Specific Stats

The web dashboard (`live_stats.json`) includes all the above plus:

| Dashboard Field | Source / Formula | Notes |
|----------------|-----------------|-------|
| `live` | `True` if stream is being tracked, `False` on stop | Dashboard uses this to toggle offline overlay |
| `live_id` | Room ID from constructor | Links back to `live.douyin.com/{live_id}` |
| `anchor_nickname` | Pre-snapshot API call + HTTP page fallback | Displayed in header |
| `ws_connected` | `not self.ws_disconnected` | Dashboard shows green/red dot |
| `gift_summary` | Top-5 computed from `gift_events` (excl. action gifts) | Dashboard gift leaderboard |
| `last_update` | `datetime.now().isoformat()` | Dashboard shows "last update" time |
| `stream_start_time` | ISO format for real-time duration clock | Frontend computes HH:MM:SS every second |
| `stream_duration_seconds` | Elapsed time in seconds | Frontend computes display string |

---

## Summary: Stat Dependency Graph

```
WebSocket Incoming Messages
│
├── WebcastLikeMessage ──────────────→ total_likes
│
├── WebcastSocialMessage ────────────→ ws_follow_first, ws_follow_last
│                                      ↓
│                                   follower_before, follower_after
│                                      ↓
│                                   _get_new_follows()
│
├── WebcastMemberMessage ────────────→ member_count, new_members
│                                      ↓
│                                   _fresh_member_count (viewer fallback)
│
├── WebcastGiftMessage ──────────────→ gift_events (after dedup)
│                                      ↓
│                                   gift_summary, light_badges, fan_club_gift_joins
│
├── WebcastRoomStatsMessage ─────────→ current_viewers, peak_viewers, cumulative_views
│                                      ↓
│                                   viewer_samples → avg_viewers
│
├── WebcastFansclubMessage ──────────→ fan_club_start/end_count, fan_club_joins
│                                      ↓
│                                   _get_fan_club_joins()
│
└── WebcastRoomUserSeqMessage ───────→ cumulative_views (totalPvForAnchor)

HTTP API (periodic)
│
└── get_user_info() ────────────────→ http_follow_first, http_follow_last
                                       ↓
                                    _get_new_follows() max() input
```
