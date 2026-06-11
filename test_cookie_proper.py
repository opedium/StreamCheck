#!/usr/bin/env python3
"""Test Weibo cookie by hitting the homepage (GET) and checking for logged-in content."""
import os
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

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Cookie": cookie_str,
}

print("=== Test 1: weibo.com homepage (GET) ===")
resp = requests.get("https://weibo.com", headers=headers, allow_redirects=True, timeout=15)
print(f"Status: {resp.status_code}, Final URL: {resp.url[:100]}")
print(f"Body length: {len(resp.text)}")
# Check for logged-in indicators in the HTML
has_login_indicator = (
    "onick" in resp.text or           # user nickname field
    "plc_" in resp.text or            # logged-in container
    "miniblog" in resp.text or        # miniblog (logged-in version)
    "WB_handle" in resp.text or       # Weibo handle element
    "pub_box" in resp.text or         # publish box (logged-in only)
    "vue-render" in resp.text         # SPA render (logged-in)
)
print(f"Has logged-in content: {has_login_indicator}")
if "passport" in resp.url:
    print("Result: REDIRECTED TO PASSPORT")
elif has_login_indicator:
    print("Result: ✅ LOGGED IN (homepage shows user content)")
else:
    print("Result: Got weibo.com page but unclear if logged in")
    # Show first 500 chars for debugging
    print(f"Preview: {resp.text[:300]}")

print()

print("=== Test 2: weibo.com/login (GET) — same as monitor's check_validity ===")
resp2 = requests.get("https://weibo.com/login", headers=headers, allow_redirects=True, timeout=15)
print(f"Status: {resp2.status_code}, Final URL: {resp2.url[:100]}")
if "passport" in resp2.url or "login" in resp2.url:
    print("Result: REDIRECTED — monitor would say INVALID")
else:
    print("Result: OK — monitor would say VALID")
