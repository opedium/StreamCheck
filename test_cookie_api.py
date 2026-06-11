#!/usr/bin/env python3
"""Test Weibo cookie via authenticated API endpoints (GET only)."""
import os
import json
import requests

os.chdir(os.path.join(os.path.dirname(__file__), "StreamMonitor"))

cookie_str = (
    "SCF=AnJMll8Xl1cxRUWGdzmkb8aL1E3Xb6tGBCKNILnvCMWnMYNKGsVLXDy05WCgfxeT09gV7mNacrOvmgu9PFkexcE.; "
    "SINAGLOBAL=4518476414680.547.1765417317736; "
    "ULV=1780034121796:4:2:2:4320807043723.306.1780034121768:1779993332540; "
    "SUB=_2A25HLS3oDeRhGe5O6lcT8y_IzzuIHXVkQy8grDV8PUNbmtANLXXtkW9NdW_OFGc-O-84CC_3at73xjCBawmBKYCW; "
    "SUBP=0033WrSXqPxfM725Ws9jqgMF55529P9D9WWEzlNcikHadP6G3vnhG.cM5JpX5KzhUgL.Fon7eK-Ee02XShM2dJLoIXnLxKnLB.qL1h-LxKML12eL1-zLxK.LB.-L1K.LxKBLB.zL122LxK-LBKBLBK.LxKqL1KzLB-BLxKBLB.2L1hqLxK-L1K5L1KMt; "
    "__snaker__id=0EpYcQJtJ2JhZuSw; "
    "XSRF-TOKEN=TfryHGk7neZtnsQaBG0J7wDN; "
    "WBPSESS=wUrX5KUozmygptzm1wF0uiJaaETOT8ZIu-qmEeYe7sKB5yuwkSPnE1OJkiBDOBwZvnLxRoN53fY55ZU3tNSfwBkrFdO1HXypaKr-hmKlxm7uzW3E-fYl1iV72RYHy76RZ_QWv-nOdu6Qk3oSEe6lmQ==; "
    "ALF=02_1783687864"
)

xsrf_token = "TfryHGk7neZtnsQaBG0J7wDN"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Cookie": cookie_str,
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://weibo.com",
    "Referer": "https://weibo.com/",
    "X-XSRF-TOKEN": xsrf_token,
}

print("=== Test: GET weibo.com/ajax/side/getUser (requires auth) ===")
try:
    resp = requests.get(
        "https://weibo.com/ajax/side/getUser",
        headers=headers,
        timeout=15
    )
    data = resp.json()
    print(f"Status: {resp.status_code}")
    if data.get("ok") == 1:
        user = data.get("data", {}).get("user", {})
        print(f"✅ COOKIE VALID! Logged in as: {user.get('screen_name', 'unknown')}")
    else:
        print(f"Response: {json.dumps(data, ensure_ascii=False)[:200]}")
except Exception as e:
    print(f"Error: {e}")
    # Try fallback: get user info from another endpoint
    print("\n=== Fallback: GET weibo.com/aj/onoff/get ===")
    try:
        resp2 = requests.get(
            "https://weibo.com/aj/onoff/get",
            headers=headers,
            timeout=15
        )
        print(f"Status: {resp2.status_code}, URL: {resp2.url[:100]}")
        text = resp2.text[:300]
        print(f"Response: {text}")
    except Exception as e2:
        print(f"Error: {e2}")
