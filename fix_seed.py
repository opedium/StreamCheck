#!/usr/bin/env python3
"""Create seed AFTER stopping the old process so the new one gets it."""
import json

# First stop the old process
import subprocess
subprocess.run(["pm2", "stop", "streammonitor"], capture_output=True)

# Now create the seed file (no old process to eat it)
seed = {
    "cumulative_views": 817000,
    "peak_viewers": 14370,
    "light_badges": 5311,
    "fan_club_start_count": 769882,
    "fan_club_end_count": 770300,
    "fan_club_joins": 346,
    "stream_start_time": "2026-06-10T20:39:00",
    "follower_before": 8078404,
    "http_follow_first": 8078184,
    "http_follow_last": 8078834,
}

with open("/root/StreamCheck/StreamMonitor/seed_override.json", "w") as f:
    json.dump(seed, f, ensure_ascii=False, indent=2)
print("Seed created (old process stopped)")

# Verify seed file exists
import os
p = "/root/StreamCheck/StreamMonitor/seed_override.json"
print(f"Seed file exists: {os.path.exists(p)}")
print(json.dumps(seed, ensure_ascii=False, indent=2))

# Start the process (it will read the seed on first write)
result = subprocess.run(["pm2", "start", "streammonitor"], capture_output=True, text=True)
print(result.stdout[-200:] if result.stdout else "")
print(result.stderr[-200:] if result.stderr else "")
