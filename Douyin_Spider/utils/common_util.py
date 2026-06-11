import json
import os

# Path to the shared cookie JSON (written by StreamMonitor cookie refresher)
_STREAMMONITOR_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../StreamMonitor")
)
_COOKIE_JSON = os.path.join(_STREAMMONITOR_DIR, "cookies.json")

dy_auth = None
dy_live_auth = None


def _read_douyin_cookie() -> str:
    """Read douyin cookie from StreamMonitor/cookies.json."""
    if not os.path.exists(_COOKIE_JSON):
        print(f"[common_util] Cookie file not found: {_COOKIE_JSON}")
        return ""
    try:
        with open(_COOKIE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data or {}).get("cookie_str", "")
    except Exception as e:
        print(f"[common_util] Failed to read cookie JSON: {e}")
        return ""


def load_env():
    global dy_auth, dy_live_auth
    cookie_str = _read_douyin_cookie()
    from builder.auth import DouyinAuth
    dy_auth = DouyinAuth()
    dy_auth.perepare_auth(cookie_str, "", "")
    dy_live_auth = DouyinAuth()
    dy_live_auth.perepare_auth(cookie_str, "", "")
    return dy_auth

def init():
    media_base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../datas/media_datas'))
    excel_base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../datas/excel_datas'))
    for base_path in [media_base_path, excel_base_path]:
        if not os.path.exists(base_path):
            os.makedirs(base_path)
            # logger.info(f'create {base_path}')
    cookies = load_env()
    base_path = {
        'media': media_base_path,
        'excel': excel_base_path,
    }
    return cookies, base_path
