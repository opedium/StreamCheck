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

for url in [
    "https://weibo.com/ajax/side/getUser",
    "https://weibo.com/aj/onoff/get",
]:
    try:
        r = requests.get(url, headers=headers, timeout=15)
        print(url)
        print("  Status: " + str(r.status_code))
        if "passport" in r.url:
            print("  => REDIRECTED")
        else:
            t = r.text[:150]
            print("  => " + t)
    except Exception as e:
        print("  ERROR: " + str(e))
