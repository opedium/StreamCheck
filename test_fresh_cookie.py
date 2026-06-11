#!/usr/bin/env python3
"""Test fresh Weibo cookie and update files if valid."""
import json
import os
import requests

cookie_str = "SCF=AvIPcaeQNeOHNPVdhccmMxFgqzTEeTEljBHAAOS9v9FbOMyfnlmPOMPb5pR2MseX5KmtUOvgNMPHPgul5CgZ3Rk.; SINAGLOBAL=9639829857949.55.1763966019146; ULV=1765174589874:2:1:1:357822812055.0286.1765174589869:1763966019385; SUB=_2A25HJWC1DeRhGe5O6lcT8y_IzzuIHXVkW_x9rDV8PUJbkNAbLXfHkW1NdW_OFBl8dyBABdxyUMJu3O6RFQwTg3DB; SUBP=0033WrSXqPxfM725Ws9jqgMF55529P9D9WWEzlNcikHadP6G3vnhG.cM5JpX5KMhUgL.Fon7eK-Ee02XShM2dJLoIXnLxKnLB.qL1h-LxKML12eL1-zLxK.LB.-L1K.LxKBLB.zL122LxK-LBKBLBK.LxKqL1KzLB-BLxKBLB.2L1hqLxK-L1K5L1KMt; ALF=1783143909; WBPSESS=wUrX5KUozmygptzm1wF0uiJaaETOT8ZIu-qmEeYe7sKB5yuwkSPnE1OJkiBDOBwZvnLxRoN53fY55ZU3tNSfwI93AlxJTgjp4HbJWvkS1bBi5Twdp8ggM0xnLi86TagjyFkm6BBqsnDNqlQSJEfqnQ==; XSRF-TOKEN=95k57geaxOfyZx02nr6cXfxa"

os.chdir(os.path.join(os.path.dirname(__file__), "StreamMonitor"))

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cookie": cookie_str,
}
resp = requests.get("https://weibo.com/login", headers=headers, allow_redirects=True, timeout=15)
final_url = resp.url
redirected = "passport" in final_url or "login" in final_url

print(f"HTTP {resp.status_code}, URL: {final_url[:80]}")
if redirected:
    print("❌ Cookie still expired")
else:
    print("✅ Cookie is VALID!")

    # Update weibo_cookies.json
    with open("weibo_cookies.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    data["cookie_str"] = cookie_str
    data["health"] = "ok"
    from datetime import datetime
    data["updated_at"] = datetime.now().isoformat()
    with open("weibo_cookies.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("✅ Updated weibo_cookies.json")

    # Also update .env
    with open(".env", "r", encoding="utf-8") as f:
        env_content = f.read()
    # Replace WEIBO_COOKIE line
    import re
    env_content = re.sub(
        r"WEIBO_COOKIE='[^']*'",
        f"WEIBO_COOKIE='{cookie_str}'",
        env_content,
    )
    with open(".env", "w", encoding="utf-8") as f:
        f.write(env_content)
    print("✅ Updated .env WEIBO_COOKIE")
