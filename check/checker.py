#!/usr/bin/env python3
# coding=utf-8
"""
Bilibili Video Upload Checker

Polls the Bilibili API to detect when monitored uploaders post new videos.
Posts Weibo notifications when new uploads are detected.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv
from loguru import logger

from wbi_sign import get_wbi_keys, sign_params

# Paths to shared cookie JSON files (written by StreamMonitor cookie refreshers)
_CHECK_DIR = os.path.dirname(__file__)
_STREAMMONITOR_DIR = os.path.join(_CHECK_DIR, "..", "StreamMonitor")
_WEIBO_COOKIES_FILE = os.path.join(_STREAMMONITOR_DIR, "weibo_cookies.json")
_BILIBILI_COOKIES_FILE = os.path.join(_STREAMMONITOR_DIR, "bilibili_cookies.json")

# Force stdout/stderr to be unbuffered so log messages appear immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


# ======================================================================
# State Manager
# ======================================================================

class StateManager:
    """Manages per-uploader state (last known video) in state.json."""

    def __init__(self, state_file: str):
        self._state_file = state_file
        self._state: dict = {}
        self._load()

    def _load(self):
        """Load state from disk."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, 'r', encoding='utf-8') as f:
                    self._state = json.load(f)
                logger.info(f"Loaded state for {len(self._state)} uploader(s)")
            else:
                logger.info("No existing state file — cold start for all uploaders")
        except Exception as e:
            logger.warning(f"Failed to load state file: {e}")
            self._state = {}

    def _save(self):
        """Write state to disk atomically (temp file + rename)."""
        try:
            tmp_path = self._state_file + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._state_file)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def get(self, mid: str) -> Optional[dict]:
        """Get stored state for an uploader. Returns None if unknown."""
        return self._state.get(mid)

    def update(self, mid: str, video_info: dict):
        """Update stored state for an uploader with latest video info."""
        required_keys = ('bvid', 'created', 'title')
        for key in required_keys:
            if key not in video_info:
                logger.error(f"Cannot update state for mid={mid}: missing key '{key}' in video_info")
                return

        self._state[mid] = {
            'last_bvid': video_info['bvid'],
            'last_created': video_info['created'],
            'last_title': video_info['title'],
            'name': video_info.get('name', ''),
        }
        self._save()


# ======================================================================
# Weibo Poster (same pattern as StreamMonitor)
# ======================================================================

class WeiboPoster:
    """Posts status updates to Weibo via the web AJAX API."""

    WEB_HOST = "https://weibo.com"
    POST_URL = f"{WEB_HOST}/ajax/statuses/update"

    def __init__(self, web_cookie: str):
        self.web_cookie = web_cookie

    def post_tweet(self, content: str) -> bool:
        """Post a text status to Weibo. Returns True on success."""
        xsrf_token = self._extract_xsrf()
        if not xsrf_token:
            logger.error("No XSRF token found in Weibo cookie")
            return False

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Cookie": self.web_cookie,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.WEB_HOST,
            "Referer": f"{self.WEB_HOST}/",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-XSRF-TOKEN": xsrf_token,
        }

        data = {
            "content": content,
            "pic_id": "",
            "visible": "0",
            "share_id": "",
            "media": "{}",
            "vote": "{}",
            "approval_state": "0",
        }

        try:
            resp = requests.post(self.POST_URL, headers=headers, data=data, timeout=15)
            result = resp.json()
            if result.get("ok") == 1:
                logger.info("Weibo posted successfully")
                return True
            else:
                logger.error(f"Weibo API returned error: {result}")
                return False
        except Exception as e:
            logger.error(f"Failed to post to Weibo: {e}")
            return False

    def check_validity(self) -> bool:
        """Verify the Weibo cookie is still valid by hitting weibo.com/login.

        A working cookie loads the page normally; an expired cookie redirects
        to passport/login.  Returns True if the cookie is valid.
        """
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Cookie": self.web_cookie,
        }
        try:
            resp = requests.get(
                "https://weibo.com/login",
                headers=headers,
                allow_redirects=True,
                timeout=15,
            )
            final_url = resp.url or ""
            if "passport" in final_url or "login" in final_url:
                logger.warning(
                    f"Weibo cookie INVALID — redirected to {final_url[:80]}"
                )
                return False
            if resp.status_code != 200:
                logger.warning(
                    f"Weibo cookie check failed: HTTP {resp.status_code}"
                )
                return False
            logger.info("Weibo cookie is valid")
            return True
        except Exception as e:
            logger.warning(f"Weibo cookie check error: {e}")
            return False

    def _extract_xsrf(self) -> str:
        """Extract XSRF token from the Web cookie string."""
        for part in self.web_cookie.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip().upper()
                if k in ("XSRF-TOKEN", "XSRF_TOKEN"):
                    return v.strip()
        return ""


# ======================================================================
# Config Loading
# ======================================================================

def _load_cookie_from_json(json_path: str, env_var: str, label: str) -> str:
    """Read a cookie string from a shared JSON file, falling back to .env.

    The JSON files are written by the StreamMonitor cookie refreshers and
    have the format: ``{"cookie_str": "...", "health": "ok", ...}``.
    """
    # Try the shared JSON file first (written by cookie refreshers)
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cookie = (data or {}).get("cookie_str", "")
            if cookie:
                logger.info(f"Loaded {label} from {json_path}")
                return cookie
            else:
                logger.warning(f"{json_path} exists but has empty cookie_str — falling back to .env")
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to read {json_path}: {e} — falling back to .env")

    # Fall back to .env
    cookie = os.getenv(env_var, "")
    if cookie:
        logger.info(f"Loaded {label} from .env ({env_var})")
    return cookie


def load_config() -> dict:
    """Load configuration from shared cookie JSONs (preferred) or .env file."""
    load_dotenv()

    mids_str = os.getenv('BILI_MIDS', '')
    if not mids_str:
        logger.error("BILI_MIDS is not set in .env file")
        sys.exit(1)

    mids = [m.strip() for m in mids_str.split(',') if m.strip()]
    if not mids:
        logger.error("BILI_MIDS is set but contains no valid member IDs")
        sys.exit(1)

    weibo_cookie = _load_cookie_from_json(_WEIBO_COOKIES_FILE, 'WEIBO_COOKIE', 'Weibo cookie')
    if not weibo_cookie:
        logger.error(
            "Weibo cookie is not available.  "
            "Set WEIBO_COOKIE in .env or ensure StreamMonitor/weibo_cookies.json exists."
        )
        sys.exit(1)

    bili_cookie = _load_cookie_from_json(_BILIBILI_COOKIES_FILE, 'BILI_COOKIE', 'Bilibili cookie')

    try:
        check_interval = int(os.getenv('CHECK_INTERVAL', '300'))
        if check_interval <= 0:
            logger.error("CHECK_INTERVAL must be a positive integer")
            sys.exit(1)
    except ValueError:
        logger.error("CHECK_INTERVAL must be a valid integer")
        sys.exit(1)

    default_template = "【B站更新】{name} 发布了新视频：{title} https://www.bilibili.com/video/{bvid}"
    weibo_template = os.getenv('WEIBO_TEMPLATE', default_template)
    # Support \n newlines in .env (same pattern as StreamMonitor)
    weibo_template = weibo_template.replace('\\n', '\n')

    log_level = os.getenv('LOG_LEVEL', 'INFO')

    return {
        'mids': mids,
        'weibo_cookie': weibo_cookie,
        'check_interval': check_interval,
        'weibo_template': weibo_template,
        'bili_cookie': bili_cookie,
        'log_level': log_level,
    }


# ======================================================================
# Bilibili API Client
# ======================================================================

BILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

BILI_HEADERS = {
    "User-Agent": BILI_USER_AGENT,
    "Referer": "https://space.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://space.bilibili.com",
}


def fetch_latest_video(mid: str, img_key: str, sub_key: str,
                       bili_cookie: str = "") -> Optional[dict]:
    """
    Fetch the latest video for a given uploader mid.

    Calls /x/space/wbi/arc/search with WBI signing.
    Returns vlist[0] as a dict with keys: bvid, aid, title, created, author, etc.
    Returns None on failure.
    """
    params = {
        'mid': mid,
        'order': 'pubdate',
        'ps': '1',
        'pn': '1',
    }
    signed = sign_params(params, img_key, sub_key)

    try:
        req_headers = dict(BILI_HEADERS)
        if bili_cookie:
            req_headers["Cookie"] = bili_cookie

        resp = requests.get(
            'https://api.bilibili.com/x/space/wbi/arc/search',
            headers=req_headers,
            params=signed,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get('code') != 0:
            logger.error(f"Bilibili API error for mid={mid}: code={data.get('code')}, message={data.get('message')}")
            return None

        vlist = data.get('data', {}).get('list', {}).get('vlist', [])
        if not vlist:
            logger.warning(f"No videos found for mid={mid}")
            return None

        video = vlist[0]
        return {
            'bvid': video.get('bvid', ''),
            'aid': video.get('aid', 0),
            'title': video.get('title', ''),
            'created': video.get('created', 0),
            'name': video.get('author', ''),
            'length': video.get('length', ''),
            'play': video.get('play', 0),
            'description': video.get('description', ''),
        }
    except requests.Timeout:
        logger.error(f"Timeout fetching videos for mid={mid}")
        return None
    except requests.RequestException as e:
        logger.error(f"HTTP error fetching videos for mid={mid}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching videos for mid={mid}: {e}")
        return None


# ======================================================================
# Template Formatting
# ======================================================================

def format_template(template: str, video: dict, mid: str) -> str:
    """
    Format the Weibo notification template with video info.

    Available variables: {name}, {title}, {bvid}, {aid}, {created}, {mid}

    Args:
        template: Template string with {variable} placeholders
        video: Video info dict from fetch_latest_video
        mid: Uploader's member ID

    Returns:
        Formatted string ready to post
    """
    created_str = ""
    if video.get('created'):
        created_str = datetime.fromtimestamp(video['created']).strftime('%Y-%m-%d %H:%M')

    try:
        return template.format(
            name=video.get('name', ''),
            title=video.get('title', ''),
            bvid=video.get('bvid', ''),
            aid=str(video.get('aid', '')),
            created=created_str,
            mid=mid,
        )
    except (KeyError, ValueError) as e:
        logger.error(f"Template formatting failed: {e}. Template: {template!r}")
        # Fallback: simple message with the essential info
        return f"【B站更新】{video.get('name', '')} 发布了新视频：{video.get('title', '')} https://www.bilibili.com/video/{video.get('bvid', '')}"


# ======================================================================
# Notification Logger
# ======================================================================

def log_notification(log_file: str, mid: str, video: dict):
    """Append a notification event to the log file."""
    entry = {
        'timestamp': datetime.now().isoformat(),
        'mid': mid,
        'bvid': video.get('bvid', ''),
        'aid': video.get('aid', 0),
        'title': video.get('title', ''),
        'name': video.get('name', ''),
    }
    try:
        existing = []
        if os.path.exists(log_file):
            with open(log_file, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        existing.append(entry)
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write notification log: {e}")


# ======================================================================
# Main Loop
# ======================================================================

def main():
    """Main entry point — loads config, enters polling loop."""
    config = load_config()

    # Configure logging
    logger.remove()
    logger.add(
        sys.stderr,
        level=config['log_level'],
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        colorize=True,
    )

    mids = config['mids']
    weibo_cookie = config['weibo_cookie']
    check_interval = config['check_interval']
    weibo_template = config['weibo_template']
    bili_cookie = config['bili_cookie']

    logger.info(f"Bilibili Checker starting — monitoring {len(mids)} uploader(s): {', '.join(mids)}")
    logger.info(f"Check interval: {check_interval}s")
    logger.info(f"Template: {weibo_template[:60]}...")

    state_file = os.path.join(os.path.dirname(__file__), 'state.json')
    log_file = os.path.join(os.path.dirname(__file__), 'notification_log.json')

    state = StateManager(state_file)
    poster = WeiboPoster(weibo_cookie)

    # Verify Weibo cookie at startup — no point polling if the cookie is dead
    if not poster.check_validity():
        logger.error(
            "Weibo cookie is invalid at startup.  Either refresh the cookie "
            "manually or wait for the cookie refresher to run."
        )
        sys.exit(1)

    while True:
        try:
            # Refresh WBI keys (cached daily)
            img_key, sub_key = get_wbi_keys()

            for mid in mids:
                logger.debug(f"Checking mid={mid}...")

                video = fetch_latest_video(mid, img_key, sub_key, bili_cookie)
                if video is None:
                    logger.warning(f"Skipping mid={mid} due to API error — will retry next cycle")
                    continue

                bvid = video['bvid']
                stored = state.get(mid)

                if stored is None:
                    # Cold start — store baseline silently
                    state.update(mid, video)
                    created_display = ""
                    if video.get('created'):
                        created_display = f" from {datetime.fromtimestamp(video['created']).strftime('%Y-%m-%d %H:%M')}"
                    logger.info(
                        f"[{video['name'] or mid}] Cold start baseline: "
                        f"{video['title']} ({bvid}){created_display}"
                    )
                elif stored['last_bvid'] != bvid:
                    # New video detected!
                    logger.info(
                        f"[{video['name']}] NEW VIDEO: {video['title']} ({bvid})"
                    )

                    # Re-check cookie validity before posting
                    if not poster.check_validity():
                        logger.error(
                            f"Weibo cookie expired — skipping post for {bvid}, "
                            f"will retry next cycle"
                        )
                        continue

                    content = format_template(weibo_template, video, mid)
                    success = poster.post_tweet(content)
                    if success:
                        state.update(mid, video)
                        log_notification(log_file, mid, video)
                    else:
                        logger.error(
                            f"Weibo post failed for {bvid} — will NOT update state, "
                            f"will retry next cycle"
                        )
                else:
                    logger.debug(f"[{video['name'] or mid}] No new video (latest: {bvid})")

        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            traceback.print_exc()

        logger.debug(f"Sleeping {check_interval}s until next check...")
        time.sleep(check_interval)


if __name__ == '__main__':
    main()
