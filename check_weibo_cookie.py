#!/usr/bin/env python3
"""Check Weibo cookie validity and diagnose posting issues."""
import json
import os
import sys
import requests

os.chdir(os.path.join(os.path.dirname(__file__), "StreamMonitor"))

# ── Load cookie ──
print("=" * 60)
print("CHECKING WEIBO COOKIE")
print("=" * 60)

# Load from weibo_cookies.json
with open("weibo_cookies.json", "r", encoding="utf-8") as f:
    wdata = json.load(f)
cookie_str = wdata.get("cookie_str", "")
print(f"\n📄 weibo_cookies.json:")
print(f"   Length: {len(cookie_str)} chars")
print(f"   Health: {wdata.get('health')}")
print(f"   Updated: {wdata.get('updated_at')}")
print(f"   Refresh count: {wdata.get('refresh_count')}")

# Also load .env to compare
from dotenv import load_dotenv
load_dotenv(".env")
env_cookie = os.getenv("WEIBO_COOKIE", "")
print(f"\n📄 .env WEIBO_COOKIE:")
print(f"   Length: {len(env_cookie)} chars")
print(f"   Same as weibo_cookies.json? {cookie_str == env_cookie}")

# Check which one would be used by the monitor
print(f"\n🔍 Monitor will load from weibo_cookies.json if present (overrides .env)")
print(f"   Using: {'weibo_cookies.json cookie' if cookie_str else '.env cookie'}")

# Extract XSRF token
def extract_xsrf(cs):
    for part in cs.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip().upper() in ("XSRF-TOKEN", "XSRF_TOKEN"):
                return v.strip()
    return "(not found)"

xsrf = extract_xsrf(cookie_str)
print(f"\n🔑 XSRF-TOKEN: {xsrf[:20]}...")

# ── Test validity ──
print(f"\n{'─' * 60}")
print("TESTING COOKIE VALIDITY (hitting weibo.com/login)")
print(f"{'─' * 60}")

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Cookie": cookie_str,
}
try:
    resp = requests.get("https://weibo.com/login", headers=headers, allow_redirects=True, timeout=15)
    final_url = resp.url
    redirected = "passport" in final_url or "login" in final_url
    body_len = len(resp.text)
    print(f"   HTTP Status: {resp.status_code}")
    print(f"   Final URL: {final_url[:100]}")
    print(f"   Body length: {body_len}")
    if redirected:
        print(f"\n❌ COOKIE INVALID — Redirected to passport/login page")
    elif resp.status_code != 200:
        print(f"\n❌ COOKIE UNUSABLE — HTTP {resp.status_code}")
    else:
        print(f"\n✅ COOKIE VALID — No redirect to login")
except Exception as e:
    print(f"\n❌ Error: {e}")

# ── Check notification log ──
print(f"\n{'─' * 60}")
print("NOTIFICATION LOG")
print(f"{'─' * 60}")
notif_csv = "notification_log.csv"
if os.path.exists(notif_csv):
    with open(notif_csv, "r", encoding="utf-8") as f:
        content = f.read()
    if content.strip():
        print(content[:500])
    else:
        print("   (empty)")
else:
    print("   (no notification log file)")

print(f"\n{'─' * 60}")
print("DIAGNOSIS")
print(f"{'─' * 60}")

if not cookie_str:
    print("⚠️  No cookie in weibo_cookies.json — monitor would skip Weibo posting")
elif redirected:
    print("⚠️  Cookie in weibo_cookies.json is EXPIRED — monitor would skip posting with 'Weibo cookie invalid'")
    print("   ⚠️  Note: weibo_cookies.json cookie differs from .env cookie!")
    if cookie_str != env_cookie:
        print("   📌 The cookie refresher overwrote the original .env cookie with a different one.")
        print("   📌 The original .env cookie might still work. Delete weibo_cookies.json and restart.")
else:
    print("✅ Cookie appears valid — if no Weibo was sent, check posting errors")
