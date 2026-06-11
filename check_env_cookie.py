#!/usr/bin/env python3
"""Check if the .env Weibo cookie is still valid."""
import os
import requests
from dotenv import load_dotenv

os.chdir(os.path.join(os.path.dirname(__file__), "StreamMonitor"))
load_dotenv(".env")
env_cookie = os.getenv("WEIBO_COOKIE", "")

print("=" * 60)
print("CHECKING .ENV COOKIE")
print("=" * 60)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Cookie": env_cookie,
}
try:
    resp = requests.get("https://weibo.com/login", headers=headers, allow_redirects=True, timeout=15)
    final_url = resp.url
    redirected = "passport" in final_url or "login" in final_url
    print(f"HTTP Status: {resp.status_code}")
    print(f"Final URL: {final_url[:100]}")
    if redirected:
        print(f"\n❌ .ENV COOKIE ALSO EXPIRED")
    else:
        print(f"\n✅ .ENV COOKIE IS STILL VALID — use this one!")
except Exception as e:
    print(f"\nError: {e}")
