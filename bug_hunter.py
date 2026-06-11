#!/usr/bin/env python3
"""
StreamMonitor Bug Hunter — watches for anomalies during live stream.
"""
import json, time, os, sys
from datetime import datetime
from urllib.request import urlopen

API_URL = "http://localhost:5000/api/live-stats"
ERR_LOG = "/root/StreamCheck/logs/streammonitor-err.log"
OUT_LOG = "/root/StreamCheck/logs/streammonitor-out.log"

last_viewers = None
stagnant_count = 0
check_count = 0
last_status_print = 0

ERROR_BLACKLIST = set()  # dedup seen errors

def check_api():
    global last_viewers, stagnant_count, check_count
    check_count += 1
    issues = []
    try:
        resp = urlopen(API_URL, timeout=5)
        data = json.loads(resp.read())
    except Exception as e:
        return [("API_DOWN", f"API unreachable: {e}")]

    now = datetime.now()
    ts = now.strftime("%H:%M:%S")
    live = data.get("live", False)
    if not live and check_count > 3:
        return [("STREAM_ENDED", f"Stream no longer live at {ts}")]
    if not live:
        return issues

    ws_ok = data.get("ws_connected", False)
    if not ws_ok and check_count > 1:
        issues.append(("WS_DOWN", f"WS disconnected at {ts}"))

    viewers = data.get("current_viewers", 0)
    if last_viewers is not None and viewers == last_viewers:
        stagnant_count += 1
        if stagnant_count >= 18:
            issues.append(("STAGNANT_VIEWERS",
                f"viewers stuck at {viewers:,} for {stagnant_count*5}+ seconds"))
    else:
        stagnant_count = 0
    last_viewers = viewers

    if viewers == 0 and live:
        issues.append(("ZERO_VIEWERS", f"viewers=0 but live=True at {ts}"))

    lu = data.get("last_update", "")
    if lu:
        try:
            lu_dt = datetime.fromisoformat(lu)
            age = (now - lu_dt).total_seconds()
            if age > 15:
                issues.append(("STALE_DATA", f"last_update age={age:.0f}s at {ts}"))
        except:
            pass

    for field in ["current_viewers", "peak_viewers", "cumulative_views", "total_likes", "new_follows"]:
        val = data.get(field, 0)
        if isinstance(val, (int, float)) and val < 0:
            issues.append(("NEGATIVE", f"{field}={val}"))

    return issues, data

def check_logs():
    issues = []
    for logpath, patterns in [(ERR_LOG, ["Recorder died", "ERROR", "recovery.*fail"]),
                               (OUT_LOG, ["WebSocket closed unexpectedly", "Recorder died", "Failed to fetch"])]:
        try:
            with open(logpath) as f:
                lines = f.readlines()[-30:]
            for line in lines:
                for pat in patterns:
                    if __import__('re').search(pat, line, __import__('re').I):
                        key = line.strip()[:100]
                        if key not in ERROR_BLACKLIST:
                            ERROR_BLACKLIST.add(key)
                            issues.append(("LOG", f"[{logpath.split('/')[-1]}] {line.strip()}"))
        except:
            pass
    return issues

print("=" * 70)
print("  StreamMonitor Bug Hunter v2")
print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
print("  Checking every 10s · Status every 60s · Stagnation alert at 3min")
print("=" * 70)

while True:
    try:
        api_result = check_api()
        if isinstance(api_result, tuple) and len(api_result) == 2:
            issues, data = api_result
        else:
            issues = api_result
            data = None

        if int(time.time()) % 60 < 5:
            issues += check_logs()

        for sev, msg in issues:
            flag = {"STREAM_ENDED": "🔴", "WS_DOWN": "🔴", "LOG": "📝",
                    "STAGNANT_VIEWERS": "⚠", "ZERO_VIEWERS": "🔴",
                    "STALE_DATA": "⚠", "NEGATIVE": "🔴", "API_DOWN": "🔴"}.get(sev, "⚠")
            print(f"  {flag} [{sev}] {msg}")

        now = time.time()
        if now - last_status_print > 60 and data:
            ts = datetime.now().strftime("%H:%M:%S")
            ws = "\033[92m●\033[0m" if data.get("ws_connected") else "\033[91m●\033[0m"
            v = f"{data.get('current_viewers', 0):>6,}"
            cv = f"{data.get('cumulative_views', 0):>7,}"
            lk = f"{data.get('total_likes', 0):>9,}"
            fw = f"{data.get('new_follows', 0):>4}"
            bd = f"{data.get('light_badges', 0):>5}"
            fc = f"{data.get('fan_club_joins', 0):>4}"
            print(f"[{ts}] {ws} 观众:{v} 累计:{cv} 点赞:{lk} 粉丝:{fw} 灯牌:{bd} 团:{fc}")
            last_status_print = now

        time.sleep(10)
    except KeyboardInterrupt:
        print("\nBug hunter stopped by user.")
        sys.exit(0)
    except Exception as e:
        print(f"  ⚠ [ERROR] Bug hunter error: {e}")
        time.sleep(10)
