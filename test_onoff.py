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

# Test with and without XSRF header
for label, h in [("WITH XSRF header", headers), ("WITHOUT XSRF header", {k:v for k,v in headers.items() if k != "X-XSRF-TOKEN"})]:
    try:
        r = requests.get("https://weibo.com/aj/onoff/get", headers=h, timeout=15)
        print(label)
        print("  Status: " + str(r.status_code))
        if "passport" in r.url:
            print("  => REDIRECTED")
        else:
            print("  => " + r.text[:150])
    except Exception as e:
        print("  ERROR: " + str(e))

# Also try m.weibo.cn API
print("---")
print("m.weibo.cn/api/config:")
try:
    m_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)",
        "Cookie": cookie,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://m.weibo.cn/",
    }
    r = requests.get("https://m.weibo.cn/api/config", headers=m_headers, timeout=15)
    print("  Status: " + str(r.status_code))
    print("  => " + r.text[:200])
except Exception as e:
    print("  ERROR: " + str(e))
