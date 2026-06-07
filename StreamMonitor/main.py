#!/usr/bin/env python3
# coding=utf-8
"""
Stream Monitor - Continuously checks if a Douyin stream is live,
sends Weibo notifications when stream goes live/offline,
and records detailed live statistics for post-stream summaries.
"""

import os
import sys
import re
import time
import json
import gzip
import argparse
import threading
import builtins
from datetime import datetime
from urllib.parse import urlencode

# Cookie refresh system
from cookie_manager import CookieManager
from telegram_notifier import TelegramNotifier

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from loguru import logger
from websocket import WebSocketApp

# Suppress the InsecureRequestWarning from urllib3
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Force stdout/stderr to be unbuffered so log messages appear immediately
sys.stdout.reconfigure(line_buffering=True)  # Python 3.7+
sys.stderr.reconfigure(line_buffering=True)


# ======================================================================
# Python 2 → 3 compatibility for the 'protobuf_to_dict' package
# ======================================================================
if not hasattr(builtins, 'long'):
    builtins.long = int
if not hasattr(builtins, 'unicode'):
    builtins.unicode = str
if not hasattr(builtins, 'basestring'):
    builtins.basestring = str


# Try to import Douyin_Spider protobuf types (optional)
HAS_LIVE_DETAILS = False
Live_pb2 = None
DouyinAPI = None
HeaderBuilder = None
Params = None
generate_signature = None

try:
    dy_path = os.path.join(os.path.dirname(__file__), '..', 'Douyin_Spider')
    dy_path = os.path.abspath(dy_path)

    # CRITICAL: Remove any installed 'static' package from sys.modules so our
    # Douyin_Spider/static/ directory takes precedence when DouyinAPI imports it.
    for key in list(sys.modules.keys()):
        if key.startswith('static') or 'static.' in key:
            del sys.modules[key]

    if dy_path not in sys.path:
        sys.path.insert(0, dy_path)

    from dy_apis.douyin_api import DouyinAPI  # type: ignore[import-untyped]
    import static.Live_pb2 as Live_pb2  # type: ignore[import-untyped]
    from builder.header import HeaderBuilder  # type: ignore[import-untyped]
    from builder.params import Params  # type: ignore[import-untyped]
    from utils.dy_util import generate_signature  # type: ignore[import-untyped]
    HAS_LIVE_DETAILS = True

except Exception as e:
    import traceback
    print(f"[STARTUP] Failed to import Douyin_Spider modules: {e}", flush=True)
    traceback.print_exc()
    Live_pb2 = None
    DouyinAPI = None
    HeaderBuilder = None
    Params = None
    generate_signature = None
    HAS_LIVE_DETAILS = False


# Stats JSON file path for the web dashboard (configurable via env var)
_STATS_FILE = os.path.join(os.path.dirname(__file__), 'live_stats.json')
STATS_FILE = os.environ.get('STATS_FILE', _STATS_FILE)


# ======================================================================
# Formatters for human-readable numbers
# ======================================================================

def fmt_wan(val: float) -> str:
    """Format a number into 万 (10k) unit.
    e.g. 48830000 -> "4883万",  523600 -> "52.36万"
    """
    if val >= 10000:
        wan = val / 10000
        # Exact multiples of 10000: clean integer display (no decimal)
        if wan == int(wan):
            return f"{int(wan)}万"
        s = f"{wan:.2f}万"
        # Strip trailing zeros after decimal: "52.30万" → "52.3万"
        s = re.sub(r'\.(\d*?)0+万', r'.\1万', s)
        # If all fractional digits were zero (bare decimal point): "52.万" → "52.0万"
        s = re.sub(r'\.万', '.0万', s)
        return s
    return str(int(val))


def fmt_num(val: int) -> str:
    """Format integer with thousands separator."""
    return f"{val:,}"


def strip_emoji(text: str) -> str:
    """Remove emoji characters from a string."""
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002702-\U000027B0"  # dingbats
        "\U000024C2-\U0000255F"  # enclosed/misc symbols only (not CJK)
        "\U00002934-\U0000293F"  # arrows
        "\U00002B50-\U00002B59"  # star/phone symbols
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        "\U0001FA00-\U0001FA6F"  # Chess Symbols
        "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        "\U00002600-\U000026FF"  # misc symbols
        "\U00002764"             # red heart
        "\U0000200D"             # zero width joiner
        "\U0000FE0F"             # variation selector-16
        "\U00002300-\U000023FF"  # misc technical
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub('', text).strip()


def parse_chinese_number(text: str) -> int:
    """Parse Chinese number string like '488.3万' to int."""
    try:
        if isinstance(text, str) and '万' in text:
            return int(float(text.replace('万', '')) * 10000)
        return int(float(text))
    except Exception:
        return 0


# ======================================================================
# Data fields found in displayLong (RoomStatsMessage)
# ======================================================================

DISPLAY_PATTERNS = {
    '点赞': r'(?:点赞|like|赞)\s*[:：]?\s*([\d.]+)万?',
    '观看': r'(?:观看|累计观看|view)\s*[:：]?\s*([\d.]+)万?',
    '在线': r'(?:在线|在线人数|当前在线)\s*[:：]?\s*([\d.]+)万?',
    '最高在线': r'(?:最高在线|峰值|peak)\s*[:：]?\s*([\d.]+)万?',
    '粉丝团': r'(?:粉丝团|member)\s*[:：]?\s*([\d.]+)万?',
    '灯牌': r'(?:灯牌|light|badge)\s*[:：]?\s*([\d.]+)万?',
}


def parse_display_long(display_long: str) -> dict:
    """Try to parse known fields from displayLong string."""
    result = {}
    for key, pattern in DISPLAY_PATTERNS.items():
        m = re.search(pattern, display_long, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            result[key] = val
    return result


# ======================================================================
# Manual protobuf wire format parser for RoomUserSeqMessage
# Extracts totalPvForAnchor (field 11, string type) which contains
# the actual cumulative view count (e.g. "381.2万" or "3811912")
# ======================================================================

def _parse_varint(data: bytes, offset: int):
    """Decode a protobuf varint at the given offset, return (value, new_offset)."""
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("Unexpected end of data while parsing varint")
        byte = data[offset]
        value |= (byte & 0x7F) << shift
        shift += 7
        offset += 1
        if not (byte & 0x80):
            break
    return value, offset


def parse_room_user_seq_pv(payload: bytes) -> int:
    """
    Parse RoomUserSeqMessage protobuf bytes to extract totalPvForAnchor (field 11).
    Field 11 is a string type (wire type 2).
    Tag byte = (11 << 3) | 2 = 90 = 0x5A.
    Returns the parsed integer value, or 0 on failure.
    """
    offset = 0
    try:
        while offset < len(payload):
            tag, offset = _parse_varint(payload, offset)
            field_num = tag >> 3
            wire_type = tag & 0x7

            if field_num == 11 and wire_type == 2:
                # This is totalPvForAnchor - read the string length and value
                str_len, offset = _parse_varint(payload, offset)
                str_bytes = payload[offset:offset + str_len]
                str_val = str_bytes.decode('utf-8', errors='replace')
                # Parse the Chinese number like "381.2万" or plain integer string
                return parse_chinese_number(str_val)
            elif wire_type == 0:
                # Varint - skip
                _, offset = _parse_varint(payload, offset)
            elif wire_type == 1:
                # 64-bit - skip 8 bytes
                offset += 8
            elif wire_type == 2:
                # Length-delimited - skip
                length, offset = _parse_varint(payload, offset)
                offset += length
            elif wire_type == 5:
                # 32-bit - skip 4 bytes
                offset += 4
            else:
                break  # Unknown wire type
    except Exception:
        pass
    return 0


def parse_fansclub_msg(payload: bytes) -> dict:
    """
    Parse FansclubMessage protobuf bytes to extract type, content and total member count.

    FansclubMessage schema:
      Common commonInfo = 1;  // nested message (wire type 2)
      int32  type       = 2;  // 1=upgrade, 2=join (wire type 0)
      string content    = 3;  // e.g. "恭喜 xxx 成为粉丝团第289687名成员" (wire type 2)
      User   user       = 4;  // nested message (wire type 2)

    total_members is parsed from the content string via multiple regex patterns
    to handle different Douyin content formats.
    Returns dict with keys: type, content, total_members (int, 0 if unparseable).
    """
    result = {'type': 0, 'content': '', 'total_members': 0}
    offset = 0
    try:
        while offset < len(payload):
            tag, offset = _parse_varint(payload, offset)
            field_num = tag >> 3
            wire_type = tag & 0x7

            if field_num == 2 and wire_type == 0:
                # int32 type: 1=upgrade, 2=join
                result['type'], offset = _parse_varint(payload, offset)
            elif field_num == 3 and wire_type == 2:
                # string content
                length, offset = _parse_varint(payload, offset)
                content_bytes = payload[offset:offset + length]
                result['content'] = content_bytes.decode('utf-8', errors='replace')
                offset += length
                # Extract total member count from content.
                # Douyin uses several formats:
                #   "恭喜 xxx 成为粉丝团第289687名成员"
                #   "加入了粉丝团，他是第528名成员"
                #   "xxx 加入粉丝团，当前团成员 12345"
                #   "恭喜 XY 成为第5667名KOI成员"  (品牌名插入在"名"和"成员"之间)
                for pattern in [
                    r'第(\d+)名.*成员',
                    r'第(\d+)位.*成员',
                    r'团成员\s*(\d+)',
                    r'(\d+)名.*成员',
                ]:
                    m = re.search(pattern, result['content'])
                    if m:
                        result['total_members'] = int(m.group(1))
                        break
            elif wire_type == 0:
                _, offset = _parse_varint(payload, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 2:
                length, offset = _parse_varint(payload, offset)
                offset += length
            elif wire_type == 5:
                offset += 4
            else:
                break
    except Exception:
        pass
    return result


def parse_gift_dedup_key(payload: bytes) -> tuple:
    """
    Extract (group_id, repeat_count, user_id) from raw GiftMessage protobuf bytes.
    Field numbers from DouyinBarrage base/messages.py:
      field 5  = repeat_count (uint64)
      field 7  = user (User sub-message, field 1 = id)
      field 11 = group_id (uint64)

    IMPORTANT: Protobuf serializes fields in field-number order (5 → 7 → 11).
    We must NOT break out of the loop after finding any single field — we need
    to keep parsing until we've seen all three or exhausted the payload.

    Returns (group_id, repeat_count, user_id).  Returns (0, 0, 0) on parse
    failure — fails open so gifts are still counted.
    """
    group_id = 0
    repeat_count = 0
    user_id = 0
    offset = 0
    try:
        while offset < len(payload):
            tag, offset = _parse_varint(payload, offset)
            field_num = tag >> 3
            wire_type = tag & 0x7

            if field_num == 5 and wire_type == 0:
                repeat_count, offset = _parse_varint(payload, offset)
            elif field_num == 11 and wire_type == 0:
                group_id, offset = _parse_varint(payload, offset)
            elif field_num == 7 and wire_type == 2:
                # User sub-message — extract field 1 (id) from it
                length, offset = _parse_varint(payload, offset)
                user_bytes = payload[offset:offset + length]
                offset += length
                u_off = 0
                while u_off < len(user_bytes):
                    u_tag, u_off = _parse_varint(user_bytes, u_off)
                    u_field = u_tag >> 3
                    u_wire = u_tag & 0x7
                    if u_field == 1 and u_wire == 0:
                        user_id, u_off = _parse_varint(user_bytes, u_off)
                        break
                    elif u_wire == 0:
                        _, u_off = _parse_varint(user_bytes, u_off)
                    elif u_wire == 1:
                        u_off += 8
                    elif u_wire == 2:
                        l, u_off = _parse_varint(user_bytes, u_off)
                        u_off += l
                    elif u_wire == 5:
                        u_off += 4
                    else:
                        break
                # Continue parsing — field 11 (group_id) comes AFTER field 7
            elif wire_type == 0:
                _, offset = _parse_varint(payload, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 2:
                length, offset = _parse_varint(payload, offset)
                offset += length
            elif wire_type == 5:
                offset += 4
            else:
                break
    except Exception:
        pass
    return (group_id, repeat_count, user_id)


# ======================================================================
# Live Stats Recorder (WebSocket-based, embedded in StreamMonitor)
# ======================================================================

class LiveStatsRecorder:
    def __init__(self, live_id: str, dy_cookie_str: str, verbose: bool = True):
        self.live_id = live_id
        self.cookie_str = dy_cookie_str
        self.verbose = verbose
        self.ws = None

        # counters
        self.total_likes = 0
        self.new_follows = 0
        self.new_members = 0
        self.member_count = 0          # max memberCount from MemberMessage protobuf
        self.light_badges = 0
        self.fan_club_joins = 0        # FansclubMessage type=2 event count
        self.fan_club_gift_joins = 0   # 入团卡 (join-card) gift count
        self.fan_club_start_count = 0  # total members from first fansclub msg
        self.fan_club_end_count = 0    # total members from latest fansclub msg
        self.gift_events = []

        # Gift dedup: (group_id, gift_name, user_id) → last_repeat_count
        self._gift_dedup = {}
        self._gift_dedup_last_cleanup = datetime.now()

        # viewer tracking
        self.current_viewers = 0
        self.viewer_samples = []       # per-minute viewer snapshots (at most 1/min)
        self._last_minute_sample = None # throttle RoomStatsMessage sampling to 1/min
        self.peak_viewers = 0
        self.peak_viewer_time = ""
        self.cumulative_views = 0
        self.display_long_history = []

        # follower snapshot
        self.follower_before = 0
        self.follower_after = 0
        self.anchor_nickname = ""

        # timing
        self.stream_start_time = None
        self.stream_end_time = None
        self._stop_event = threading.Event()
        
        # WS disconnection tracking and recovery
        self.ws_disconnected = False
        self.ws_disconnect_time = None
        self.ws_recovery_attempted = False
        self.http_cumulative_recovery = False  # True if we used HTTP to recover cumulative metrics
        self._reconnect_count = 0              # >0 means we've reconnected at least once
        self._last_http_refresh = None         # datetime of last HTTP cumulative refresh
        self._pre_snapshot_done = threading.Event()  # set when _take_pre_snapshot() completes

    def start_background(self, callback=None):
        self._callback = callback
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(f"LiveStatsRecorder started for live.douyin.com/{self.live_id}")

    def stop(self):
        self._stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self._write_live_stats_json(live=False)

    def attempt_ws_recovery(self, checker: 'DouyinLiveChecker' = None):
        """Attempt to recover from WS disconnection via reconnection + HTTP fallback.

        Returns:
            True if recovery (either WS reconnection or HTTP fallback) was successful
            False if recovery failed
        """
        if self.ws_recovery_attempted:
            # Allow retry after 10-minute cooldown in case network recovers
            cooldown = getattr(self, '_last_recovery_attempt_time', None)
            if cooldown is not None:
                if (datetime.now() - cooldown).total_seconds() < 600:
                    logger.debug("WS recovery already attempted recently, skipping (cooldown active)")
                    return self.http_cumulative_recovery
                logger.info("[WS Recovery] Cooldown expired, retrying WS reconnection...")
            else:
                logger.debug("WS recovery already attempted, skipping duplicate attempt")
                return self.http_cumulative_recovery

        self.ws_recovery_attempted = True
        self._last_recovery_attempt_time = datetime.now()
        
        # Strategy 1: Attempt WS reconnection in a background thread (must NOT block main loop)
        logger.warning(f"[WS Recovery] Attempting WebSocket reconnection for room {self.live_id}")
        retry_count = 0
        max_retries = 2
        backoff_delay = 3
        
        while retry_count < max_retries:
            try:
                from builder.auth import DouyinAuth  # type: ignore[import-untyped]
                auth = DouyinAuth()
                auth.perepare_auth(self.cookie_str, "", "")
                
                import sys
                old_stdout = sys.stdout
                sys.stdout = open(os.devnull, 'w')
                try:
                    # Verify live room is still reachable before reconnecting
                    DouyinAPI.get_live_info(auth, self.live_id)
                finally:
                    sys.stdout = old_stdout
                
                logger.info(f"[WS Recovery] Reconnection attempt {retry_count + 1}: room still reachable, restarting WS in background...")
                # Reset state so _connect_ws's finally block re-arms correctly
                self._reconnect_count += 1
                self._stop_event.clear()
                self.ws_disconnected = False
                self.ws_recovery_attempted = False  # allow future recoveries
                # Start in background thread - DO NOT call _connect_ws directly here
                # as run_forever() would block the main monitoring loop.
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
                return True
            except Exception as e:
                retry_count += 1
                if retry_count < max_retries:
                    logger.warning(f"[WS Recovery] Reconnection attempt {retry_count} failed: {e}. Retrying in {backoff_delay}s...")
                    time.sleep(backoff_delay)
                    backoff_delay = int(backoff_delay * 1.5)
                else:
                    logger.warning(f"[WS Recovery] WebSocket reconnection failed after {max_retries} attempts")
        
        # Strategy 2: Fallback to HTTP to fetch cumulative metrics
        logger.info("[WS Recovery] WebSocket reconnection failed, attempting HTTP fallback for cumulative metrics...")
        return self.fetch_cumulative_via_http()
    
    def fetch_cumulative_via_http(self, mark_recovery=True):
        """Fetch cumulative metrics via HTTP.
        Updates: cumulative_views, total_likes, follower_after

        Args:
            mark_recovery: If True, sets http_cumulative_recovery flag
                           (used during WS disconnect recovery).
                           Set False for periodic healthy refreshes.

        Returns True if successful, False otherwise.
        """
        try:
            from builder.auth import DouyinAuth
            auth = DouyinAuth()
            auth.perepare_auth(self.cookie_str, "", "")
            
            import sys
            old_stdout = sys.stdout
            sys.stdout = open(os.devnull, 'w')
            try:
                room_info = DouyinAPI.get_live_info(auth, self.live_id)
            finally:
                sys.stdout = old_stdout
            
            room_id = room_info['room_id']
            user_id = room_info['user_id']
            
            # get_webcast_detail returns the initial bootstrap proto (cursor/internalExt)
            # which does NOT contain stats messages - skip it.
            # Instead just log that we have room_id for reference.
            logger.info(f"[HTTP Recovery] Room confirmed: room_id={room_id}, user_id={user_id}")
            
            # Get follower count from user info
            sec_uid = room_info.get('sec_uid', '')
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                old_stdout = sys.stdout
                sys.stdout = open(os.devnull, 'w')
                try:
                    user_info = DouyinAPI.get_user_info(auth, user_url)
                finally:
                    sys.stdout = old_stdout
                
                user_data = user_info.get('user', {})
                fc = user_data.get('follower_count', 0)
                if isinstance(fc, str):
                    fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
                fc_int = int(fc)
                self.follower_after = fc_int
                logger.info(f"[HTTP Recovery] Updated follower_after to {self.follower_after:,}")
            
            if mark_recovery:
                self.http_cumulative_recovery = True
            self._last_http_refresh = datetime.now()
            logger.info("[HTTP Recovery] Successfully recovered cumulative metrics via HTTP")
            return True
        except Exception as e:
            logger.error(f"[HTTP Recovery] Failed to fetch cumulative metrics: {e}")
            return False

    def is_running(self):
        return not self._stop_event.is_set() and self._thread.is_alive() if hasattr(self, '_thread') else False

    def _run(self):
        if not HAS_LIVE_DETAILS:
            logger.error("Cannot start LiveStatsRecorder: Douyin_Spider modules not available")
            return

        cookie_dict = {}
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookie_dict[k.strip()] = v.strip()

        from builder.auth import DouyinAuth  # type: ignore[import-untyped]
        auth = DouyinAuth()
        auth.perepare_auth(self.cookie_str, "", "")
        self._connect_ws(auth, cookie_dict)

    def _connect_ws(self, auth, cookie_dict):
        try:
            # Suppress stdout from DouyinAPI calls (they may print dicts)
            import sys
            old_stdout = sys.stdout
            sys.stdout = open(os.devnull, 'w')
            try:
                room_info = DouyinAPI.get_live_info(auth, self.live_id)
            finally:
                sys.stdout = old_stdout
            
            room_id = room_info['room_id']
            user_id = room_info['user_id']
            ttwid = room_info['ttwid']
            try:
                # Only take pre-snapshot on initial connect, not on reconnect.
                # On reconnect, follower_before must keep the original value.
                if self._reconnect_count == 0:
                    self._take_pre_snapshot(auth, room_info)
            except Exception as e:
                logger.warning(f"LiveStatsRecorder: Pre-snapshot failed (non-fatal, continuing): {e}")
        except Exception as e:
            logger.error(f"LiveStatsRecorder: Failed to get live room info: {e}")
            return

        params = Params()
        res = DouyinAPI.get_webcast_detail(
            auth, str(user_id), room_id,
            f"https://live.douyin.com/{self.live_id}"
        )
        frame = Live_pb2.LiveResponse()
        frame.ParseFromString(res)

        (params
         .add_param('app_name', 'douyin_web')
         .add_param('version_code', '180800')
         .add_param('webcast_sdk_version', '1.0.15')
         .add_param('update_version_code', '1.0.15')
         .add_param('compress', 'gzip')
         .add_param('device_platform', 'web')
         .add_param('cookie_enabled', 'true')
         .add_param('screen_width', '1707')
         .add_param('screen_height', '960')
         .add_param('browser_language', 'zh-CN')
         .add_param('browser_platform', 'Win32')
         .add_param('browser_name', 'Mozilla')
         .add_param('browser_version',
                    HeaderBuilder.ua.split('Mozilla/')[-1])
         .add_param('browser_online', 'true')
         .add_param('tz_name', 'Etc/GMT-8')
         .add_param('cursor', str(frame.cursor))
         .add_param('internal_ext', frame.internalExt)
         .add_param('host', 'https://live.douyin.com')
         .add_param('aid', '6383')
         .add_param('live_id', '1')
         .add_param('did_rule', '3')
         .add_param('endpoint', 'live_pc')
         .add_param('support_wrds', '1')
         .add_param('user_unique_id', str(user_id))
         .add_param('im_path', '/webcast/im/fetch/')
         .add_param('identity', 'audience')
         .add_param('need_persist_msg_count', '15')
         .add_param('insert_task_id', '')
         .add_param('live_reason', '')
         .add_param('room_id', room_id)
         .add_param('heartbeatDuration', '0')
         .add_param('signature', generate_signature(room_id, user_id))
        )
        wss_url = f"wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/?{urlencode(params.get())}"

        self.ws = WebSocketApp(
            url=wss_url,
            header={
                'Pragma': 'no-cache',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
                'User-Agent': HeaderBuilder.ua,
                'Upgrade': 'websocket',
                'Cache-Control': 'no-cache',
                'Connection': 'Upgrade',
            },
            cookie=auth.cookie_str,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open
        )

        try:
            self.ws.run_forever(origin='https://live.douyin.com')
        except Exception:
            pass
        finally:
            self._stop_event.set()
            # Only run end-of-stream cleanup on an intentional stop (stop() called)
            # or final disconnect.  On unexpected mid-stream disconnect
            # (ws_disconnected=True), skip cleanup — reconnection will resume.
            if not self.ws_disconnected and self.stream_end_time is None:
                self.stream_end_time = datetime.now()
                self._write_live_stats_json(live=False)
                self._take_post_snapshot(auth)
                self._generate_summary()
                if hasattr(self, '_callback') and self._callback:
                    self._callback(self)

    def _take_pre_snapshot(self, auth, room_info):
        try:
            sec_uid = room_info.get('sec_uid', '')
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                # Suppress stdout from DouyinAPI (it prints debug dicts)
                import sys
                old_stdout = sys.stdout
                sys.stdout = open(os.devnull, 'w')
                try:
                    user_info = DouyinAPI.get_user_info(auth, user_url)
                finally:
                    sys.stdout = old_stdout
                
                user_data = user_info.get('user', {})
                self.anchor_nickname = user_data.get('nickname', '')
                fc = user_data.get('follower_count', 0)
                if isinstance(fc, str):
                    fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
                # Always capture fresh — never use seed data for follower_before.
                # Each stream start is a new baseline.
                self.follower_before = int(fc)

                # ── Fan club member count (pre-stream baseline) ──────
                # Log the API response structure so we can discover the
                # correct field name for fan club total members.
                logger.info(
                    f"[StatsRecorder] user_info top-level keys: "
                    f"{list(user_info.keys())}"
                )
                logger.info(
                    f"[StatsRecorder] user_data keys: "
                    f"{list(user_data.keys())}"
                )
                # Try common field-name patterns for fan club member count.
                # Priority: known Douyin API fields → common patterns.
                club_count = (
                    user_data.get('fans_club_count')
                    or user_data.get('fansclub_count')
                    or user_data.get('fansclub_member_count')
                    or user_data.get('club_member_count')
                )
                # Also check nested structures
                room_data = user_data.get('room_data', {}) or {}
                if isinstance(room_data, dict):
                    club_count = club_count or room_data.get('fansclub_count')
                if club_count is not None:
                    club_count = int(club_count)
                    self.fan_club_start_count = club_count
                    self.fan_club_end_count = club_count
                    logger.info(
                        f"[StatsRecorder] Fan club pre-stream count "
                        f"(from user profile API): {club_count:,}"
                    )

                if self.verbose:
                    logger.info(f"[StatsRecorder] Pre-snapshot: nickname={self.anchor_nickname}, followers={self.follower_before}")
            else:
                self.anchor_nickname = room_info.get('room_title', '')
        except Exception as e:
            logger.warning(f"[StatsRecorder] Pre-snapshot failed: {e}")
        finally:
            self._pre_snapshot_done.set()

    def _take_post_snapshot(self, auth, fallback_sec_uid: str = ""):
        """Fetch current follower count from Douyin API and update follower_after.

        Args:
            auth: DouyinAuth instance.
            fallback_sec_uid: If get_live_info fails or returns no sec_uid
                              (common when the stream just ended), use this
                              sec_uid to still fetch the follower count.
        """
        try:
            # Suppress stdout from DouyinAPI (it prints debug dicts)
            import sys
            old_stdout = sys.stdout
            sys.stdout = open(os.devnull, 'w')
            try:
                room_info = DouyinAPI.get_live_info(auth, self.live_id)
            finally:
                sys.stdout = old_stdout

            sec_uid = room_info.get('sec_uid', '') or fallback_sec_uid
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                old_stdout = sys.stdout
                sys.stdout = open(os.devnull, 'w')
                try:
                    user_info = DouyinAPI.get_user_info(auth, user_url)
                finally:
                    sys.stdout = old_stdout

                user_data = user_info.get('user', {})
                fc = user_data.get('follower_count', 0)
                if isinstance(fc, str):
                    fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
                self.follower_after = int(fc)
                if self.verbose:
                    logger.info(f"[StatsRecorder] Post-snapshot: followers={self.follower_after}, delta={self.follower_after - self.follower_before}")
        except Exception as e:
            logger.warning(f"[StatsRecorder] Post-snapshot failed: {e}")

    def _get_follower(self, auth, sec_uid: str) -> int:
        """Fetch the current follower count for a given sec_uid.

        Direct API call — no get_live_info round-trip needed.
        Returns 0 on failure.
        """
        try:
            import sys
            user_url = f"https://www.douyin.com/user/{sec_uid}"
            old_stdout = sys.stdout
            sys.stdout = open(os.devnull, 'w')
            try:
                user_info = DouyinAPI.get_user_info(auth, user_url)
            finally:
                sys.stdout = old_stdout

            user_data = user_info.get('user', {})
            fc = user_data.get('follower_count', 0)
            if isinstance(fc, str):
                fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
            return int(fc)
        except Exception as e:
            logger.warning(f"[StatsRecorder] _get_follower failed for {sec_uid}: {e}")
            return 0

    def _on_open(self, ws):
        logger.info(f"[StatsRecorder] WebSocket connected to room {self.live_id}")
        # Apply seed BEFORE setting stream_start_time so the seed's original
        # start time (from before a restart) takes precedence over the reconnect time.
        self._write_live_stats_json()
        # Preserve original start time across reconnects so duration reflects
        # the full broadcast, not just the most recent connection.
        if self.stream_start_time is None:
            self.stream_start_time = datetime.now()
        threading.Thread(target=self._ping, args=(ws,), daemon=True).start()
        threading.Thread(target=self._periodic_summary, args=(ws,), daemon=True).start()
        if self.verbose:
            logger.info(f"[StatsRecorder] Started recording at {self.stream_start_time.strftime('%H:%M:%S')}")

    def _ping(self, ws):
        while not self._stop_event.is_set():
            frame = Live_pb2.PushFrame()
            frame.payloadType = "hb"
            try:
                ws.send(frame.SerializeToString(), opcode=0x02)
            except Exception:
                break
            # Refresh the dashboard JSON every 5s so the frontend always has fresh data
            self._write_live_stats_json()
            time.sleep(5)

    def _periodic_summary(self, ws):
        counter = 0
        while not self._stop_event.is_set():
            time.sleep(60)
            if self._stop_event.is_set():
                break
            self._cleanup_gift_dedup()
            counter += 1

            # ── Periodic follower_after refresh (every 1 minute) ────
            # follower_after drives _get_new_follows() delta, so we must
            # keep it current via HTTP API calls.  Without this periodic
            # refresh, follower_after stays frozen and the delta-based
            # follow count never grows during a healthy stream.
            try:
                self.fetch_cumulative_via_http(mark_recovery=False)
            except Exception:
                pass

            print(f"\n{'─'*60}")
            print(f"  📊 直播中概览 (已直播{counter}分钟) - {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'─'*60}")

            avg_viewers = sum(self.viewer_samples) // len(self.viewer_samples) if self.viewer_samples else 0
            peak = self.peak_viewers
            likes = self.total_likes
            badge = self.light_badges
            # Use API snapshot delta (follower_after - follower_before)
            # which captures NET change including non-live-room follows.
            # follower_after is now periodically refreshed (see above).
            gifts = len(self.gift_events)

            views_wan = self._try_get_wan('观看')
            likes_wan = self._try_get_wan('点赞')
            peak_wan = self._try_get_wan('最高在线')
            current_online = self.viewer_samples[-1] if self.viewer_samples else 0

            print(f"  👀 当前在线：{fmt_wan(current_online)}人")
            if self.cumulative_views:
                print(f"  👁 场观：{fmt_wan(self.cumulative_views)}人")
            # Prefer total_likes (from LikeMessage.total, real-time cumulative) over displayLong "点赞"
            print(f"  👍 点赞：{fmt_wan(likes) if likes > 0 else (likes_wan or '?')}")
            print(f"  🔥 最高在线：{peak_wan or fmt_wan(peak)}")
            print(f"  📊 平均在线：{fmt_wan(avg_viewers)}")
            print(f"  📈 新增关注：{self._get_new_follows()}")
            print(f"  🌟 新增粉丝团：{self._get_fan_club_joins()}人")
            # Prefer stream-specific light_badge events; show displayLong fallback if WS disconnected
            if self.light_badges > 0:
                print(f"  💡 点亮灯牌：{self.light_badges}个 (粉丝团灯牌)")
            else:
                badge_wan = self._try_get_wan('灯牌')
                if badge_wan:
                    print(f"  💡 点亮灯牌：{badge_wan}个 (频道全天)")
                else:
                    print(f"  💡 点亮灯牌：0个")
            print(f"  🎁 礼物事件：{gifts}")
            if gifts > 0 and self.gift_events:
                gift_summary = {}
                for g in self.gift_events:
                    name = g['gift']
                    if self._is_action_gift(name):
                        continue
                    gift_summary[name] = gift_summary.get(name, 0) + g['count']
                top = sorted(gift_summary.items(), key=lambda x: -x[1])[:3]
                if top:
                    print(f"  🎀 热门礼物：{' | '.join([f'{n}×{c}' for n,c in top])}")
            print(f"{'─'*60}\n")
            self._write_live_stats_json()

    def _write_live_stats_json(self, live=True):
        """Write current stats to a JSON file for the web dashboard.

        Uses atomic write (temp file + rename) to prevent the web server
        from reading a half-written file.
        """
        try:
            # ── Seed override (safe against repeated application) ──
            # Seed file persists across process restarts so we can recover
            # state after a crash.  Every assignment below is idempotent:
            #   • Cumulative counters use max() → never decrease
            #   • Baseline values only set when currently 0/unset → never overwrite
            #   • viewer_samples only appended once (tracked via _seed_samples_applied)
            _seed_path = os.path.join(os.path.dirname(__file__), 'seed_override.json')
            if os.path.exists(_seed_path):
                try:
                    with open(_seed_path, 'r') as _sf:
                        _seed = json.load(_sf)
                    # ── Baseline values: only set when not already present ──
                    if _seed.get('stream_start_time'):
                        seed_start = datetime.fromisoformat(_seed['stream_start_time'])
                        if self.stream_start_time is None or seed_start < self.stream_start_time:
                            self.stream_start_time = seed_start
                    # Baseline values: seed is the ground truth — always apply.
                    # _take_pre_snapshot runs on process restart and fetches the
                    # CURRENT follower count, NOT the pre-stream baseline.  The
                    # seed's value is the original pre-stream snapshot and must win.
                    if _seed.get('follower_before'):
                        self.follower_before = int(_seed['follower_before'])
                    if _seed.get('follower_after'):
                        self.follower_after = int(_seed['follower_after'])
                    if _seed.get('fan_club_start_count'):
                        self.fan_club_start_count = int(_seed['fan_club_start_count'])
                    # ── Cumulative counters: use max() so live events aren't erased ──
                    if _seed.get('peak_viewers'):
                        self.peak_viewers = max(self.peak_viewers, int(_seed['peak_viewers']))
                    if _seed.get('cumulative_views'):
                        self.cumulative_views = max(self.cumulative_views, int(_seed['cumulative_views']))
                    if _seed.get('total_likes'):
                        self.total_likes = max(self.total_likes, int(_seed['total_likes']))
                    if _seed.get('light_badges'):
                        self.light_badges = max(self.light_badges, int(_seed['light_badges']))
                    if _seed.get('fan_club_end_count'):
                        self.fan_club_end_count = max(self.fan_club_end_count, int(_seed['fan_club_end_count']))
                    # ── avg_override: only set once ──
                    if _seed.get('avg_override') and not hasattr(self, '_avg_override'):
                        self._avg_override = int(_seed['avg_override'])
                    # ── Viewer samples: only append once ──
                    if _seed.get('seed_viewer_samples'):
                        _seed_samples = _seed['seed_viewer_samples']
                        if isinstance(_seed_samples, list) and _seed_samples:
                            if not getattr(self, '_seed_samples_applied', False):
                                self.viewer_samples = list(_seed_samples) + self.viewer_samples
                                self._last_minute_sample = None
                                self._seed_samples_applied = True
                                logger.info(f"[SeedOverride] Seeded {len(_seed_samples)} viewer samples for gap period")
                    logger.info(f"[SeedOverride] Applied: start={self.stream_start_time}, fb={self.follower_before:,}, fa={self.follower_after:,}, peak={getattr(self,'peak_viewers',0)}, badges={self.light_badges}, fc_start={self.fan_club_start_count}, vsamples={len(self.viewer_samples)}")
                except Exception as _se:
                    logger.warning(f"[SeedOverride] Failed: {_se}")
            # ── End seed override ──────────────────────────────────────────
            if live:
                # Build gift summary: top 5 real gifts (exclude action gifts)
                gift_summary = {}
                for g in self.gift_events:
                    name = g['gift']
                    if self._is_action_gift(name):
                        continue
                    gift_summary[name] = gift_summary.get(name, 0) + g['count']
                top_gifts = sorted(gift_summary.items(), key=lambda x: -x[1])[:5]

                duration_seconds = 0
                if self.stream_start_time:
                    duration_seconds = int((datetime.now() - self.stream_start_time).total_seconds())

                data = {
                    "live": True,
                    "live_id": self.live_id,
                    "anchor_nickname": self.anchor_nickname,
                    "total_likes": self.total_likes,
                    "new_follows": self._get_new_follows(),
                    "follower_before": self.follower_before,
                    "follower_after": self.follower_after,
                    "fan_club_joins": self._get_fan_club_joins(),
                    "fan_club_delta": max(0, self.fan_club_end_count - self.fan_club_start_count) if self.fan_club_start_count > 0 else 0,
                    "fan_club_event_joins": self.fan_club_joins,
                    "fan_club_gift_joins": self.fan_club_gift_joins,
                    "light_badges": self.light_badges,
                    "current_viewers": self.current_viewers,
                    "peak_viewers": self.peak_viewers,
                    "average_viewers": sum(self.viewer_samples) // len(self.viewer_samples) if self.viewer_samples else 0,
                    "cumulative_views": self.cumulative_views,
                    "member_count": self.member_count,
                    "stream_start_time": self.stream_start_time.isoformat() if self.stream_start_time else None,
                    "stream_duration_seconds": duration_seconds,
                    "ws_connected": not self.ws_disconnected,
                    "gift_summary": top_gifts,
                    "last_update": datetime.now().isoformat(),
                }
            else:
                # Preserve all existing stats data during shutdown/offline.
                # This keeps the dashboard showing the last known state and
                # allows restart detection via last_update timestamp.
                duration_seconds = 0
                if self.stream_start_time:
                    duration_seconds = int((datetime.now() - self.stream_start_time).total_seconds())
                # Build gift summary from existing gift_events
                gift_summary = {}
                for g in self.gift_events:
                    name = g['gift']
                    if self._is_action_gift(name):
                        continue
                    gift_summary[name] = gift_summary.get(name, 0) + g['count']
                top_gifts = sorted(gift_summary.items(), key=lambda x: -x[1])[:5]
                data = {
                    "live": False,
                    "live_id": self.live_id,
                    "anchor_nickname": self.anchor_nickname,
                    "total_likes": self.total_likes,
                    "new_follows": self._get_new_follows(),
                    "follower_before": self.follower_before,
                    "follower_after": self.follower_after,
                    "fan_club_joins": self._get_fan_club_joins(),
                    "fan_club_delta": max(0, self.fan_club_end_count - self.fan_club_start_count) if self.fan_club_start_count > 0 else 0,
                    "fan_club_event_joins": self.fan_club_joins,
                    "fan_club_gift_joins": self.fan_club_gift_joins,
                    "light_badges": self.light_badges,
                    "current_viewers": self.current_viewers,
                    "peak_viewers": self.peak_viewers,
                    "average_viewers": sum(self.viewer_samples) // len(self.viewer_samples) if self.viewer_samples else 0,
                    "cumulative_views": self.cumulative_views,
                    "member_count": self.member_count,
                    "stream_start_time": self.stream_start_time.isoformat() if self.stream_start_time else None,
                    "stream_duration_seconds": duration_seconds,
                    "ws_connected": False,
                    "gift_summary": top_gifts,
                    "last_update": datetime.now().isoformat(),
                }

            tmp_path = STATS_FILE + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, STATS_FILE)
        except Exception as e:
            logger.debug(f"[StatsRecorder] Failed to write stats JSON: {e}")

    def _on_message(self, ws, message):
        try:
            frame = Live_pb2.PushFrame()
            frame.ParseFromString(message)
            origin_bytes = gzip.decompress(frame.payload)
            response = Live_pb2.LiveResponse()
            response.ParseFromString(origin_bytes)

            if response.needAck:
                s = Live_pb2.PushFrame()
                s.payloadType = "ack"
                s.payload = response.internalExt.encode('utf-8')
                s.logId = frame.logId
                ws.send(s.SerializeToString(), opcode=0x02)

            for item in response.messagesList:
                method = item.method
                payload = item.payload

                if method == 'WebcastLikeMessage':
                    msg = Live_pb2.LikeMessage()
                    msg.ParseFromString(payload)
                    self.total_likes = max(self.total_likes, msg.total)
                    if self.verbose:
                        logger.info(f"[LIKE] user={msg.user.nickname}, count={msg.count}, total={msg.total}, cumulative={self.total_likes}")

                elif method == 'WebcastSocialMessage':
                    msg = Live_pb2.SocialMessage()
                    msg.ParseFromString(payload)
                    if msg.action == 1:
                        self.new_follows += 1
                        if self.verbose:
                            logger.info(f"[FOLLOW] user={msg.user.nickname}, followCount={msg.followCount}, total={self.new_follows}")

                elif method == 'WebcastMemberMessage':
                    msg = Live_pb2.MemberMessage()
                    msg.ParseFromString(payload)
                    self.new_members += 1
                    # Track max memberCount from protobuf — this is the cumulative
                    # audience member count (room joins), used as "新增成员" in summaries.
                    if msg.memberCount > self.member_count:
                        self.member_count = msg.memberCount
                    if self.verbose:
                        logger.info(f"[MEMBER] user={msg.user.nickname}, memberCount={msg.memberCount}, total_events={self.new_members}")

                elif method == 'WebcastGiftMessage':
                    msg = Live_pb2.GiftMessage()
                    msg.ParseFromString(payload)
                    gift_name = msg.gift.name
                    # Each GiftMessage is ONE gift instance.  comboCount is the
                    # cumulative position in a combo streak (1st, 2nd, 3rd…),
                    # NOT the number of gifts in this message.  Using it as a
                    # multiplier would double-/triple-count combo gifts.
                    combo = 1
                    # ── Gift dedup (delta method) ──────────────────────────
                    # Douyin sends 2 messages per gift event (repeat_end=0/1)
                    # sharing the same group_id.  Dedup by accepting only when
                    # repeat_count increases vs the last seen value for the key
                    # (group_id, gift_name, user_id).
                    gid, rc, uid = parse_gift_dedup_key(payload)
                    if gid > 0 and not self._should_count_gift(gid, gift_name, uid, rc):
                        continue  # duplicate — skip this gift entirely
                    # ── End gift dedup ─────────────────────────────────────
                    self.gift_events.append({
                        'user': msg.user.nickname,
                        'gift': gift_name,
                        'count': combo,
                    })
                    # Gifts that represent badge light-up actions:
                    # "点点星光", "粉丝团灯牌", "闪烁星河"
                    if gift_name in ('点点星光', '粉丝团灯牌', '闪烁星河'):
                        self.light_badges += 1
                    # Gifts that represent fan club join actions:
                    # "入团卡" (join card) is a reliable fallback when
                    # FansclubMessage protobuf parsing misses events
                    # (e.g. during WebSocket reconnection gaps).
                    if '入团卡' in gift_name or '团卡' in gift_name:
                        self.fan_club_gift_joins += combo
                    if self.verbose:
                        join_tag = " [JOIN-CARD]" if ('入团卡' in gift_name or '团卡' in gift_name) else ""
                        logger.info(f"[GIFT{join_tag}] {msg.user.nickname} × {gift_name} x{combo}")

                elif method == 'WebcastRoomStatsMessage':
                    msg = Live_pb2.RoomStatsMessage()
                    msg.ParseFromString(payload)
                    self.display_long_history.append(msg.displayLong)
                    self.current_viewers = msg.displayValue
                    current_viewers = self.current_viewers
                    if current_viewers > self.peak_viewers:
                        self.peak_viewers = current_viewers
                        self.peak_viewer_time = datetime.now().strftime('%H:%M:%S')
                    # Sample viewer count at most once per minute for a
                    # time-weighted average (each sample ≈ 1 minute of broadcast).
                    now = datetime.now()
                    if (self._last_minute_sample is None or
                        (now - self._last_minute_sample).total_seconds() >= 60):
                        self.viewer_samples.append(current_viewers)
                        self._last_minute_sample = now

                    # RoomStatsMessage.total may be cumulative views when higher than displayValue
                    if msg.total > current_viewers:
                        self.cumulative_views = max(self.cumulative_views, msg.total)
                        if self.verbose:
                            logger.info(f"[STATS] 场观(累计) = {msg.total:,}")

                    parsed = parse_display_long(msg.displayLong)
                    if self.verbose:
                        logger.info(f"[ROOMSTATS] \"{msg.displayLong}\" | value={msg.displayValue:,} total={msg.total:,}")
                        if parsed:
                            for k, v in parsed.items():
                                logger.info(f"  parsed[{k}] = {v}")

                    for key, val in parsed.items():
                        if key == '点赞':
                            like_val = int(val * 10000) if val < 1e6 else int(val)
                            self.total_likes = max(self.total_likes, like_val)
                        elif key == '观看':
                            view_val = int(val * 10000) if val < 1e6 else int(val)
                            self.cumulative_views = max(self.cumulative_views, view_val)
                        # NOTE: displayLong "灯牌" is the CHANNEL'S ALL-DAY cumulative total.
                        # Do NOT max() it into the stream-specific light_badges counter.
                        # Store separately so the summary can use it as a fallback only.

                elif method == 'WebcastFansclubMessage':
                    # FansclubMessage: field 1=Common, field 2=type (1=upgrade, 2=join),
                    # field 3=content ("恭喜 xxx 成为粉丝团第{N}名成员"), field 4=User.
                    # Use parse_fansclub_msg() to extract all fields + total_members from content.
                    m = parse_fansclub_msg(payload)
                    if m['type'] == 2:
                        self.fan_club_joins += 1
                    elif m['type'] == 1:
                        pass  # upgrade event — not a join, but still has total_members
                    # NOTE: light_badges are counted EXCLUSIVELY from gifts:
                    #       点点星光, 粉丝团灯牌, 闪烁星河

                    # Track total fan club member count range: start (first seen) → end (latest)
                    if m['total_members'] > 0:
                        if self.fan_club_start_count == 0:
                            self.fan_club_start_count = m['total_members']
                        self.fan_club_end_count = m['total_members']
                    elif self.verbose:
                        # Content parsing failed — log the raw content for debugging
                        logger.warning(
                            f"[FANSCLUB] Could not extract total_members from content: "
                            f"type={m['type']} content={m['content']!r}"
                        )

                    if self.verbose:
                        fc_delta = 0
                        if self.fan_club_start_count > 0 and self.fan_club_end_count > 0:
                            fc_delta = self.fan_club_end_count - self.fan_club_start_count
                        logger.info(
                            f"[FANSCLUB] type={m['type']} content={m['content']} "
                            f"total_members={m['total_members']} "
                            f"start={self.fan_club_start_count} end={self.fan_club_end_count} "
                            f"delta={fc_delta} joins_events={self.fan_club_joins} "
                            f"gift_joins={self.fan_club_gift_joins}"
                        )

                elif method in (
                    'WebcastLightMessage', 'WebcastFanBadgeMessage',
                    'WebcastAudienceMessage',
                ):
                    # These are badge-related events but we count light badges
                    # exclusively from the three gifts: 点点星光, 粉丝团灯牌, 闪烁星河
                    if self.verbose:
                        logger.info(f"[BADGE/{method}] (not counted toward light_badges)")

                elif method == 'WebcastRoomUserSeqMessage':
                    # Parse RoomUserSeqMessage protobuf to extract totalPvForAnchor (field 11, string)
                    # This gives the actual cumulative view count, not just concurrent viewers.
                    try:
                        pv = parse_room_user_seq_pv(payload)
                        if pv > self.cumulative_views:
                            self.cumulative_views = pv
                        if self.verbose and pv > 0:
                            logger.info(f"[VIEWS] 场观(累计) = {pv:,}")
                    except Exception as e:
                        if self.verbose:
                            logger.debug(f"[VIEWS] parse failed: {e}")

        except Exception:
            pass

    def _on_error(self, ws, error):
        logger.error(f"[StatsRecorder] WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        if self._stop_event.is_set():
            # Intentional close (stop() was called) - not a mid-stream disconnect
            return
        logger.info(f"[StatsRecorder] WebSocket closed unexpectedly (status={close_status_code})")
        # Mark as disconnected so run_once() can trigger recovery on next iteration
        self.ws_disconnected = True
        self.ws_disconnect_time = datetime.now()
        logger.warning("[StatsRecorder] Unexpected WS disconnect. Main loop will attempt recovery.")

    def _generate_summary(self):
        print(f"\n{'='*60}")
        print(f"  📊 直播总结 / Stream Summary")
        print(f"{'='*60}\n")

        if self.verbose:
            print(f"  [Raw counters]")
            print(f"    total_likes       = {self.total_likes:,}")
            print(f"    cumulative_views  = {self.cumulative_views:,}")
            print(f"    new_follows       = {self.new_follows}")
            print(f"    follower_before   = {self.follower_before:,}")
            print(f"    follower_after    = {self.follower_after:,}")
            print(f"    new_members       = {self.new_members}")
            print(f"    member_count      = {self.member_count:,}")
            print(f"    light_badges      = {self.light_badges}")
            print(f"    fan_club_joins    = {self._get_fan_club_joins()} (events={self.fan_club_joins}, start={self.fan_club_start_count}, end={self.fan_club_end_count})")
            print(f"    peak_viewers      = {self.peak_viewers:,}")
            print(f"    viewer_samples    = {len(self.viewer_samples)}")
            print(f"    gift_events        = {len(self.gift_events)}")
            print()

        avg_viewers = 0
        if self.viewer_samples:
            avg_viewers = sum(self.viewer_samples) // len(self.viewer_samples)

        duration_str = ""
        if self.stream_start_time and self.stream_end_time:
            delta = self.stream_end_time - self.stream_start_time
            mins, secs = divmod(int(delta.total_seconds()), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                duration_str = f"{hours}小时{mins}分钟"
            else:
                duration_str = f"{mins}分钟{secs}秒"

        anchor = self.anchor_nickname or f"直播 {self.live_id}"
        print(f"📺 主播：【{anchor}】直播结束")

        views_wan = self._try_get_wan('观看')
        if views_wan:
            print(f"👀 本场直播累计观看：{views_wan}人")
        elif self.cumulative_views:
            print(f"👀 本场直播累计观看：{fmt_wan(self.cumulative_views)}人")
        else:
            print(f"👀 本场直播累计观看：--")

        # Prefer total_likes (from LikeMessage.total, stream cumulative) over displayLong "点赞"
        if self.total_likes > 0:
            print(f"👍 本场直播点赞数据：{fmt_wan(self.total_likes)}")
        else:
            likes_wan = self._try_get_wan('点赞')
            if likes_wan:
                print(f"👍 本场直播点赞数据：{likes_wan}")
            else:
                print(f"👍 本场直播点赞数据：--")

        print(f"📈 本场新增粉丝：{fmt_wan(self._get_new_follows())}人")
        print(f"（粉丝：{fmt_wan(self.follower_before)} ➡️ {fmt_wan(self.follower_after)}）")

        peak_wan = self._try_get_wan('最高在线')
        if peak_wan:
            print(f"🔥 最高在线：{peak_wan}人")
        elif self.peak_viewers:
            print(f"🔥 最高在线：{fmt_wan(self.peak_viewers)}人")
        else:
            print(f"🔥 最高在线：--")

        print(f"📊 平均在线：{fmt_wan(avg_viewers)}人")

        # Fan club joins: prefer delta from content total-members (end - start);
        # fallback to type=2 event count; last resort: displayLong "粉丝团".
        joins = self._get_fan_club_joins()
        if joins > 0:
            print(f"🌟 今日新增粉丝团：{joins}人")
        else:
            members_str = self._wan_to_raw_str(self._try_get_wan('粉丝团'))
            if members_str != "0":
                print(f"🌟 今日新增粉丝团：{members_str}人 (频道全天)")
            else:
                print(f"🌟 今日新增粉丝团：--")

        # Light badges: prefer stream-specific event count over all-day displayLong.
        if self.light_badges > 0:
            print(f"💡 今日点亮灯牌：{self.light_badges}人")
        else:
            badge_str = self._wan_to_raw_str(self._try_get_wan('灯牌'))
            if badge_str != "0":
                print(f"💡 今日点亮灯牌：{badge_str}人 (频道全天)")
            else:
                print(f"💡 今日点亮灯牌：--")

        if duration_str:
            print(f"⏱ 直播时长：{duration_str}")

        if self.gift_events:
            gift_summary = {}
            for g in self.gift_events:
                name = g['gift']
                if self._is_action_gift(name):
                    continue
                gift_summary[name] = gift_summary.get(name, 0) + g['count']
            top_gifts = sorted(gift_summary.items(), key=lambda x: -x[1])[:5]
            if top_gifts:
                print(f"\n🎁 热门礼物 TOP{len(top_gifts)}:")
                for name, cnt in top_gifts:
                    print(f"   · {name} × {cnt}")

        print(f"\n{'='*60}")
        print(f"  🏁 总结完毕")
        print(f"{'='*60}\n")

    # Gifts that represent system actions, not viewer-chosen gifts.
    # These should be excluded from the popular-gifts ranking because
    # they are already reflected in the stats cards (light_badges,
    # fan_club_joins).  Showing them as "gifts" double-counts and
    # confuses users.
    _ACTION_GIFT_PATTERNS = (
        '粉丝团灯牌',   # badge light-up action
        '点点星光',     # badge light-up action
        '闪烁星河',     # badge light-up action
        '入团卡',       # fan-club join card
        '点亮粉丝团',   # fan-club light/join action
    )

    @staticmethod
    def _is_action_gift(name: str) -> bool:
        """Return True if *name* is a system-action gift, not a real gift."""
        for pattern in LiveStatsRecorder._ACTION_GIFT_PATTERNS:
            if pattern in name:
                return True
        return False

    def _get_new_follows(self) -> int:
        """Best estimate of new followers during this stream.

        Primary:  delta = follower_after - follower_before (API snapshots).
                  Captures NET follower change, including followers gained
                  from outside the live room (profile, recommendations, etc.).
        Fallback: new_follows — direct count of WebcastSocialMessage action=1
                  events received via WebSocket.  Only used when delta = 0
                  (i.e. follower_after hasn't been refreshed yet).
        """
        delta = self.follower_after - self.follower_before
        if delta > 0:
            return delta
        # delta <= 0 — follower_after not yet refreshed (or unfollows/bot cleanup).
        # Fall back to the WebSocket event counter.
        return max(0, self.new_follows)

    def _get_fan_club_joins(self) -> int:
        """Best estimate of fan club joins during this stream.

        Uses three sources, taking the maximum since each can miss some joins:

        1. fan_club_joins — direct count of type=2 FansclubMessage events
        2. fan_club_gift_joins — 入团卡 gift count (fallback when protobuf parsing misses)
        3. content-based delta (fan_club_end_count - fan_club_start_count)
           Most reliable when start_count was seeded from the user profile API
           at stream start.  Survives process restarts via seed_override.json.
        """
        delta = 0
        if self.fan_club_start_count > 0 and self.fan_club_end_count > 0:
            delta = max(0, self.fan_club_end_count - self.fan_club_start_count)
        return max(self.fan_club_joins, self.fan_club_gift_joins, delta)

    def _should_count_gift(self, group_id: int, gift_name: str, user_id: int,
                           repeat_count: int) -> bool:
        """Delta-method gift dedup.

        Douyin sends 2 WebcastGiftMessage per gift event (repeat_end=0 then
        repeat_end=1) sharing the same group_id.  Combo gifts also send
        duplicate messages for each combo increment.

        Dedup key = (group_id, gift_name, user_id).  Only accept when
        repeat_count increases (delta > 0).  This correctly handles:
          - Single-shot gifts (repeat_count=1, only one message or two dupes):
              first message delta=1 → accept, second delta=0 → skip.
          - Combo gifts (repeat_count increments 1,2,3… with dupes at each step):
              each genuine increment has delta=1 → accept; dupes have delta=0 → skip.
        """
        key = (group_id, gift_name, user_id)
        prev = self._gift_dedup.get(key, 0)
        if repeat_count > prev:
            self._gift_dedup[key] = repeat_count
            return True
        return False

    def _cleanup_gift_dedup(self):
        """Evict stale entries from the dedup dict to bound memory usage.

        Douyin never reuses group_id within a stream, so entries whose
        repeat_count hasn't changed in >30 minutes are safe to remove.
        We use a two-phase approach:
          1. Every ~10 mins, mark all current entries as "candidate for eviction"
             by storing (repeat_count, timestamp).
          2. On the next cleanup cycle, entries whose repeat_count hasn't
             changed since the last cycle are evicted.

        This is safe because the full-dict clear (previous approach) would
        cause double-counting if messages were replayed after the clear
        (e.g. during WebSocket reconnection).
        """
        now = datetime.now()
        if (now - self._gift_dedup_last_cleanup).total_seconds() < 600:
            return  # only run every 10 minutes

        # Build new dict keeping only entries that changed since last cycle
        if not hasattr(self, '_gift_dedup_snapshot'):
            self._gift_dedup_snapshot = {}  # key → repeat_count at last cleanup

        new_dedup = {}
        evicted = 0
        # Snapshot the items to avoid RuntimeError if _should_count_gift()
        # (called from the WebSocket message thread) mutates the dict during iteration.
        for key, rc in list(self._gift_dedup.items()):
            prev_rc = self._gift_dedup_snapshot.get(key, -1)
            if rc != prev_rc:
                # repeat_count changed → gift was active recently, keep it
                new_dedup[key] = rc
            else:
                evicted += 1

        # Take a new snapshot of what remains
        self._gift_dedup = new_dedup
        self._gift_dedup_snapshot = dict(new_dedup)
        self._gift_dedup_last_cleanup = now

        if evicted > 0:
            logger.debug(f"[Dedup] Evicted {evicted} stale entries, "
                         f"{len(new_dedup)} active entries remain")

    def _try_get_wan(self, key: str):
        """Try to extract a known field from displayLong history.
        Returns formatted string like '97.2万' or '8013'.
        Correctly handles values with and without 万 suffix in the original text."""
        pattern = DISPLAY_PATTERNS.get(key)
        if not pattern:
            return None
        for dl in reversed(self.display_long_history):
            m = re.search(pattern, dl, re.IGNORECASE)
            if m:
                val = float(m.group(1))
                has_wan = '万' in m.group(0)  # check if original string had 万
                if has_wan:
                    if val == int(val):
                        s = f"{val:.1f}万"
                        return re.sub(r'\.0万', '万', s)
                    else:
                        s = f"{val:.2f}万"
                        s = re.sub(r'\.(\d*?)0+万', r'.\1万', s)
                        return s
                else:
                    return str(int(val))
        return None

    @staticmethod
    def _wan_to_raw_str(wan_val):
        """Convert a 万-suffixed value to a raw integer string.

        '5.2万' → '52000', '8013' → '8013', None → '0'
        Used as a safe conversion for _try_get_wan fallback values
        so templates don't show misleading decimals like '5.2'.
        """
        if not wan_val:
            return "0"
        if '万' in wan_val:
            return str(int(float(wan_val.replace('万', '')) * 10000))
        return wan_val


# ======================================================================
# Douyin live-status checker (direct HTTP)
# ======================================================================

class DouyinLiveChecker:
    STATUS_LIVE = "2"
    STATUS_OFFLINE = "4"

    HEADERS = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en;q=0.7,ja;q=0.6",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "priority": "u=0, i",
        "referer": "https://live.douyin.com/?from_nav=1",
        "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    }

    def __init__(self, live_id: str, cookie_str: str = ""):
        self.live_id = live_id
        self.cookie = self._parse_cookie(cookie_str) if cookie_str else {}

    @staticmethod
    def _parse_cookie(cookie_str: str) -> dict:
        result = {}
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    def check_status(self) -> dict:
        """Check Douyin stream status with retry logic for timeouts."""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            url = f"https://live.douyin.com/{self.live_id}"
            try:
                resp = requests.get(url, headers=self.HEADERS, cookies=self.cookie, verify=False, timeout=15)

                # Detect auth failure before raise_for_status
                # (redirect to login means cookies are dead)
                if "passport" in resp.url or "sso.douyin.com" in resp.url:
                    logger.warning(
                        f"[Health] Auth redirect detected: {resp.url[:80]}"
                    )
                    return {"room_status": "auth_error"}

                resp.raise_for_status()
                ttwid = ""
                if 'ttwid' in resp.cookies:
                    ttwid = resp.cookies['ttwid']
                elif 'ttwid' in resp.cookies.get_dict():
                    ttwid = resp.cookies.get_dict()['ttwid']

                soup = BeautifulSoup(resp.text, 'html.parser')
                scripts = soup.select('script[nonce]')

                for script in scripts:
                    if script.string is None or 'roomId' not in script.string:
                        continue
                    try:
                        user_id = re.findall(r'\\"user_unique_id\\":\\"(\d+)\\"', script.string)[0]
                        room_id = re.findall(r'\\"roomId\\":\\"(\d+)\\"', script.string)[0]
                        unique_id = re.findall(r'\\"user_unique_id\\":\\"(\d+)\\"', script.string)[0]
                        room_info = re.findall(
                            r'\\"roomInfo\\":\{\\"room\\":\{\\"id_str\\":\\".*?\\",\\"status\\":(.*?),\\"status_str\\":\\".*?\\",\\"title\\":\\"(.*?)\\"',
                            script.string,
                        )
                        anchor_id = re.findall(r'\\"anchor\\":\{\\"id_str\\":\\"(\d+)\\"', script.string)[0]
                        sec_uid = re.findall(r'\\"sec_uid\\":\\"(.*?)\\"', script.string)[0]
                        # Extract anchor nickname from page data (fallback when protobuf modules unavailable)
                        # Must look for nickname inside the anchor object, not the logged-in user's nickname
                        anchor_nickname = ""
                        anchor_nickname_match = re.search(r'\\"anchor\\":\{.*?\\"nickname\\":\\"(.*?)\\"', script.string)
                        if anchor_nickname_match:
                            anchor_nickname = anchor_nickname_match.group(1)

                        if room_info:
                            room_status = room_info[0][0]
                            room_title = room_info[0][1]
                        else:
                            room_status = self.STATUS_OFFLINE
                            room_title = ""

                        result = {
                            "room_id": room_id,
                            "user_id": user_id,
                            "user_unique_id": unique_id,
                            "anchor_id": anchor_id,
                            "sec_uid": sec_uid,
                            "ttwid": ttwid,
                            "room_status": room_status,
                            "room_title": room_title,
                            "anchor_nickname": anchor_nickname,
                        }
                        # Cache last known status so timeout fallback can preserve it
                        self._last_status = room_status
                        logger.debug(f"Douyin check result: status={room_status}, title={room_title}")
                        return result
                    except Exception:
                        continue

                logger.warning("Could not find room info in page HTML")
                return {"room_status": self.STATUS_OFFLINE}

            except requests.Timeout as e:
                # Retry on timeout errors (transient network issues)
                if attempt < max_retries - 1:
                    logger.warning(f"Timeout checking stream (attempt {attempt+1}/{max_retries}), retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"HTTP timeout after {max_retries} attempts: {e}")
                    # On final timeout, return last known status to avoid false offline detection
                    last = getattr(self, '_last_status', None)
                    if last:
                        logger.info(f"Keeping previous status ({last}) to avoid false offline detection")
                        return {"room_status": last}
                    return {"room_status": self.STATUS_OFFLINE}
            except requests.RequestException as e:
                # For other HTTP errors, fail immediately (no retry)
                logger.error(f"HTTP error checking Douyin stream: {e}")
                return {"room_status": self.STATUS_OFFLINE}
            except Exception as e:
                logger.error(f"Unexpected error checking Douyin stream: {e}")
                return {"room_status": self.STATUS_OFFLINE}


# ======================================================================
# Weibo posting (direct HTTP)
# ======================================================================

class WeiboPoster:
    WEB_HOST = "https://weibo.com"
    POST_URL = f"{WEB_HOST}/ajax/statuses/update"

    def __init__(self, web_cookie: str):
        self.web_cookie = web_cookie

    def post_tweet(self, content: str) -> bool:
        # Remove emoji characters before posting to Weibo
        content = strip_emoji(content)
        cookies = self._cookies()
        xsrf_token = self._extract_xsrf()
        logger.info(f"Posting to Weibo: {content[:80]}...")

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
                logger.info(f"Weibo posted successfully!")
                return True
            else:
                logger.error(f"Weibo API returned error: {result}")
                return False
        except Exception as e:
            logger.error(f"Failed to post Weibo: {e}")
            return False

    def _cookies(self) -> dict:
        result = {}
        for part in self.web_cookie.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    def _extract_xsrf(self) -> str:
        cookies = self._cookies()
        return cookies.get("XSRF-TOKEN", cookies.get("xsrf-token", ""))


# ======================================================================
# Stream Monitor (main monitoring class)
# ======================================================================

def _stats_json_indicates_restart(live_id: str, max_age_seconds: int = 120) -> bool:
    """Check if live_stats.json indicates we're restarting onto a stream that was
    already being monitored.

    Uses last_update timestamp only (NOT the 'live' flag) because stop() writes
    ``{"live": false}`` during shutdown, overwriting the previous live state.
    A recent last_update means a process was recently monitoring this stream.
    """
    try:
        if not os.path.exists(STATS_FILE):
            return False
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            stats = json.load(f)
        last_update = stats.get('last_update', '')
        if not last_update:
            return False
        last_dt = datetime.fromisoformat(last_update)
        if (datetime.now() - last_dt).total_seconds() > max_age_seconds:
            return False
        # If live_id is present and doesn't match, it's a different stream
        json_live_id = stats.get('live_id', '')
        if json_live_id and json_live_id != live_id:
            return False
        return True
    except Exception:
        return False


class StreamMonitor:
    def __init__(self, live_id: str, dy_cookie_str: str, weibo_cookie: str,
                 live_template: str = None,
                 check_interval: int = 60, live_title: str = "",
                 record_stats: bool = False, verbose: bool = False,
                 offline_summary_template: str = None,
                 dry_run: bool = False):
        self.live_id = live_id
        self.check_interval = check_interval
        self.live_title = live_title
        self.dy_cookie_str = dy_cookie_str
        self.current_status = None
        self.record_stats = record_stats
        self.verbose = verbose
        self._dry_run = dry_run

        self._summary_posted = False
        self._live_posted = False  # guard against duplicate stream-start Weibo
        self.live_template = live_template or "【开播提醒】主播已开播！直播主题：{title}"
        # Replace literal \n with actual newlines so users can write single-line
        # templates in .env like: {name} 直播结束\n场观：{views}
        if offline_summary_template:
            offline_summary_template = offline_summary_template.replace('\\n', '\n')
        self.offline_summary_template = offline_summary_template or \
            "{name} 直播结束\n场观：{views}\n点赞：{likes}\n最高在线：{peak}\n平均在线：{avg}\n新增粉丝：{followers}\n新增粉丝团：{members}\n点亮灯牌：{badges}\n热门礼物：{gifts}"

        self.checker = DouyinLiveChecker(live_id, dy_cookie_str)
        self.poster = WeiboPoster(weibo_cookie)

        self.stats_recorder = None
        self.log_file = os.path.join(os.path.dirname(__file__), 'notification_log.json')

        # FIX: Store anchor nickname from HTTP page data as fallback
        # Stats recorder (WebSocket/API) may fail to fetch nickname,
        # but HTTP page HTML parsing always gets it for live_start.
        self._anchor_nickname = ""

        # Track consecutive HTTP offline detections to handle the case where
        # Douyin WebSocket stays connected indefinitely after stream ends.
        # After CONSECUTIVE_OFFLINE_LIMIT consecutive detections, force handle_offline.
        self._consecutive_offline_count = 0
        self.CONSECUTIVE_OFFLINE_LIMIT = 2

        # ── Cookie refresh system ─────────────────────────────────────
        self.cookie_manager = CookieManager()
        self.notifier = TelegramNotifier()
        self._cookie_refreshing = False  # guard against concurrent refreshes
        self._last_auth_error_time = None

        # Bootstrap cookies.json from .env on first run,
        # then use cookies.json as the authority from now on.
        self.cookie_manager.bootstrap_from_env()
        cookie_data = self.cookie_manager.load()
        # Use cookies.json as cookie source (overrides .env)
        if cookie_data.get("cookie_str"):
            self.dy_cookie_str = cookie_data["cookie_str"]
            # Re-parse into checker
            self.checker.cookie = DouyinLiveChecker._parse_cookie(
                self.dy_cookie_str
            )

        # Validate templates at startup
        self._validate_template(self.live_template, self._get_live_template_keys())
        self._validate_template(self.offline_summary_template, self._get_offline_template_keys())

        logger.info("StreamMonitor initialised: live.douyin.com/{}", live_id)

    def reload_cookies(self):
        """Hot-reload cookies from cookies.json into all running components."""
        data = self.cookie_manager.load()
        new_cookie_str = data.get("cookie_str", "")
        if not new_cookie_str:
            logger.warning(
                "[CookieReload] cookies.json has empty cookie_str, "
                "skipping reload"
            )
            return False
        self.dy_cookie_str = new_cookie_str
        self.checker.cookie = DouyinLiveChecker._parse_cookie(new_cookie_str)
        if self.stats_recorder and self.stats_recorder.is_running():
            self.stats_recorder.cookie_str = new_cookie_str
        logger.info(
            "[CookieReload] Hot-reloaded cookies into checker and recorder"
        )
        return True

    def _trigger_cookie_refresh(self):
        """Emergency cookie refresh — called when auth failure detected.

        Runs in a background thread to avoid blocking the main loop.
        """
        if self._cookie_refreshing:
            logger.debug(
                "[CookieRefresh] Refresh already in progress, skipping"
            )
            return
        self._cookie_refreshing = True
        try:
            from cookie_refresher import CookieRefresher

            refresher = CookieRefresher(self.cookie_manager)
            success = asyncio.run(refresher.refresh())
            if success:
                self.reload_cookies()
                self.cookie_manager.mark_healthy()
                self.notifier.send(
                    "✅ Emergency cookie refresh succeeded",
                    state="ok",
                )
            else:
                self.cookie_manager.mark_unhealthy()
                self.notifier.send(
                    "\U0001f534 CRITICAL: Emergency cookie refresh "
                    "FAILED — monitor may be blind. "
                    "Manual re-login required.",
                    state="dead",
                )
        except Exception as e:
            logger.error(f"[CookieRefresh] Emergency refresh error: {e}")
            self.notifier.send(
                f"❌ Cookie refresh crashed: {e}",
                state=None,  # force-send on crash
            )
        finally:
            self._cookie_refreshing = False

    def _check_cookie_health(self, room_info):
        """Check if the HTTP response indicates an auth failure.

        Returns True if cookies appear healthy, False if auth error detected.
        """
        status = room_info.get("room_status", "")
        if status == "auth_error":
            logger.warning(
                "[Health] Auth failure detected — "
                "triggering emergency refresh"
            )
            self.cookie_manager.mark_unhealthy()
            threading.Thread(
                target=self._trigger_cookie_refresh, daemon=True
            ).start()
            return False
        return True

    @staticmethod
    def _get_live_template_keys():
        return {'name', 'title', 'live_id', 'room_id'}

    @staticmethod
    def _get_offline_template_keys():
        return {'name', 'views', 'likes', 'peak', 'avg', 'followers', 'members', 'badges', 'gifts', 'duration'}

    @staticmethod
    def _build_offline_summary_values(r, anchor_name: str, end_time):
        """Compute all offline summary values from a LiveStatsRecorder.

        Returns a dict with keys matching the offline summary template:
        name, views, likes, peak, avg, followers, members, badges, gifts, duration.
        Callers format the result into a template or print it.
        """
        # ── views ──
        views_val = r.cumulative_views if r.cumulative_views > 0 else 0
        if views_val == 0 and r.viewer_samples:
            views_val = r.peak_viewers
        pv = (r._try_get_wan('观看')
              or (fmt_wan(views_val) if views_val > 0 else "")
              or (fmt_wan(r.peak_viewers) if r.peak_viewers > 0 else ""))

        # ── likes ──
        likes = (fmt_wan(r.total_likes) if r.total_likes else
                 r._try_get_wan('点赞') or "")

        # ── peak ──
        peak = (r._try_get_wan('最高在线')
                or (fmt_wan(r.peak_viewers) if r.peak_viewers > 0 else "")
                or (fmt_wan(max(r.viewer_samples)) if r.viewer_samples else ""))

        # ── avg ──
        _avg_ov = getattr(r, '_avg_override', 0)
        if _avg_ov > 0:
            avg = fmt_wan(_avg_ov)
        elif r.viewer_samples:
            avg = fmt_wan(sum(r.viewer_samples) // len(r.viewer_samples))
        elif r.peak_viewers > 0:
            avg = fmt_wan(r.peak_viewers)
        else:
            avg = ""

        # ── followers ──
        followers_str = str(r._get_new_follows())

        # ── fan club joins ──
        joins = r._get_fan_club_joins()
        if joins > 0:
            members_str = str(joins)
        else:
            members_str = r._wan_to_raw_str(r._try_get_wan('粉丝团'))

        # ── light badges ──
        if r.light_badges > 0:
            badges_str = str(r.light_badges)
        else:
            badges_str = r._wan_to_raw_str(r._try_get_wan('灯牌'))

        # ── duration ──
        duration_str = ""
        if r.stream_start_time:
            delta_dur = end_time - r.stream_start_time
            mins, secs = divmod(int(delta_dur.total_seconds()), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                duration_str = f"{hours}小时{mins}分钟"
            else:
                duration_str = f"{mins}分钟{secs}秒"
        else:
            duration_str = "--"

        # ── gifts ──
        gifts_str = ""
        if r.gift_events:
            gift_summary = {}
            for g in r.gift_events:
                name = g['gift']
                if LiveStatsRecorder._is_action_gift(name):
                    continue
                gift_summary[name] = gift_summary.get(name, 0) + g['count']
            top = sorted(gift_summary.items(), key=lambda x: -x[1])[:3]
            if top:
                gifts_str = " | ".join([f"{n}×{c}" for n, c in top])

        return {
            'name': anchor_name or "",
            'views': pv,
            'likes': likes,
            'peak': peak,
            'avg': avg,
            'followers': followers_str,
            'members': members_str,
            'badges': badges_str,
            'gifts': gifts_str,
            'duration': duration_str,
        }

    def _validate_template(self, template: str, valid_keys: set):
        """Check that all placeholders in the template are valid known keys."""
        import re as _re
        found = set(_re.findall(r'\{(\w+)\}', template))
        unknown = found - valid_keys
        if unknown:
            logger.warning(
                f"Template contains unknown placeholder(s): {', '.join(sorted(unknown))}. "
                f"Valid placeholders: {', '.join(sorted(valid_keys))}"
            )

    def log_notification(self, event_type: str, content: str, success: bool):
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "live_id": self.live_id,
            "event": event_type,
            "content": content,
            "success": success,
        }
        try:
            logs = []
            if os.path.exists(self.log_file):
                with open(self.log_file, 'r', encoding='utf-8') as f:
                    logs = json.load(f)
            logs.append(log_entry)
            with open(self.log_file, 'w', encoding='utf-8') as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error writing notification log: {e}")

    def handle_live(self, room_info: dict):
        title = room_info.get('room_title', self.live_title or '')

        # FIX: Store anchor nickname from HTTP page data as fallback for handle_offline
        http_anchor = room_info.get('anchor_nickname', '')
        if http_anchor:
            self._anchor_nickname = http_anchor

        # Start the stats recorder first so it can fetch the anchor nickname
        if HAS_LIVE_DETAILS:
            logger.info("Starting live stats recorder (WebSocket)...")
            self.stats_recorder = LiveStatsRecorder(
                self.live_id, self.dy_cookie_str, verbose=self.verbose
            )
            self.stats_recorder.start_background()
            # Give the recorder a moment to fetch anchor info from the API
            time.sleep(2)

        # Use anchor nickname from recorder if available, otherwise from HTML page fallback
        anchor_name = ""
        if self.stats_recorder and self.stats_recorder.anchor_nickname:
            anchor_name = self.stats_recorder.anchor_nickname
        if not anchor_name:
            anchor_name = room_info.get('anchor_nickname', "")
        # Persist for handle_offline in case recorder is gone by then
        if anchor_name:
            self._anchor_nickname = anchor_name

        # Only post "stream started" once per stream.  WS reconnections
        # happen inside LiveStatsRecorder and don't trigger handle_live,
        # but guard anyway in case HTTP briefly flaps OFFLINE→LIVE.
        if self._live_posted:
            logger.info("Stream is LIVE but start notification already sent — skipping")
            return
        self._live_posted = True

        try:
            content = self.live_template.format(
                name=anchor_name,
                title=title,
                live_id=self.live_id,
                room_id=room_info.get('room_id', ''),
            )
        except KeyError as e:
            logger.error(f"Live template format error: missing key {e}. Template: {self.live_template!r}")
            content = f"【开播提醒】主播已开播！ live.douyin.com/{self.live_id}"

        logger.info("Stream is LIVE → posting notification")
        if self._dry_run:
            logger.info(f"[DRY-RUN] Would post live notification:\n{content}")
            success = True
        else:
            success = self.poster.post_tweet(content)
        self.log_notification("live_start", content, success)

    def handle_offline(self, room_info: dict = None):
        # Prevent double-posting if called multiple times (e.g. from both _on_close and run_once)
        if self._summary_posted:
            logger.debug("Offline summary already posted, skipping duplicate.")
            return
        self._summary_posted = True

        # ── Post-snapshot (must succeed before Weibo is sent) ─────────
        # The follower_after value drives the "new follows" delta in the
        # offline summary.  If we post before refreshing it, the Weibo
        # shows a stale/near-zero follower increase.  We retry with
        # increasing backoff until it succeeds.
        post_snapshot_ok = False
        if self.stats_recorder and self.stats_recorder.is_running():
            logger.info("Stopping stats recorder and generating summary...")
            self.stats_recorder.stream_end_time = datetime.now()

            from builder.auth import DouyinAuth  # type: ignore[import-untyped]
            auth = DouyinAuth()
            auth.perepare_auth(self.dy_cookie_str, "", "")
            sec_uid = (room_info or {}).get('sec_uid', '')

            if not sec_uid:
                logger.error(
                    "No sec_uid available — SKIPPING Weibo post."
                )
                self.log_notification(
                    "live_end_skipped_no_sec_uid",
                    "sec_uid missing from room_info",
                    False,
                )
                self.stats_recorder.stop()
                return

            # Fetch follower count directly via _get_follower.
            # Retry up to 3 times with backoff in case the API is slow.
            for attempt in range(3):
                if attempt > 0:
                    wait = 10 * attempt  # 10s, 20s
                    logger.info(
                        f"Waiting {wait}s before follower retry "
                        f"{attempt + 1}/3..."
                    )
                    time.sleep(wait)
                fc = self.stats_recorder._get_follower(auth, sec_uid)
                if fc > 0:
                    self.stats_recorder.follower_after = fc
                    post_snapshot_ok = True
                    logger.info(
                        f"Follower fetch succeeded: "
                        f"follower_after={fc:,}"
                    )
                    break
                else:
                    logger.warning(
                        f"Follower fetch attempt {attempt + 1}/3 returned 0 "
                        f"— retrying..."
                    )

            if not post_snapshot_ok:
                logger.error(
                    "All 3 follower fetch attempts returned 0. "
                    "SKIPPING Weibo post — follower delta would be unreliable."
                )
                self.log_notification(
                    "live_end_skipped_no_snapshot",
                    f"follower_after still 0 after 3 attempts",
                    False,
                )
                # Still stop the recorder so it doesn't linger
                self.stats_recorder.stop()
                return

        # Build summary text for Weibo using the shared computation.
        if self.stats_recorder is not None:
            r = self.stats_recorder
            now = datetime.now()
            # Try to also get anchor name from HTTP page as fallback
            if not r.anchor_nickname:
                try:
                    http_anchor = room_info.get('anchor_nickname', '') if room_info else ''
                    if not http_anchor:
                        checker = DouyinLiveChecker(self.live_id, self.dy_cookie_str)
                        ri = checker.check_status()
                        http_anchor = ri.get('anchor_nickname', '')
                    r.anchor_nickname = http_anchor or r.anchor_nickname
                except Exception:
                    pass
            anchor_name = r.anchor_nickname or ""

            vals = self._build_offline_summary_values(r, anchor_name, now)

            try:
                content = self.offline_summary_template.format(
                    name=vals['name'],
                    views=vals['views'] or "--",
                    likes=vals['likes'] or "--",
                    peak=vals['peak'] or "--",
                    avg=vals['avg'] or "--",
                    followers=vals['followers'],
                    members=vals['members'],
                    badges=vals['badges'],
                    gifts=vals['gifts'] if vals['gifts'] else "--",
                    duration=vals['duration'] or "--",
                )
            except KeyError as e:
                logger.error(f"Template format error: missing key {e}")
                content = f"直播结束 live.douyin.com/{self.live_id}"
            
            # Append data recovery status note only if HTTP fallback was used (WS reconnection
            # preserves full data so no warning needed; normal stream-end WS close is not a fault)
            if r.ws_recovery_attempted:
                if r.http_cumulative_recovery:
                    content += "\n\n⚠️ 注意：直播期间WebSocket连接中断，已通过HTTP恢复部分数据。peak/avg数据可能不完整。"
                    logger.warning("[Offline Summary] WebSocket disconnected; HTTP fallback used for cumulative metrics")
                else:
                    content += "\n\n⚠️ 警告：直播期间WebSocket连接中断，恢复失败。所有统计数据可能不完整！"
                    logger.error("[Offline Summary] WebSocket disconnected and all recovery attempts failed")
        else:
            try:
                content = self.offline_summary_template.format(
                    name=self._anchor_nickname or "",
                    views="--",
                    likes="--",
                    peak="--",
                    avg="--",
                    followers="0",
                    members="0",
                    badges="0",
                    gifts="--",
                    duration="--",
                )
            except KeyError as e:
                logger.error(f"Template format error: missing key {e}")
                content = f"直播结束 live.douyin.com/{self.live_id}"

        # Post to Weibo — but only if the data is reliable.
        # If WS disconnected and ALL recovery attempts failed, the stats are
        # garbage (zeroes, channel-all-day fallbacks, etc.).  Silence is better
        # than false data.
        if self.stats_recorder is not None:
            r = self.stats_recorder
            if r.ws_recovery_attempted and not r.http_cumulative_recovery:
                logger.warning(
                    "[Offline Summary] SKIPPING Weibo post: WS disconnected and "
                    "all recovery attempts failed. Stats would be unreliable."
                )
                success = False
                self.log_notification("live_end_skipped_unreliable", "", False)
                return
        else:
            # No recorder at all — nothing to report.
            logger.warning(
                "[Offline Summary] SKIPPING Weibo post: no stats recorder available."
            )
            return

        logger.info("Stream is OFFLINE → posting summary")
        if self._dry_run:
            logger.info(f"[DRY-RUN] Would post offline summary:\n{content}")
            success = True
        else:
            success = self.poster.post_tweet(content)
        self.log_notification("live_end", content, success)

        # Stop the recorder — sets _stop_event (kills background threads)
        # and atomically writes live=False to the dashboard JSON
        if self.stats_recorder:
            self.stats_recorder.stop()

    def _print_summary_from_recorder(self):
        """Print summary to console only (not Weibo)."""
        if not self.stats_recorder:
            return
        self.stats_recorder.stream_end_time = datetime.now()
        try:
            from builder.auth import DouyinAuth  # type: ignore[import-untyped]
            auth = DouyinAuth()
            auth.perepare_auth(self.dy_cookie_str, "", "")
            self.stats_recorder._take_post_snapshot(auth)
        except Exception:
            pass
        self.stats_recorder._generate_summary()
        self.stats_recorder.stop()
        self.stats_recorder = None

    def run_once(self) -> bool:
        now = datetime.now()
        print(f"\n[{now.strftime('%H:%M:%S')}] Checking stream status...", flush=True)

        try:
            # Suppress stdout from check_status() which may print debug dicts
            old_stdout = sys.stdout
            sys.stdout = open(os.devnull, 'w')
            try:
                room_info = self.checker.check_status()
            finally:
                sys.stdout = old_stdout
            
            logger.debug(f"check_status() returned: {list(room_info.keys())}")
        except Exception as e:
            logger.error(f"Failed to check stream status: {e}")
            return False

        # ── Periodic cookie reload (picks up refreshes from standalone process) ──
        _reload_every_n = max(1, 300 // self.check_interval)  # ~every 5 min
        if not hasattr(self, '_cookie_reload_counter'):
            self._cookie_reload_counter = 0
        self._cookie_reload_counter += 1
        if self._cookie_reload_counter >= _reload_every_n:
            self._cookie_reload_counter = 0
            data = self.cookie_manager.load()
            if data.get("cookie_str") and data["cookie_str"] != self.dy_cookie_str:
                logger.info(
                    "[CookieReload] Detected updated cookies, hot-reloading..."
                )
                self.reload_cookies()

        new_status = str(room_info.get('room_status', DouyinLiveChecker.STATUS_OFFLINE))

        # ── Cookie health check ───────────────────────────────────────────
        if new_status == "auth_error":
            self._check_cookie_health(room_info)
            # If we were previously LIVE, preserve state — don't trigger
            # a false offline event due to auth failure.
            if self.current_status == DouyinLiveChecker.STATUS_LIVE:
                print(
                    f"[{now.strftime('%H:%M:%S')}] ⚠ Auth error but keeping "
                    f"LIVE state — emergency refresh triggered",
                    flush=True,
                )
                return False
            else:
                print(
                    f"[{now.strftime('%H:%M:%S')}] ⚠ Auth error — "
                    f"retrying next cycle",
                    flush=True,
                )
                return False
        status_text = "LIVE" if new_status == DouyinLiveChecker.STATUS_LIVE else "OFFLINE"
        title = room_info.get('room_title', '')
        if title:
            self.live_title = title

        # FIX: Store HTTP anchor nickname on every check cycle as fallback
        http_anchor = room_info.get('anchor_nickname', '')
        if http_anchor:
            self._anchor_nickname = http_anchor

        changed = False

        # Self-healing: if stream is LIVE but stats recorder died (e.g. WS disconnected),
        # attempt recovery with WS reconnection first, then HTTP fallback.
        # Reset consecutive offline counter whenever HTTP says LIVE
        if new_status == DouyinLiveChecker.STATUS_LIVE:
            self._consecutive_offline_count = 0
            if not self.stats_recorder or not self.stats_recorder.is_running():
                if HAS_LIVE_DETAILS and self.current_status == DouyinLiveChecker.STATUS_LIVE:
                    if self.stats_recorder and self.stats_recorder.cumulative_views > 0:
                        # Recorder had real data but WS died - attempt recovery
                        if self.stats_recorder.ws_disconnected:
                            logger.warning("WS died mid-stream. Attempting recovery (WS reconnect + HTTP fallback)...")
                            recovery_success = self.stats_recorder.attempt_ws_recovery(checker=self)
                            if recovery_success:
                                logger.info("WS recovery successful! Resuming/recovered data collection.")
                            else:
                                logger.error("WS recovery failed. Using incomplete data for offline summary.")
                        else:
                            logger.warning("Recorder died but not due to WS. Keeping old recorder.")
                    else:
                        logger.info("Stats recorder was dead, restarting for live stream...")
                        self.stats_recorder = LiveStatsRecorder(
                            self.live_id, self.dy_cookie_str, verbose=self.verbose
                        )
                        self.stats_recorder.start_background()

        if self.current_status is None:
            if new_status == DouyinLiveChecker.STATUS_LIVE:
                # Check if we're restarting onto an already-live stream.
                # If the stats JSON was recently written for the same live_id,
                # skip the start Weibo to avoid duplicate notifications.
                if _stats_json_indicates_restart(self.live_id):
                    self._live_posted = True
                    print(f"[{now.strftime('%H:%M:%S')}] Stream is LIVE (restart) – resuming stats without notification", flush=True)
                else:
                    print(f"[{now.strftime('%H:%M:%S')}] Stream is LIVE – starting stats recording + notification", flush=True)
                self.handle_live(room_info)
                # Wait for _take_pre_snapshot() to finish (max 15s).
                # follower_before always comes from a fresh API capture on
                # each stream start — never from stale seed/saved data.
                if self.stats_recorder:
                    self.stats_recorder._pre_snapshot_done.wait(timeout=15)
                changed = True
            else:
                print(f"[{now.strftime('%H:%M:%S')}] Stream is OFFLINE – waiting for live", flush=True)
            self.current_status = new_status
        elif new_status != self.current_status:
            changed = True
            if new_status == DouyinLiveChecker.STATUS_LIVE:
                # New stream cycle — reset guards from the previous stream.
                self._live_posted = False
                self._summary_posted = False
                print(f"[{now.strftime('%H:%M:%S')}] Stream went LIVE – starting stats recording + notification", flush=True)
                self.handle_live(room_info)
            else:
                # FIX: Handle case where Douyin WebSocket stays connected indefinitely
                # after stream ends. Track consecutive HTTP OFFLINE detections; after
                # CONSECUTIVE_OFFLINE_LIMIT, force handle_offline() despite WS being up.
                if self.stats_recorder and self.stats_recorder.is_running():
                    self._consecutive_offline_count += 1
                    if self._consecutive_offline_count < self.CONSECUTIVE_OFFLINE_LIMIT:
                        logger.warning(
                            f"HTTP reports OFFLINE ({self._consecutive_offline_count}/{self.CONSECUTIVE_OFFLINE_LIMIT}) "
                            f"but WebSocket is still connected. Will force offline after limit."
                        )
                        # Reset status back to LIVE so we don't lose live state
                        self.current_status = DouyinLiveChecker.STATUS_LIVE
                        print(f"[{now.strftime('%H:%M:%S')}] ⚠ HTTP says OFFLINE but WS still running ({self._consecutive_offline_count}/{self.CONSECUTIVE_OFFLINE_LIMIT})", flush=True)
                        time.sleep(5)
                        return False
                    else:
                        logger.warning(
                            f"HTTP OFFLINE detected {self.CONSECUTIVE_OFFLINE_LIMIT} consecutive times. "
                            f"Forcing offline event despite WS being connected."
                        )
                        print(f"[{now.strftime('%H:%M:%S')}] Stream went OFFLINE (confirmed by {self.CONSECUTIVE_OFFLINE_LIMIT} HTTP checks) – posting notification", flush=True)
                        # handle_offline will stop the recorder and take post-snapshot internally
                        self.handle_offline(room_info)
                        self.current_status = new_status
                        return True

                print(f"[{now.strftime('%H:%M:%S')}] Stream went OFFLINE – posting notification", flush=True)
                self.handle_offline(room_info)
            self.current_status = new_status
        else:
            # Print detailed info and preview every check cycle
            if self.stats_recorder and self.stats_recorder.is_running():
                r = self.stats_recorder
                uptime = ""
                if r.stream_start_time:
                    elapsed = now - r.stream_start_time
                    mins, secs = divmod(int(elapsed.total_seconds()), 60)
                    hours, mins = divmod(mins, 60)
                    if hours > 0:
                        uptime = f" | Uptime: {hours}h{mins}m"
                    else:
                        uptime = f" | Uptime: {mins}m{secs}s"
                viewers = fmt_wan(r.current_viewers) if r.current_viewers else "?"
                stats_info = f" | Viewers: {viewers} | Likes: {fmt_wan(r.total_likes) if r.total_likes else '?'}{uptime}"
                print(f"[{now.strftime('%H:%M:%S')}] Status: {status_text} | Title: {title}{stats_info}", flush=True)
                
                # Print preview of what will be posted to Weibo
                # FIX: Use fallback anchor name in preview
                anchor_name = r.anchor_nickname or self._anchor_nickname
                vals = self._build_offline_summary_values(r, anchor_name, now)
                try:
                    preview = self.offline_summary_template.format(
                        name=vals['name'],
                        views=vals['views'] or "--",
                        likes=vals['likes'] or "--",
                        peak=vals['peak'] or "--",
                        avg=vals['avg'] or "--",
                        followers=vals['followers'],
                        members=vals['members'],
                        badges=vals['badges'],
                        gifts=vals['gifts'] if vals['gifts'] else "--",
                        duration=vals['duration'] or "--",
                    )
                    print(f"\n📋 WEIBO PREVIEW (will post when stream ends):")
                    print(strip_emoji(preview))
                    print()
                except Exception as e:
                    logger.debug(f"Error generating preview: {e}")

                # Write live stats for the web dashboard (atomic JSON file)
                if self.stats_recorder and self.stats_recorder.is_running():
                    self.stats_recorder._write_live_stats_json()
                    # Periodic HTTP refresh of follower_after (every 1 min)
                    # so follower delta = follower_after - follower_before is accurate.
                    r = self.stats_recorder
                    last_refresh = getattr(r, '_last_http_refresh', None)
                    if last_refresh is None or (now - last_refresh).total_seconds() >= 60:
                        logger.debug("[Main Loop] Periodic HTTP refresh of follower count...")
                        try:
                            r.fetch_cumulative_via_http(mark_recovery=False)
                        except Exception:
                            pass
            else:
                print(f"[{now.strftime('%H:%M:%S')}] Status: {status_text} | Title: {title}", flush=True)
                # If the recorder thread died (e.g. WebSocket disconnected) but the
                # stream is still LIVE, keep writing the last known stats so the
                # dashboard doesn't go completely stagnant.  The ws_connected field
                # will be False so the UI can show a warning.
                if self.stats_recorder and new_status == DouyinLiveChecker.STATUS_LIVE:
                    # Periodically refresh cumulative metrics via HTTP (every 1 min)
                    # when WebSocket is unavailable, so cumulative_views and
                    # follower counts don't go completely stale.
                    r = self.stats_recorder
                    last_refresh = getattr(r, '_last_http_refresh', None)
                    if last_refresh is None or (now - last_refresh).total_seconds() >= 60:
                        if r.ws_disconnected and not r.ws_recovery_attempted:
                            # Primary recovery path still available — try it
                            logger.info("[Main Loop] Attempting deferred WS recovery...")
                            r.attempt_ws_recovery(checker=self)
                        elif r.http_cumulative_recovery:
                            # Already in HTTP-fallback; refresh cumulative data
                            logger.debug("[Main Loop] Refreshing cumulative metrics via HTTP...")
                            r.fetch_cumulative_via_http()
                        else:
                            # Periodic HTTP refresh even when WS is healthy.
                            # Keeps follower_after up-to-date for accurate delta.
                            # mark_recovery=False — this is NOT a recovery, just a refresh.
                            logger.debug("[Main Loop] Periodic HTTP refresh of follower count...")
                            r.fetch_cumulative_via_http(mark_recovery=False)
                    self.stats_recorder._write_live_stats_json()

        return changed

    def run_forever(self):
        logger.info("=" * 60)
        logger.info("  StreamMonitor server starting (24/7 mode)")
        logger.info(f"  live.douyin.com/{self.live_id}")
        logger.info(f"  Check interval: {self.check_interval}s")
        logger.info(f"  HasLiveDetails: {HAS_LIVE_DETAILS}")
        logger.info("=" * 60)

        consecutive_errors = 0
        max_consecutive_errors = 10

        while True:
            try:
                changed = self.run_once()
                if changed:
                    consecutive_errors = 0  # reset on success
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                logger.info("Monitor stopped by user")
                if self.stats_recorder and self.stats_recorder.is_running():
                    logger.info("Stopping stats recorder...")
                    self.stats_recorder.stop()
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Unexpected error in monitor loop (attempt {consecutive_errors}/{max_consecutive_errors}): {e}")
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"Too many consecutive errors ({consecutive_errors}). Waiting 5min before retrying...")
                    time.sleep(300)
                    consecutive_errors = 0
                else:
                    time.sleep(self.check_interval)


# ======================================================================
# Configuration & CLI
# ======================================================================

def load_env_config():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        load_dotenv(env_path)
    return {
        'DY_LIVE_COOKIES': os.getenv('DY_LIVE_COOKIES', ''),
        'DY_COOKIES': os.getenv('DY_COOKIES', ''),
        'WEIBO_COOKIE': os.getenv('WEIBO_COOKIE', ''),
        'DY_LIVE_ID': os.getenv('DY_LIVE_ID', ''),
        'DY_LIVE_TITLE': os.getenv('DY_LIVE_TITLE', ''),
        'CHECK_INTERVAL': int(os.getenv('CHECK_INTERVAL', '10')),
        'LIVE_TEMPLATE': os.getenv('LIVE_TEMPLATE', ''),
        'OFFLINE_SUMMARY_TEMPLATE': os.getenv('OFFLINE_SUMMARY_TEMPLATE', ''),
    }

def main():
    parser = argparse.ArgumentParser(
        description='Douyin Stream Monitor – Posts Weibo notifications when a stream goes live/offline, with optional detailed live statistics'
    )
    parser.add_argument('--live-id', type=str, help='Douyin live room ID')
    parser.add_argument('--dy-cookie', type=str, help='Douyin cookie string')
    parser.add_argument('--weibo-cookie', type=str, help='Weibo web cookie string')
    parser.add_argument('--interval', type=int, default=10, help='Check interval in seconds (default: 10)')
    parser.add_argument('--live-title', type=str, default='', help='Anchor live room title')
    parser.add_argument('--live-template', type=str, default='', help='Weibo template when live')
    parser.add_argument('--once', action='store_true', help='Run a single check and exit')
    parser.add_argument('--record-stats', action='store_true',
                        help='Record detailed live statistics (WebSocket connection required)')
    parser.add_argument('--verbose', action='store_true',
                        help='Show detailed live event logs (likes, follows, gifts, etc.)')
    parser.add_argument('--summary-only', action='store_true',
                        help='Skip monitoring, just connect to a currently live stream and show summary when done')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview formatted templates without actually posting to Weibo')
    parser.add_argument('--preview-offline', action='store_true',
                        help='Connect to the current live stream for 60s, then preview the offline summary with real data')
    parser.add_argument('--offline-weibo-template', action='store_true',
                        help='Connect to a live stream and post the offline template with real stats to Weibo every 5 minutes (for testing)')

    args = parser.parse_args()
    config = load_env_config()

    dy_cookie = args.dy_cookie or config['DY_LIVE_COOKIES'] or config['DY_COOKIES']
    weibo_cookie = args.weibo_cookie or config['WEIBO_COOKIE']
    live_id = args.live_id or config['DY_LIVE_ID']
    live_title = args.live_title or config['DY_LIVE_TITLE']
    check_interval = args.interval or config['CHECK_INTERVAL']
    record_stats = args.record_stats
    verbose = args.verbose

    if not dy_cookie:
        logger.error("Missing Douyin cookie.  Set DY_COOKIES/DY_LIVE_COOKIES in .env or pass --dy-cookie.")
        sys.exit(1)

    if not live_id:
        logger.error("Missing live room ID.  Set DY_LIVE_ID in .env or pass --live-id.")
        sys.exit(1)

    if args.summary_only:
        if not HAS_LIVE_DETAILS:
            logger.error("Summary mode requires Douyin_Spider modules (protobuf, WebSocket).")
            sys.exit(1)
        logger.info(f"Summary-only mode: connecting to live.douyin.com/{live_id}")
        recorder = LiveStatsRecorder(live_id, dy_cookie, verbose=True)
        recorder.start_background()
        try:
            while recorder.is_running():
                time.sleep(1)
        except KeyboardInterrupt:
            recorder.stop()
        return

    if args.preview_offline:
        if not HAS_LIVE_DETAILS:
            logger.error("Preview requires Douyin_Spider modules (protobuf, WebSocket).")
            sys.exit(1)
        logger.info(f"Preview mode: connecting to live.douyin.com/{live_id} for 1 minute to capture real data...")
        print(f"  Capturing live data for 60 seconds... (press Ctrl+C to skip wait)")

        recorder = LiveStatsRecorder(live_id, dy_cookie, verbose=False)
        recorder.start_background()
        try:
            for i in range(60, 0, -1):
                if not recorder.is_running():
                    break
                print(f"\r  ⏳ Waiting {i}s... ", end="", flush=True)
                time.sleep(1)
            print(f"\r  ✅ Capture complete!           ")
        except KeyboardInterrupt:
            print(f"\r  ⏹ Stopped early.               ")
        recorder.stream_end_time = datetime.now()
        recorder.stop()
        time.sleep(0.5)

        # Take post-snapshot for follower count delta
        try:
            from builder.auth import DouyinAuth
            auth = DouyinAuth()
            auth.perepare_auth(recorder.cookie_str, "", "")
            recorder._take_post_snapshot(auth)
        except Exception:
            pass

        # Build the preview using the same offline_summary_template logic
        offline_summary_tpl = config.get('OFFLINE_SUMMARY_TEMPLATE', '') or ''
        if offline_summary_tpl:
            offline_summary_tpl = offline_summary_tpl.replace('\\n', '\n')
        else:
            offline_summary_tpl = "{name} 直播结束\n场观：{views}\n点赞：{likes}\n最高在线：{peak}\n平均在线：{avg}\n新增粉丝：{followers}\n新增粉丝团：{members}\n点亮灯牌：{badges}\n热门礼物：{gifts}"

        r = recorder

        # Fallback: if recorder didn't get the anchor nickname, try page HTML
        if not r.anchor_nickname:
            try:
                checker = DouyinLiveChecker(live_id, dy_cookie)
                room_info = checker.check_status()
                r.anchor_nickname = room_info.get('anchor_nickname', '') or r.anchor_nickname
            except Exception:
                pass

        vals = StreamMonitor._build_offline_summary_values(
            r, r.anchor_nickname or "",
            r.stream_end_time or datetime.now())

        try:
            preview = offline_summary_tpl.format(
                name=vals['name'],
                views=vals['views'] or "--",
                likes=vals['likes'] or "--",
                peak=vals['peak'] or "--",
                avg=vals['avg'] or "--",
                followers=vals['followers'],
                members=vals['members'],
                badges=vals['badges'],
                gifts=vals['gifts'] if vals['gifts'] else "--",
                duration=vals['duration'] or "--",
            )
        except KeyError as e:
            print(f"[ERROR] Template contains unknown placeholder: {e}")
            sys.exit(1)

        print("\n" + "=" * 60)
        print("  📋 离线总结预览 (Preview)")
        print("=" * 60)
        print(strip_emoji(preview))
        print("=" * 60 + "\n")
        print("[INFO] This preview was generated from 60 seconds of actual stream data.")
        print("       The real offline post will use the entire stream's data.\n")
        return

    if args.offline_weibo_template:
        if not HAS_LIVE_DETAILS:
            logger.error("Offline weibo template mode requires Douyin_Spider modules (protobuf, WebSocket).")
            sys.exit(1)
        if not weibo_cookie:
            logger.error("Missing Weibo cookie.  Set WEIBO_COOKIE in .env or pass --weibo-cookie.")
            sys.exit(1)

        logger.info(f"Offline weibo template test mode: connecting to live.douyin.com/{live_id}")
        poster = WeiboPoster(weibo_cookie)

        offline_summary_tpl = config.get('OFFLINE_SUMMARY_TEMPLATE', '') or ''
        if offline_summary_tpl:
            offline_summary_tpl = offline_summary_tpl.replace('\\n', '\n')
        else:
            offline_summary_tpl = "{name} 直播结束\n场观：{views}\n点赞：{likes}\n最高在线：{peak}\n平均在线：{avg}\n新增粉丝：{followers}\n新增粉丝团：{members}\n点亮灯牌：{badges}\n热门礼物：{gifts}"

        recorder = LiveStatsRecorder(live_id, dy_cookie, verbose=True)
        recorder.start_background()

        print(f"\n{'='*60}")
        print(f"  🧪 Offline Weibo Template Test Mode")
        print(f"  Posting template with real stats to Weibo every 5 minutes")
        print(f"{'='*60}\n")

        try:
            post_count = 0
            while recorder.is_running():
                for _ in range(300):  # 5 minutes = 300 seconds, checked per-second
                    if not recorder.is_running():
                        break
                    time.sleep(1)
                if not recorder.is_running():
                    break

                post_count += 1
                now = datetime.now()
                r = recorder

                # Compute the template variables from live recorder data
                # Try to also get anchor name from HTTP page as fallback
                if not r.anchor_nickname:
                    try:
                        checker = DouyinLiveChecker(live_id, dy_cookie)
                        room_info = checker.check_status()
                        r.anchor_nickname = room_info.get('anchor_nickname', '') or r.anchor_nickname
                    except Exception:
                        pass
                anchor_name = r.anchor_nickname or ""
                vals = StreamMonitor._build_offline_summary_values(r, anchor_name, now)
                try:
                    content = offline_summary_tpl.format(
                        name=vals['name'],
                        views=vals['views'] or "--",
                        likes=vals['likes'] or "--",
                        peak=vals['peak'] or "--",
                        avg=vals['avg'] or "--",
                        followers=vals['followers'],
                        members=vals['members'],
                        badges=vals['badges'],
                        gifts=vals['gifts'] if vals['gifts'] else "--",
                        duration=vals['duration'] or "--",
                    )
                except KeyError as e:
                    logger.error(f"Template format error: missing key {e}")
                    continue

                print(f"\n{'─'*60}")
                print(f"  📝 Test Post #{post_count} at {now.strftime('%H:%M:%S')}")
                print(f"{'─'*60}")
                print(strip_emoji(content))
                print(f"{'─'*60}")
                success = poster.post_tweet(content)
                if success:
                    print(f"  ✅ Posted successfully!\n")
                else:
                    print(f"  ❌ Failed to post\n")
        except KeyboardInterrupt:
            print(f"\n⏹ Stopped by user.\n")
        finally:
            recorder.stop()
        return

    if not weibo_cookie:
        logger.error("Missing Weibo cookie.  Set WEIBO_COOKIE in .env or pass --weibo-cookie.")
        sys.exit(1)

    offline_summary_tpl = config.get('OFFLINE_SUMMARY_TEMPLATE', '') or ''

    monitor = StreamMonitor(
        live_id=live_id,
        dy_cookie_str=dy_cookie,
        weibo_cookie=weibo_cookie,
        live_template=args.live_template or config['LIVE_TEMPLATE'] or None,
        check_interval=check_interval,
        live_title=live_title,
        record_stats=record_stats,
        verbose=verbose,
        offline_summary_template=offline_summary_tpl or None,
        dry_run=args.dry_run,
    )

    if args.once:
        monitor.run_once()
    else:
        monitor.run_forever()


if __name__ == '__main__':
    main()