import requests, json

with open("/root/StreamCheck/StreamMonitor/weibo_cookies.json") as f:
    data = json.load(f)
cookie = data.get("cookie_str", "")

xsrf = ""
for part in cookie.split(";"):
    part = part.strip()
    if "=" in part:
        k, v = part.split("=", 1)
        if k.strip().upper() in ("XSRF-TOKEN", "XSRF_TOKEN"):
            xsrf = v.strip()
            break

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cookie": cookie,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://weibo.com/",
    "X-XSRF-TOKEN": xsrf,
}

# Test endpoints that might work for auth check
urls = [
    "https://weibo.com/ajax/statuses/config",
    "https://weibo.com/aj/onoff/get",
]

for url in urls:
    try:
        r = requests.get(url, headers=headers, timeout=15)
        print(url)
        print("  Status: " + str(r.status_code))
        if "passport" in r.url:
            print("  => REDIRECTED to passport")
        else:
            t = r.text[:200]
            print("  => " + t)
            if '"ok":1' in t:
                print("  => AUTHENTICATED!")
            elif '"code":"100001"' in t:
                print("  => NOT AUTHENTICATED")
    except Exception as e:
        print("  ERROR: " + str(e))
