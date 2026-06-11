#!/usr/bin/env python3
"""Inject fresh Weibo cookie into the browser profile."""
import json, os, sys
from datetime import datetime

from playwright.sync_api import sync_playwright

PROFILE_DIR = "/root/StreamCheck/StreamMonitor/weibo_browser_profile"
COOKIE_FILE = "/root/StreamCheck/StreamMonitor/weibo_cookies.json"

# Read the fresh cookie from the saved file
with open(COOKIE_FILE) as f:
    data = json.load(f)
cookie_str = data.get("cookie_str", "")
print(f"Cookie length: {len(cookie_str)} chars")

# Parse cookie string into Playwright cookie objects
# Determine domain based on cookie name (some are weibo.com, some are sina.com.cn)
cookie_domains = {
    "SCF": ".weibo.com",
    "SINAGLOBAL": ".sina.com.cn",
    "ULV": ".sina.com.cn",
    "SUB": ".weibo.com",
    "SUBP": ".weibo.com",
    "__snaker__id": ".weibo.com",
    "WBPSESS": ".weibo.com",
    "ALF": ".weibo.com",
    "XSRF-TOKEN": ".weibo.com",
}

cookies_playwright = []
for part in cookie_str.split(";"):
    part = part.strip()
    if "=" not in part:
        continue
    name, value = part.split("=", 1)
    name = name.strip()
    value = value.strip()
    domain = cookie_domains.get(name, ".weibo.com")
    cookies_playwright.append({
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "secure": True,
        "httpOnly": False,
        "sameSite": "Lax",
    })

print(f"Parsed {len(cookies_playwright)} cookies")

# Launch browser with the profile and inject cookies
with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=True,
        no_viewport=True,
    )
    page = browser.new_page()

    # Navigate to weibo.com first (required to set cookies for the domain)
    page.goto("https://weibo.com", wait_until="domcontentloaded", timeout=30000)

    # Inject all cookies
    for c in cookies_playwright:
        try:
            page.context.add_cookies([c])
        except Exception as e:
            print(f"  Warning: failed to set cookie {c['name']}: {e}")

    # Reload to verify
    page.goto("https://weibo.com", wait_until="domcontentloaded", timeout=30000)
    current_url = page.url
    status = "PASS" if "passport.weibo.com" not in current_url else "FAIL"
    print(f"Test: {status} — url={current_url[:80]}")

    page.close()
    browser.close()

# Update cookie file health status
data["health"] = "ok"
data["updated_at"] = datetime.now().isoformat()
with open(COOKIE_FILE, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print(f"Cookie health set to: ok")

sys.exit(0 if status == "PASS" else 1)
