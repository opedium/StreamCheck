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
import signal
import argparse
import threading
import builtins
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

# Beijing timezone (UTC+8) — used for natural-day boundaries
_BEIJING_TZ = timezone(timedelta(hours=8))


def _beijing_now() -> datetime:
    """Return current datetime in Beijing time (UTC+8)."""
    return datetime.now(_BEIJING_TZ)

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from loguru import logger
from websocket import WebSocketApp

# Suppress the InsecureRequestWarning from urllib3
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from contextlib import contextmanager


@contextmanager
def _suppress_stdout():
    """Temporarily redirect stdout to /dev/null, properly closing the fd."""
    old_stdout = sys.stdout
    with open(os.devnull, 'w') as null:
        sys.stdout = null
        try:
            yield
        finally:
            sys.stdout = old_stdout

# Force stdout/stderr to be unbuffered so log messages appear immediately
sys.stdout.reconfigure(line_buffering=True, encoding='utf-8')  # Python 3.7+
sys.stderr.reconfigure(line_buffering=True, encoding='utf-8')


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


def safe_print(*args, **kwargs):
    """Print that survives closed stdout (e.g., PM2 log rotation).

    When PM2 rotates the log pipe, stdout becomes closed and print()
    raises ``ValueError: I/O operation on closed file``.  This wrapper
    swallows that error so the monitor loop never crashes on output.
    """
    try:
        print(*args, **kwargs)
    except (ValueError, OSError):
        pass


# Stats JSON file path for the web dashboard (configurable via env var)
_STATS_FILE = os.path.join(os.path.dirname(__file__), 'live_stats.json')
STATS_FILE = os.environ.get('STATS_FILE', _STATS_FILE)

# CSV time-series files — efficient append-only format for long-term data
_STATS_CSV_FILE = os.path.join(os.path.dirname(__file__), 'stats_timeseries.csv')
STATS_CSV_FILE = os.environ.get('STATS_CSV_FILE', _STATS_CSV_FILE)
_NOTIF_CSV_FILE = os.path.join(os.path.dirname(__file__), 'notification_log.csv')
NOTIF_CSV_FILE = os.environ.get('NOTIF_CSV_FILE', _NOTIF_CSV_FILE)


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
    """Try to parse known fields from displayLong string.

    Returns {key: (value, has_wan)} where has_wan is True if the original
    matched text contained 万 (indicating the value needs *10000).
    """
    result = {}
    for key, pattern in DISPLAY_PATTERNS.items():
        m = re.search(pattern, display_long, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            has_wan = '万' in m.group(0)
            result[key] = (val, has_wan)
    return result


# ======================================================================
# Manual protobuf wire format parsers
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


# ======================================================================
# RoomUserSeqMessage parser
#
# Fields (from protobuf definition):
#   field  3: total                   (varint) → current online viewers
#   field  6: popularity              (varint) → 人气值 (inflated, NOT actual)
#   field  7: total_user              (varint) → cumulative unique visitors
#   field 10: online_user_for_anchor  (varint) → anchor's view of online count
#   field 11: total_pv_for_anchor     (string) → cumulative page views
#
# Returns (current_viewers, cumulative_views) tuple, or (0, 0) on failure.
# ======================================================================

def parse_room_user_seq_msg(payload: bytes):
    """
    Parse RoomUserSeqMessage protobuf bytes to extract:
      - field 3 (total): current online viewer count (varint)
      - field 11 (totalPvForAnchor): cumulative view count (string, may have 万)
    Returns (current_viewers, cumulative_views).
    """
    current_viewers = 0
    cumulative_views = 0
    offset = 0
    try:
        while offset < len(payload):
            tag, offset = _parse_varint(payload, offset)
            field_num = tag >> 3
            wire_type = tag & 0x7

            if field_num == 3 and wire_type == 0:
                # total — current online viewers (varint)
                current_viewers, offset = _parse_varint(payload, offset)
            elif field_num == 11 and wire_type == 2:
                # totalPvForAnchor — cumulative view count (string)
                str_len, offset = _parse_varint(payload, offset)
                str_bytes = payload[offset:offset + str_len]
                str_val = str_bytes.decode('utf-8', errors='replace')
                cumulative_views = parse_chinese_number(str_val)
                offset += str_len
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
    return current_viewers, cumulative_views


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
                # Extract total member count from content string.
                # Douyin formats observed:
                #   "恭喜 xxx 成为粉丝团第289687名成员"
                #   "加入了粉丝团，他是第528名成员"
                #   "xxx 加入粉丝团，当前团成员 12345"
                #   "恭喜 XY 成为第5667名KOI成员"
                for pattern in [
                    r'第\s*(\d+)\s*名',       # 第289687名
                    r'第\s*(\d+)\s*位',       # 第528位
                    r'团成员\s*(\d+)',         # 团成员 12345
                    r'(\d+)\s*名\s*成员',      # 5667名成员 (品牌名 between 名 and 成员)
                    r'(\d+)\s*位\s*成员',      # 528位成员
                    r'(\d+)\s*人',             # 12345人
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


def dump_all_protobuf_fields(payload: bytes, label: str = "", max_depth: int = 2, _indent: int = 0):
    """Dump ALL fields from raw protobuf bytes (including unknown/undefined fields).
    Recurses into sub-messages. Uses print() for guaranteed output."""
    import struct as _struct
    prefix = "  " * _indent
    if _indent == 0:
        print(f"[PROTO-DUMP] {label}: {len(payload)} bytes", flush=True)
    offset = 0
    field_count = 0
    while offset < len(payload):
        try:
            tag, offset = _parse_varint(payload, offset)
            field_num = tag >> 3
            wire_type = tag & 0x7
            if wire_type == 0:  # Varint
                val, offset = _parse_varint(payload, offset)
                print(f"{prefix}[{label}] field {field_num} (varint) = {val}", flush=True)
                field_count += 1
            elif wire_type == 1:  # 64-bit
                if offset + 8 <= len(payload):
                    val = _struct.unpack('<d', payload[offset:offset+8])[0]
                    print(f"{prefix}[{label}] field {field_num} (64-bit) = {val}", flush=True)
                offset += 8
                field_count += 1
            elif wire_type == 2:  # Length-delimited (string or sub-message)
                length, offset = _parse_varint(payload, offset)
                sub = payload[offset:offset + length]
                offset += length
                field_count += 1
                # Always try to decode as string first
                try:
                    s = sub.decode('utf-8', errors='replace')
                    if len(s) < 100:
                        print(f"{prefix}[{label}] field {field_num} (str) = {s!r}", flush=True)
                    else:
                        print(f"{prefix}[{label}] field {field_num} (str, {len(s)}c) = {s[:80]!r}...", flush=True)
                except Exception:
                    pass
                # Recurse into sub-messages for key fields
                if field_num in (1, 7, 8, 15) and _indent < max_depth:
                    dump_all_protobuf_fields(sub, f'{label}.f{field_num}', max_depth, _indent + 1)
            elif wire_type == 5:  # 32-bit
                if offset + 4 <= len(payload):
                    val = _struct.unpack('<f', payload[offset:offset+4])[0]
                    print(f"{prefix}[{label}] field {field_num} (32-bit) = {val}", flush=True)
                offset += 4
                field_count += 1
            else:
                print(f"{prefix}[{label}] Unknown wire type {wire_type} at offset {offset}", flush=True)
                break
        except Exception as e:
            print(f"{prefix}[{label}] Parse error at offset {offset}: {e}", flush=True)
            break
    if _indent == 0:
        print(f"[PROTO-DUMP] {label}: {field_count} top-level fields", flush=True)


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
        self.new_members = 0
        self.member_count = 0          # max memberCount from MemberMessage protobuf
        self._fresh_member_count = 0  # latest memberCount (resets on reconnect)
        self.light_badges = 0
        self._light_badge_users = set()  # (user_id, date_str) — one per user per natural day
        self.fan_club_joins = 0        # FansclubMessage type=2 event count
        self.fan_club_gift_joins = 0   # 入团卡 (join-card) gift count
        self.fan_club_start_count = 0  # total members from first fansclub msg
        self.fan_club_end_count = 0    # total members from latest fansclub msg
        self.gift_events = []

        # WS followCount tracking (precise follower delta)
        # SocialMessage.followCount carries the anchor's absolute follower
        # count at the time of each follow event.  First-vs-last delta is
        # immune to API 万-rounding precision loss and to per-event counter
        # drift (missed events during reconnect).
        self.ws_follow_first = 0       # first followCount seen this stream
        self.ws_follow_last = 0        # most recent followCount seen
        self.ws_follow_last_time = None  # datetime when ws_follow_last VALUE last increased
        self._ws_restart_count = 0     # how many times WS was restarted for stagnant followCount
        self._ws_last_restart_time = None  # cooldown: don't restart more than once per 10 min
        # HTTP API follower tracking (authoritative fallback)
        # When WS followCount stalls (common), the HTTP API
        # get_user_info().follower_count provides the anchor's true
        # follower count.  Only precise integer values (not 万-rounded)
        # are accepted to avoid ±500 rounding error.
        self.http_follow_first = 0     # first precise HTTP follower count
        self.http_follow_last = 0      # latest precise HTTP follower count
        self.follower_count = 0        # anchor's actual total follower count (from HTTP API, authoritative)

        # Gift dedup: (group_id, gift_name, user_id) → last_repeat_count
        self._gift_dedup = {}
        self._gift_dedup_last_cleanup = datetime.now()

        # viewer tracking
        self.current_viewers = 0
        self._last_viewer_update = None  # datetime of last RoomStatsMessage
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
        self._intentional_stop = False         # True when stop() is called (not an unexpected disconnect)
        self._last_data_message_time = None    # when the last data message arrived; used for WS silence detection
        self._fresh_member_update_time = None  # when _fresh_member_count was last updated

    def _get_current_viewers(self) -> int:
        """Return current viewers — displayValue is primary, memberCount is emergency fallback.

        displayValue from RoomStatsMessage is the true concurrent viewer count.
        memberCount from MemberMessage is a per-user number (NOT concurrent viewers),
        used ONLY as an emergency fallback when the stream just started and no
        RoomStatsMessage has been received yet.
        """
        display_time = getattr(self, '_last_viewer_update', None)
        now = datetime.now()
        ROOMSTAT_STALE_SECONDS = 60

        # If displayValue has ever been received and is fresh, use it
        if display_time is not None:
            staleness = (now - display_time).total_seconds()
            if staleness <= ROOMSTAT_STALE_SECONDS:
                return max(getattr(self, 'current_viewers', 0), 0)

        # DisplayValue is stale — try memberCount fallback WITH staleness check.
        # memberCount is per-user data from MemberMessage, not concurrent viewers,
        # so it's only useful as a brief bridge during WS reconnect.
        member_count = getattr(self, '_fresh_member_count', 0)
        member_time = getattr(self, '_fresh_member_update_time', None)
        if member_count > 0 and member_time is not None:
            staleness = (now - member_time).total_seconds()
            if staleness <= ROOMSTAT_STALE_SECONDS:
                return member_count

        # Everything stale — return last known displayValue anyway
        return max(getattr(self, 'current_viewers', 0), 0)

    @staticmethod
    def _clear_csv_if_new_stream():
        """Clear CSV if last row is >2 hours old — this is a new stream,
        not a crash recovery.  Keeps the file clean across streams."""
        import csv as _csv
        try:
            if not os.path.exists(STATS_CSV_FILE):
                return
            with open(STATS_CSV_FILE, 'r', encoding='utf-8', newline='') as _f:
                reader = list(_csv.DictReader(_f))
            if not reader:
                return
            ts_str = reader[-1].get('timestamp', '')
            if ts_str:
                ts = datetime.fromisoformat(ts_str)
                age_hours = (datetime.now() - ts).total_seconds() / 3600
                if age_hours > 2:
                    os.remove(STATS_CSV_FILE)
                    logger.info(f"[CSV] Cleared old CSV (last row {age_hours:.1f}h ago — new stream)")
        except Exception:
            pass

    @classmethod
    def _recover_from_csv(cls, live_id: str) -> dict:
        """Recover cumulative stats from the last CSV row after a crash.

        Only recovers if the last row is ≤10 minutes old and matches the
        given live_id.  Returns a dict of field→value, or {} on miss.
        """
        import csv as _csv
        try:
            if not os.path.exists(STATS_CSV_FILE):
                return {}
            with open(STATS_CSV_FILE, 'r', encoding='utf-8', newline='') as _f:
                reader = list(_csv.DictReader(_f))
            if not reader:
                return {}
            row = reader[-1]
            # Only recover if timestamp is recent (≤10 min) and live_id matches
            ts_str = row.get('timestamp', '')
            if ts_str:
                ts = datetime.fromisoformat(ts_str)
                if (datetime.now() - ts).total_seconds() > 600:
                    logger.info(f"[CSV Recovery] Last row too old ({ts_str}), skipping")
                    return {}
            if row.get('live_id', '') != live_id:
                return {}
            recovered = {}
            # NOTE: total_likes and cumulative_views are NOT recovered.
            # They are per-stream cumulative values reported by WebSocket
            # messages (LIKE/ROOMSTATS/USERSEQ).  Recovering yesterday's
            # all-time totals would block today's lower per-stream values
            # due to max() semantics in the message handlers.
            for field in ('peak_viewers',
                          'ws_follow_first', 'ws_follow_last',
                          'http_follow_first', 'http_follow_last',
                          'fan_club_start_count', 'fan_club_end_count',
                          'fan_club_joins', 'fan_club_gift_joins',
                          'light_badges', 'member_count'):
                val = row.get(field, '')
                if val:
                    recovered[field] = int(val)
            # Recover light_badge_day as a string (used for daily dedup boundary)
            _badge_day = row.get('light_badge_day', '')
            if _badge_day:
                recovered['light_badge_day'] = _badge_day
            # Recover top-5 gift names+counts from CSV
            for rank in (1, 2, 3, 4, 5):
                g_name = row.get(f'gift_top{rank}_name', '') or ''
                g_count = row.get(f'gift_top{rank}_count', '') or ''
                if g_name and g_count:
                    recovered.setdefault('gift_events', []).append({
                        'user': '(crash-recovered)',
                        'gift': g_name,
                        'count': int(g_count),
                    })
            # Recover viewer samples: reconstruct synthetic samples from average
            # so the overall average is preserved across crashes.
            _vs_count = int(row.get('viewer_sample_count', 0) or 0)
            _vs_sum = int(row.get('viewer_sample_sum', 0) or 0)
            if _vs_count > 0 and _vs_sum > 0:
                _vs_avg = _vs_sum // _vs_count
                recovered['viewer_samples'] = [_vs_avg] * _vs_count
            if row.get('stream_start_time'):
                recovered['stream_start_time'] = row['stream_start_time']
            logger.info(f"[CSV Recovery] Recovered {len(recovered)} fields from last CSV row "
                        f"(ts={ts_str}, live_id={live_id})")
            return recovered
        except Exception as e:
            logger.warning(f"[CSV Recovery] Failed: {e}")
            return {}

    def start_background(self, callback=None):
        self._callback = callback
        # Clear old CSV data if this is a new stream (last row >2 hours old)
        self._clear_csv_if_new_stream()
        # Attempt CSV crash recovery before starting
        _recovered = self._recover_from_csv(self.live_id)
        if _recovered:
            for key, val in _recovered.items():
                if key == 'stream_start_time':
                    try:
                        self.stream_start_time = datetime.fromisoformat(val)
                    except Exception:
                        pass
                elif key == 'viewer_samples':
                    # Reconstructed synthetic samples — prepend to preserve
                    # average while allowing new real samples to accumulate
                    if isinstance(val, list) and val:
                        self.viewer_samples = list(val)
                        self._last_minute_sample = None
                elif key == 'gift_events':
                    # Reconstructed top-N gifts from CSV — prepend so new
                    # live events don't overwrite crash-recovered data
                    if isinstance(val, list) and val:
                        self.gift_events = list(val) + self.gift_events
                elif key == 'fan_club_start_count':
                    # Baseline — only set if not yet captured from live WS.
                    if self.fan_club_start_count == 0:
                        self.fan_club_start_count = val
                elif key in ('follower_before', 'ws_follow_first'):
                    # Recover these baseline values from CSV for crash recovery
                    # within the SAME stream.  Cross-stream contamination is
                    # prevented by the 10-min freshness + live_id match checks above.
                    # The WS handler's first-event guard (ws_follow_first==0) will
                    # still fire for truly new streams where no CSV row exists.
                    if not hasattr(self, key) or getattr(self, key, 0) == 0:
                        setattr(self, key, val)
                elif key == 'light_badge_day':
                    # Restore the badge day so midnight-boundary eviction and
                    # per-user-per-day dedup work correctly after crash recovery.
                    # Without this, _light_badge_day resets to '' on restart,
                    # causing the midnight-eviction block to refilter the
                    # (empty) dedup set on every badge — harmless, but the
                    # recovered badge count still works correctly.
                    self._light_badge_day = str(val)
                elif hasattr(self, key):
                    current = getattr(self, key, 0)
                    setattr(self, key, max(current, val))
            # CRITICAL: If http_follow_first was recovered from CSV, mark
            # _http_first_seen=True so the first HTTP refresh doesn't
            # overwrite it with the current (higher) API value.  Without
            # this, the follower delta artificially shrinks after every
            # recorder restart because the baseline moves up.
            if self.http_follow_first > 0:
                self._http_first_seen = True
            _vs = len(self.viewer_samples)
            _avg = sum(self.viewer_samples) // _vs if _vs else 0
            logger.info(f"[CSV Recovery] Applied recovered stats: "
                        f"likes={self.total_likes:,}, views={self.cumulative_views:,}, "
                        f"peak={self.peak_viewers:,}, badges={self.light_badges}, "
                        f"viewer_samples={_vs} (avg={_avg:,})")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(f"LiveStatsRecorder started for live.douyin.com/{self.live_id}")

    def stop(self):
        self._stop_event.set()
        self._intentional_stop = True
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
        import random as _random
        logger.warning(f"[WS Recovery] Attempting WebSocket reconnection for room {self.live_id}")
        retry_count = 0
        max_retries = 5
        base_delay = 3

        while retry_count < max_retries:
            try:
                from builder.auth import DouyinAuth  # type: ignore[import-untyped]
                auth = DouyinAuth()
                auth.perepare_auth(self.cookie_str, "", "")

                with _suppress_stdout():
                    # Verify live room is still reachable before reconnecting
                    DouyinAPI.get_live_info(auth, self.live_id)

                logger.info(f"[WS Recovery] Reconnection attempt {retry_count + 1}/{max_retries}: room still reachable, restarting WS in background...")
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
                    # Exponential backoff with jitter: 3s, 6s, 12s, 24s (±20% jitter)
                    delay = base_delay * (2 ** (retry_count - 1))
                    jitter = delay * 0.2 * (2 * _random.random() - 1)
                    delay = max(1, int(delay + jitter))
                    logger.warning(f"[WS Recovery] Reconnection attempt {retry_count}/{max_retries} failed: {e}. Retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    logger.warning(f"[WS Recovery] WebSocket reconnection failed after {max_retries} attempts (total ~{base_delay * (2**(max_retries-1) - 1)}s window)")
        
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
            with _suppress_stdout():
                room_info = DouyinAPI.get_live_info(auth, self.live_id)

            # Fall back to HTTP-parsed room_info when get_live_info fails
            if room_info is None:
                room_info = getattr(self, '_http_room_info', None)
            if room_info is None:
                logger.error("[HTTP Recovery] No room_info available (get_live_info returned None, no HTTP fallback)")
                return False

            room_id = room_info['room_id']
            user_id = room_info['user_id']

            # -- HTTP viewer count fallback --------------------------------
            # When WS RoomStatsMessage/RoomUserSeqMessage go stale (Douyin
            # throttles these after long-running streams), extract viewer
            # count from the enter_room API response.  room_info often
            # contains a "room" sub-object with live view stats.
            try:
                _room_sub = room_info.get('room', {}) or {}
                if isinstance(_room_sub, dict):
                    _viewer = (_room_sub.get('room_view_stats', {}) or {}).get('online_count', 0)
                    if not _viewer:
                        _viewer = _room_sub.get('online_count', 0) or _room_sub.get('viewer_count', 0) or 0
                    if not _viewer:
                        _viewer = room_info.get('online_count', 0) or room_info.get('viewer_count', 0) or 0
                    if _viewer and _viewer > 0:
                        self.current_viewers = int(_viewer)
                        self._last_viewer_update = datetime.now()
                        if self.verbose:
                            logger.info(f"[HTTP Fallback] Viewer count via API = {int(_viewer):,}")
            except Exception:
                pass

            # get_webcast_detail returns the initial bootstrap proto (cursor/internalExt)
            # which does NOT contain stats messages - skip it.
            # Instead just log that we have room_id for reference.
            logger.info(f"[HTTP Recovery] Room confirmed: room_id={room_id}, user_id={user_id}")
            
            # Get follower count from user info
            sec_uid = room_info.get('sec_uid', '')
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                with _suppress_stdout():
                    user_info = DouyinAPI.get_user_info(auth, user_url)

                if not isinstance(user_info, dict):
                    logger.warning(f"[HTTP Recovery] get_user_info returned unexpected type: {type(user_info).__name__}")
                    user_data = {}
                else:
                    user_data = user_info.get('user', {})
                fc = user_data.get('follower_count', 0)
                _fc_precision_loss = False
                if isinstance(fc, str):
                    if '万' in fc:
                        _fc_precision_loss = True
                        fc = float(fc.replace('万', '')) * 10000
                    else:
                        fc = float(fc)
                fc_int = int(fc)
                if _fc_precision_loss:
                    logger.info(
                        f"[HTTP Recovery] follower_count is 万-rounded "
                        f"(\"{user_data.get('follower_count')}\") — "
                        f"precision loss up to ±500"
                    )
                else:
                    # Precise integer — track for HTTP-based follower delta
                    # Baseline: capture the FIRST precise value seen this session.
                    # Once set, NEVER lower it — the Douyin API fluctuates ±20-30
                    # between calls (caching/server differences), which would
                    # artificially shrink the delta if we reset to lower values.
                    # Only http_follow_last should update (to the highest seen).
                    if not getattr(self, '_http_first_seen', False):
                        self._http_first_seen = True
                        self.http_follow_first = fc_int
                        # Also set http_follow_last to match on first capture
                        if fc_int > self.http_follow_last:
                            self.http_follow_last = fc_int
                        if self.verbose:
                            logger.info(f"[HTTP Follow] http_follow_first={fc_int:,} (first this session)")
                    # Track the anchor's actual total follower count (authoritative)
                    # Direct assignment (not max) so real follower losses are reflected.
                    self.follower_count = fc_int
                    if fc_int > self.http_follow_last:
                        self.http_follow_last = fc_int
                        if self.verbose:
                            logger.info(f"[HTTP Follow] http_follow_last={fc_int:,}")
                # Use API value for follower_after (not WS followCount ordinal).
                # The API is the authoritative source for the anchor's actual total.
                self.follower_after = self.follower_count
                logger.info(f"[HTTP Refresh] WS follower={self.ws_follow_last:,}, "
                            f"API={fc_int:,}, follower_after={self.follower_after:,}, "
                            f"http_delta={max(0, self.http_follow_last - self.http_follow_first):,}")
            
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
            with _suppress_stdout():
                room_info = DouyinAPI.get_live_info(auth, self.live_id)

            # Fall back to HTTP-parsed room_info (from check_status via handle_live)
            # when get_live_info fails — its regex-based parsing is fragile.
            if room_info is None:
                room_info = getattr(self, '_http_room_info', None)
                if room_info:
                    logger.info("LiveStatsRecorder: using HTTP-parsed room_info as fallback "
                                f"(room_id={room_info.get('room_id')}, user_id={room_info.get('user_id')})")
            if room_info is None:
                logger.error("LiveStatsRecorder: get_live_info returned None and no HTTP fallback available")
                return
            room_id = room_info.get('room_id')
            user_id = room_info.get('user_id')
            if not room_id or not user_id:
                logger.error(f"LiveStatsRecorder: incomplete room data: room_id={room_id}, user_id={user_id}")
                return
            ttwid = room_info.get('ttwid', '')
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
            # Distinguish intentional stop (stop() called) from unexpected
            # disconnect or exception.  The _on_close callback may or may not
            # have fired depending on how run_forever exited — always check
            # _intentional_stop (set by stop()) to decide.
            if not self._intentional_stop:
                # Unexpected exit — mark disconnected so the main loop's
                # recovery/reconnect logic triggers on next cycle.
                self.ws_disconnected = True
                self.ws_disconnect_time = datetime.now()
                logger.warning("[StatsRecorder] WebSocket disconnected unexpectedly. Main loop will attempt recovery.")
            elif not self.ws_disconnected and self.stream_end_time is None:
                # Intentional stop — safe to run end-of-stream cleanup.
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

                # Retry the API up to 3 times if it returns 0 or 万-rounded.
                # The WS SocialMessage followCount is the fallback if the
                # API never returns a precise value.
                _api_attempts = 0
                _api_value = 0
                _api_precision_loss = True

                while _api_attempts < 3 and (_api_value == 0 or _api_precision_loss):
                    _api_attempts += 1
                    with _suppress_stdout():
                        user_info = DouyinAPI.get_user_info(auth, user_url)
                    user_data = user_info.get('user', {})
                    if _api_attempts == 1:
                        self.anchor_nickname = user_data.get('nickname', '')
                    fc = user_data.get('follower_count', 0)
                    _fc_precision_loss = False
                    if isinstance(fc, str):
                        if '万' in fc:
                            _fc_precision_loss = True
                            fc = float(fc.replace('万', '')) * 10000
                        else:
                            fc = float(fc)
                    _api_value = int(fc) if fc else 0
                    _api_precision_loss = _fc_precision_loss
                    if _api_attempts < 3 and (_api_value == 0 or _api_precision_loss):
                        time.sleep(1)  # brief pause between retries

                # Use API value if a precise non-zero result was obtained
                if self.follower_before == 0 and not _api_precision_loss and _api_value > 0:
                    self.follower_before = _api_value
                    if self.verbose:
                        logger.info(
                            f"[StatsRecorder] follower_before={_api_value:,} "
                            f"(from API /aweme/v1/web/user/profile/other/ "
                            f"after {_api_attempts} attempt(s))"
                        )
                elif _api_precision_loss:
                    logger.info(
                        f"[StatsRecorder] API follower_count={_api_value:,} "
                        f"(万-rounded after {_api_attempts} attempts — "
                        f"WS SocialMessage will be used as fallback)"
                    )
                elif self.follower_before > 0:
                    logger.debug(
                        f"[StatsRecorder] API follower_count={_api_value:,} "
                        f"(precise, but follower_before already set to {self.follower_before:,})"
                    )

                # Fan club baseline comes exclusively from the first
                # WebcastFansclubMessage (type=2 join) total_members field.
                # The HTTP API user_data does not expose this count.

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
            with _suppress_stdout():
                room_info = DouyinAPI.get_live_info(auth, self.live_id)

            if room_info is None:
                room_info = getattr(self, '_http_room_info', {})
            sec_uid = room_info.get('sec_uid', '') or fallback_sec_uid
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                with _suppress_stdout():
                    user_info = DouyinAPI.get_user_info(auth, user_url)

                user_data = user_info.get('user', {})
                fc = user_data.get('follower_count', 0)
                if isinstance(fc, str):
                    fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
                # Use API value for follower_after (authoritative).
                # The WS followCount is an event ordinal, not the actual total.
                fc_int = int(fc) if fc else 0
                if fc_int > 0:
                    self.follower_count = fc_int
                self.follower_after = self.follower_count
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
            with _suppress_stdout():
                user_info = DouyinAPI.get_user_info(auth, user_url)

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
        # Preserve original start time across reconnects so duration reflects
        # the full broadcast, not just the most recent connection.
        if self.stream_start_time is None:
            self.stream_start_time = datetime.now()
        # Reset fresh member tracking on reconnect so the viewer-count fallback
        # doesn't return stale values from the old connection.
        self._fresh_member_count = 0
        self._fresh_member_update_time = None
        threading.Thread(target=self._ping, args=(ws,), daemon=True).start()
        threading.Thread(target=self._periodic_summary, args=(ws,), daemon=True).start()
        # Write initial stats immediately so the dashboard flips to "live" right away
        self._write_live_stats_json()
        if self.verbose:
            logger.info(f"[StatsRecorder] Started recording at {self.stream_start_time.strftime('%H:%M:%S')}")

    def _ping(self, ws):
        while not self._stop_event.is_set():
            # WS silence detection: if no data message for 90s, force close so
            # the main loop triggers reconnection/recovery.  This handles the
            # case where the TCP connection stays alive but Douyin stops pushing
            # ALL messages.
            if self._last_data_message_time is not None:
                silence = (datetime.now() - self._last_data_message_time).total_seconds()
                if silence > 90:
                    logger.warning(f"[StatsRecorder] No data message for {silence:.0f}s — closing WS for reconnect")
                    try:
                        ws.close()
                    except Exception:
                        pass
                    self.ws_disconnected = True
                    break
            # RoomStatsMessage staleness: if displayValue hasn't updated in 120s
            # even though other message types are flowing, try HTTP fallback
            # first.  If that succeeds, keep the WS.  Only disconnect if HTTP
            # fallback also fails to get a fresh viewer count.
            if self._last_viewer_update is not None:
                roomstat_age = (datetime.now() - self._last_viewer_update).total_seconds()
                if roomstat_age > 90:
                    tried_http = getattr(self, '_http_viewer_attempted', False)
                    if not tried_http:
                        # First time stale — try HTTP API for viewer count
                        logger.info(f"[StatsRecorder] RoomStatsMessage stale ({roomstat_age:.0f}s) — trying HTTP fallback")
                        self._http_viewer_attempted = True
                        try:
                            self.fetch_cumulative_via_http(mark_recovery=getattr(self, 'http_cumulative_recovery', False))
                        except Exception:
                            pass
                        # Re-check if HTTP fallback refreshed _last_viewer_update
                        if self._last_viewer_update is not None:
                            roomstat_age = (datetime.now() - self._last_viewer_update).total_seconds()
                if roomstat_age > 120:
                    logger.warning(f"[StatsRecorder] No RoomStatsMessage for {roomstat_age:.0f}s — closing WS for reconnect")
                    self._http_viewer_attempted = False
                    try:
                        ws.close()
                    except Exception:
                        pass
                    self.ws_disconnected = True
                    break
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

            # ── Dedicated follower_count refresh (every 5 minutes) ────
            # Periodically fetch the anchor's actual follower count from the
            # user profile API.  This is more reliable than the WS followCount
            # ordinal and avoids the http_follow_first fluctuation problem.
            if counter > 0 and counter % 5 == 0:
                try:
                    self._refresh_follower_count()
                except Exception:
                    pass

            # Write CSV/JSON BEFORE printing so crash recovery data is
            # always persisted even if stdout is disconnected.
            self._write_live_stats_json()
            self._write_stats_csv()

            try:
                print(f"\n{'─'*60}")
                print(f"  📊 直播中概览 (已直播{counter}分钟) - {datetime.now().strftime('%H:%M:%S')}")
                print(f"{'─'*60}")

                avg_viewers = sum(self.viewer_samples) // len(self.viewer_samples) if self.viewer_samples else 0
                peak = self.peak_viewers
                likes = self.total_likes
                badge = self.light_badges
                gifts = len(self.gift_events)

                views_wan = self._try_get_wan('观看')
                likes_wan = self._try_get_wan('点赞')
                peak_wan = self._try_get_wan('最高在线')
                current_online = self.viewer_samples[-1] if self.viewer_samples else 0

                print(f"  👀 当前在线：{fmt_wan(current_online)}人")
                if self.cumulative_views:
                    print(f"  👁 场观：{fmt_wan(self.cumulative_views)}人")
                print(f"  👍 点赞：{fmt_wan(likes) if likes > 0 else (likes_wan or '?')}")
                print(f"  🔥 最高在线：{peak_wan or fmt_wan(peak)}")
                print(f"  📊 平均在线：{fmt_wan(avg_viewers)}")
                print(f"  📈 新增关注：{self._get_new_follows()}")
                print(f"  🌟 新增粉丝团：{self._get_fan_club_joins()}人")
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
            except Exception:
                pass  # stdout might be disconnected in background

    def _write_live_stats_json(self, live=True):
        """Write current stats to a JSON file for the web dashboard.

        Uses atomic write (temp file + rename) to prevent the web server
        from reading a half-written file.
        """
        try:
            # Seed override: recover stats from gap periods after process restart.
            # seed_override.json persists across restarts; applied ONCE per process
            # lifetime. Live WS/API values take over after bootstrap.
            # Cumulative counters use max() so live events are never decreased.
            import os as _os_seed
            _seed_path = _os_seed.path.join(_os_seed.path.dirname(__file__), 'seed_override.json')
            if _os_seed.path.exists(_seed_path):
                try:
                    with open(_seed_path, 'r') as _sf:
                        _seed = json.load(_sf)
                    # Staleness guard: if the seed's stream_start_time is too old,
                    # it belongs to a previous stream — delete and ignore it.
                    # This prevents yesterday's seed from polluting today's new stream.
                    _MAX_SEED_AGE_HOURS = 6
                    _seed_stale = False
                    if _seed.get('stream_start_time'):
                        try:
                            _seed_start = datetime.fromisoformat(_seed['stream_start_time'])
                            _seed_age_hours = (datetime.now() - _seed_start).total_seconds() / 3600
                            if _seed_age_hours > _MAX_SEED_AGE_HOURS:
                                _seed_stale = True
                                logger.warning(
                                    f'[SeedOverride] Seed is {_seed_age_hours:.1f}h old '
                                    f'(>{_MAX_SEED_AGE_HOURS}h limit) — discarding stale seed '
                                    f'from previous stream'
                                )
                        except Exception:
                            pass

                    # One-shot guard: seed applies only on first call, then
                    # live WS events and periodic HTTP refreshes take over.
                    if not getattr(self, '_seed_applied', False) and not _seed_stale:
                        self._seed_applied = True
                        if _seed.get('stream_start_time'):
                            seed_start = datetime.fromisoformat(_seed['stream_start_time'])
                            if self.stream_start_time is None or seed_start < self.stream_start_time:
                                self.stream_start_time = seed_start
                        if _seed.get('follower_before'):
                            self.follower_before = int(_seed['follower_before'])
                        if _seed.get('follower_after'):
                            self.follower_after = int(_seed['follower_after'])
                        if _seed.get('fan_club_start_count'):
                            self.fan_club_start_count = int(_seed['fan_club_start_count'])
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
                        if _seed.get('fan_club_joins'):
                            self.fan_club_joins = max(self.fan_club_joins, int(_seed['fan_club_joins']))
                        if _seed.get('http_follow_first'):
                            # Direct assignment (not max) — the seed value is the
                            # authoritative baseline. CSV recovery may have set a
                            # higher value from the last row, which would defeat
                            # the purpose of a seed-provided baseline.
                            self.http_follow_first = int(_seed['http_follow_first'])
                            self._http_first_seen = True
                        if _seed.get('http_follow_last'):
                            self.http_follow_last = max(self.http_follow_last, int(_seed['http_follow_last']))
                        if _seed.get('ws_follow_first'):
                            self.ws_follow_first = max(self.ws_follow_first, int(_seed['ws_follow_first']))
                        if _seed.get('ws_follow_last'):
                            self.ws_follow_last = max(self.ws_follow_last, int(_seed['ws_follow_last']))
                        if _seed.get('follower_count'):
                            self.follower_count = max(self.follower_count, int(_seed['follower_count']))
                        if _seed.get('avg_override') and not hasattr(self, '_avg_override'):
                            self._avg_override = int(_seed['avg_override'])
                        if _seed.get('seed_viewer_samples'):
                            _seed_samples = _seed['seed_viewer_samples']
                            if isinstance(_seed_samples, list) and _seed_samples:
                                if not getattr(self, '_seed_samples_applied', False):
                                    self.viewer_samples = list(_seed_samples) + self.viewer_samples
                                    self._last_minute_sample = None
                                    self._seed_samples_applied = True
                                    logger.info('[SeedOverride] Seeded ' + str(len(_seed_samples)) + ' viewer samples for gap period')
                        _os_seed.remove(_seed_path)
                        logger.info('[SeedOverride] Applied: start=' + str(self.stream_start_time) + ', fb=' + str(self.follower_before) + ', fa=' + str(self.follower_after) + ', peak=' + str(getattr(self,'peak_viewers',0)) + ', badges=' + str(self.light_badges) + ', fc_start=' + str(self.fan_club_start_count) + ', vsamples=' + str(len(self.viewer_samples)))
                    else:
                        # Seed already applied this session. Delete the file
                        # so it doesn't keep triggering reads (harmless but wasteful).
                        _os_seed.remove(_seed_path)
                except Exception as _se:
                    logger.warning('[SeedOverride] Failed: ' + str(_se))
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
                    "follower_count": self.follower_count,
                    "fan_club_joins": self._get_fan_club_joins(),
                    "fan_club_delta": max(0, self.fan_club_end_count - self.fan_club_start_count) if self.fan_club_start_count > 0 else 0,
                    "fan_club_event_joins": self.fan_club_joins,
                    "fan_club_gift_joins": self.fan_club_gift_joins,
                    "light_badges": self.light_badges,
                    "current_viewers": self._get_current_viewers(),
                    "peak_viewers": self.peak_viewers,
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
                    "follower_count": self.follower_count,
                    "fan_club_joins": self._get_fan_club_joins(),
                    "fan_club_delta": max(0, self.fan_club_end_count - self.fan_club_start_count) if self.fan_club_start_count > 0 else 0,
                    "fan_club_event_joins": self.fan_club_joins,
                    "fan_club_gift_joins": self.fan_club_gift_joins,
                    "light_badges": self.light_badges,
                    "current_viewers": self._get_current_viewers(),
                    "peak_viewers": self.peak_viewers,
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

    def _write_stats_csv(self):
        """Append one row to the CSV time-series file.

        Called every 60s from _periodic_summary().  Uses atomic append
        (write temp + rename) so the web dashboard can safely tail the file.
        Writes a header row if the file doesn't exist yet.
        """
        import csv as _csv
        import os as _os_csv
        try:
            _exists = _os_csv.path.exists(STATS_CSV_FILE)
            _tmp = STATS_CSV_FILE + '.tmp'

            _expected_header = (
                'timestamp,live_id,anchor_nickname,'
                'current_viewers,peak_viewers,cumulative_views,'
                'total_likes,follower_before,follower_after,'
                'follower_count,follower_delta,ws_follow_first,ws_follow_last,'
                'http_follow_first,http_follow_last,'
                'fan_club_start_count,fan_club_end_count,fan_club_joins,'
                'fan_club_gift_joins,light_badges,light_badge_day,'
                'gift_event_count,'
                'gift_top1_name,gift_top1_count,'
                'gift_top2_name,gift_top2_count,'
                'gift_top3_name,gift_top3_count,'
                'gift_top4_name,gift_top4_count,'
                'gift_top5_name,gift_top5_count,'
                'ws_connected,'
                'stream_duration_s,stream_start_time,member_count,'
                'viewer_sample_count,viewer_sample_sum'
            )

            # If file exists, copy to temp then append; if header is stale
            # (format changed), rewrite with new header and all data rows.
            if _exists:
                with open(STATS_CSV_FILE, 'r', encoding='utf-8', newline='') as _rf:
                    _first_line = _rf.readline().rstrip('\r\n')
                    _rest = _rf.read()
                with open(_tmp, 'w', encoding='utf-8', newline='') as _wf:
                    if _first_line != _expected_header:
                        # Header format changed — rewrite with new header
                        # followed by existing data rows (skip the old header)
                        _wf.write(_expected_header + '\n')
                        _wf.write(_rest)
                        logger.info("[CSV] Migrated header to new format "
                                    f"({len(_expected_header.split(','))} columns)")
                    else:
                        _wf.write(_first_line + '\n')
                        _wf.write(_rest)
            else:
                # Ensure empty temp file
                open(_tmp, 'w').close()

            with open(_tmp, 'a', encoding='utf-8', newline='') as _af:
                _writer = _csv.writer(_af)
                if not _exists:
                    _writer.writerow(_expected_header.split(','))
                _duration = 0
                if self.stream_start_time:
                    _duration = int(
                        (datetime.now() - self.stream_start_time).total_seconds()
                    )
                _writer.writerow([
                    datetime.now().isoformat(),
                    self.live_id,
                    self.anchor_nickname,
                    self._get_current_viewers(),
                    self.peak_viewers,
                    self.cumulative_views,
                    self.total_likes,
                    self.follower_before,
                    self.follower_after,
                    self.follower_count,
                    self._get_new_follows(),
                    self.ws_follow_first,
                    self.ws_follow_last,
                    self.http_follow_first,
                    self.http_follow_last,
                    self.fan_club_start_count,
                    self.fan_club_end_count,
                    self.fan_club_joins,
                    self.fan_club_gift_joins,
                    self.light_badges,
                    getattr(self, '_light_badge_day', ''),
                    len(self.gift_events),
                    # Top 5 gifts by count (excluding action gifts)
                    *self._gift_top5_csv(),
                    0 if self.ws_disconnected else 1,
                    _duration,
                    self.stream_start_time.isoformat() if self.stream_start_time else '',
                    self.member_count,
                    len(self.viewer_samples),
                    sum(self.viewer_samples),
                ])
            _os_csv.replace(_tmp, STATS_CSV_FILE)
        except Exception as _e:
            logger.debug(f"[StatsRecorder] Failed to write stats CSV: {_e}")

    def _on_message(self, ws, message):
        # Track last data message time for WS silence detection
        self._last_data_message_time = datetime.now()
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
                        # Track absolute follower count from WebSocket payload.
                        # This is the anchor's precise total at the moment of
                        # this follow event — far more accurate than the API's
                        # 万-rounded follower_count field.
                        fc = msg.followCount
                        if fc > 0:
                            # ── WS session reset detection ─────────────────────
                            # After a crash recovery, ws_follow_first may carry a
                            # value from the OLD WebSocket session.  If the new
                            # session's followCount values are ALL lower than the
                            # recovered ws_follow_first, the WS session was reset
                            # and old values don't carry over.  Reset to 0 so
                            # http_delta covers the gap until WS followCount
                            # starts updating again in this session.
                            if self.ws_follow_first > 0 and fc < self.ws_follow_first:
                                if self.verbose:
                                    logger.info(
                                        f"[FOLLOW] WS session reset detected: "
                                        f"old_first={self.ws_follow_first:,}, "
                                        f"new_fc={fc:,} — resetting WS counters"
                                    )
                                self.ws_follow_first = 0
                                self.ws_follow_last = 0
                            # ── End reset detection ────────────────────────────
                            if self.ws_follow_first == 0:
                                self.ws_follow_first = fc
                                # Only set follower_before from WS if it wasn't
                                # already set by seed_override or CSV recovery.
                                # This preserves manual corrections across restarts.
                                if self.follower_before == 0:
                                    self.follower_before = fc
                                if self.verbose:
                                    logger.info(f"[FOLLOW] ws_follow_first={fc:,} (follower_before preserved as {self.follower_before:,})")
                            if fc > self.ws_follow_last:
                                self.ws_follow_last = fc
                                self.ws_follow_last_time = datetime.now()
                                    # ws_follow_last_time only updates when the
                                    # VALUE changes, not when a message arrives.
                                    # This prevents "message spam with same value"
                                    # from masking a truly stalled followCount.
                        if self.verbose:
                            logger.info(f"[FOLLOW] user={msg.user.nickname}, "
                                        f"followCount={fc:,}, "
                                        f"ws_delta={self.ws_follow_last - self.ws_follow_first}")

                elif method == 'WebcastMemberMessage':
                    msg = Live_pb2.MemberMessage()
                    msg.ParseFromString(payload)
                    self.new_members += 1
                    # Track max memberCount from protobuf — this is the cumulative
                    # audience member count (room joins), used as "新增成员" in summaries.
                    if msg.memberCount > self.member_count:
                        self.member_count = msg.memberCount
                    # Track latest memberCount for viewer fallback.
                    # This is the LATEST value, NOT max() — max() would cause
                    # the fallback to never decrease after a WS reconnect.
                    self._fresh_member_count = msg.memberCount
                    self._fresh_member_update_time = datetime.now()
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
                    # Protobuf dump: enabled only via env var PROTOBUF_DUMP=1
                    # Dumps the first 20 unique gift payloads to stdout for debugging
                    # protobuf field layout.  Disabled by default because each dump
                    # is 6-17KB of protobuf bytes per gift type.
                    if (os.environ.get('PROTOBUF_DUMP', '').strip() in ('1', 'true')
                            and not hasattr(self, '_dumped_gifts')):
                        self._dumped_gifts = set()
                        logger.info(f"[PROTO-DUMP] Dumper enabled via PROTOBUF_DUMP=1")
                    if getattr(self, '_dumped_gifts', None) is not None:
                        if gift_name not in self._dumped_gifts:
                            self._dumped_gifts.add(gift_name)
                            logger.info(f"[PROTO-DUMP] Dumping fields for gift: {gift_name} (payload={len(payload)} bytes)")
                            dump_all_protobuf_fields(payload, f'GiftMessage:{gift_name}')
                            logger.info(f"[PROTO-DUMP] Done dumping {gift_name}")
                            if len(self._dumped_gifts) >= 20:
                                logger.info(f"[PROTO-DUMP] Dumped 20 unique gift types — disabling dumper")
                    self.gift_events.append({
                        'user': msg.user.nickname,
                        'gift': gift_name,
                        'count': combo,
                    })
                    # Cap gift_events at 20,000 to prevent unbounded memory growth
                    # on long streams.  This discards the oldest entries but
                    # preserves the summary (top-N) via CSV persistence.
                    MAX_GIFT_EVENTS = 20000
                    if len(self.gift_events) > MAX_GIFT_EVENTS:
                        self.gift_events = self.gift_events[-MAX_GIFT_EVENTS:]
                    # Gifts that represent badge light-up actions.
                    # A user can only contribute ONE badge per natural day
                    # (midnight-to-midnight).  After 12am, the same user
                    # can contribute another.
                    if gift_name in ('点点星光', '粉丝团灯牌', '闪烁星河', '点亮粉丝团'):
                        today = _beijing_now().strftime('%Y%m%d')
                        # Evict entries from previous days to bound memory
                        if getattr(self, '_light_badge_day', '') != today:
                            self._light_badge_users = {
                                (u, d) for (u, d) in self._light_badge_users
                                if d == today
                            }
                            self._light_badge_day = today
                        # Use msg.user.id from the fully-parsed GiftMessage (not the
                        # raw-protobuf uid from parse_gift_dedup_key) — the raw parser
                        # may return uid=0 when the User sub-message is unparseable,
                        # causing all such gifts to share uid=0 and silently undercount badges.
                        _badge_uid = getattr(msg.user, 'id', 0) or uid
                        if (_badge_uid, today) not in self._light_badge_users:
                            self._light_badge_users.add((_badge_uid, today))
                            self.light_badges += 1
                            if self.verbose:
                                logger.info(f"[BADGE] user_id={_badge_uid} — light badge #{self.light_badges} today={today}")
                        else:
                            if self.verbose:
                                logger.info(f"[BADGE] user_id={_badge_uid} already counted today — skipping")
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
                    MAX_DISPLAY_LONG = 1440  # ~1 hour at 2.5s intervals
                    if len(self.display_long_history) > MAX_DISPLAY_LONG:
                        self.display_long_history = self.display_long_history[-MAX_DISPLAY_LONG:]
                    self.current_viewers = msg.displayValue
                    self._last_viewer_update = datetime.now()
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
                        # Cap viewer_samples at 1,440 (24 hours of 1-min samples)
                        # to prevent unbounded memory growth on long-running streams.
                        MAX_VIEWER_SAMPLES = 1440
                        if len(self.viewer_samples) > MAX_VIEWER_SAMPLES:
                            self.viewer_samples = self.viewer_samples[-MAX_VIEWER_SAMPLES:]

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

                    for key, (val, has_wan) in parsed.items():
                        actual = int(val * 10000) if has_wan else int(val)
                        if key == '点赞':
                            self.total_likes = max(self.total_likes, actual)
                        elif key == '观看':
                            self.cumulative_views = max(self.cumulative_views, actual)
                        # NOTE: displayLong "灯牌" is the CHANNEL'S ALL-DAY cumulative total.
                        # Do NOT max() it into the stream-specific light_badges counter.
                        # Store separately so the summary can use it as a fallback only.

                elif method == 'WebcastFansclubMessage':
                    # field 2 = type (1=upgrade, 2=join)
                    # field 3 = content ("恭喜 xxx 成为粉丝团第{N}名成员")
                    # field 4 = User
                    # total_members extracted from content via regex in
                    # parse_fansclub_msg()
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
                        self.fan_club_end_count = max(self.fan_club_end_count, m['total_members'])
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


                elif method == 'WebcastRoomUserSeqMessage':
                    # Parse RoomUserSeqMessage to extract field 3 (current online viewers)
                    # and field 11 (cumulative views).  Field 3 is the actual viewer count
                    # that Douyin's own UI uses — more accurate than RoomStatsMessage.
                    try:
                        online_now, cum_views = parse_room_user_seq_msg(payload)
                        if online_now > 0:
                            self.current_viewers = online_now
                            self._last_viewer_update = datetime.now()
                            if online_now > self.peak_viewers:
                                self.peak_viewers = online_now
                                self.peak_viewer_time = datetime.now().strftime('%H:%M:%S')
                            # Sample viewer count at most once per minute for
                            # time-weighted average, same as RoomStatsMessage.
                            _rus_now = datetime.now()
                            if (self._last_minute_sample is None or
                                (_rus_now - self._last_minute_sample).total_seconds() >= 60):
                                self.viewer_samples.append(online_now)
                                self._last_minute_sample = _rus_now
                                MAX_VIEWER_SAMPLES = 1440
                                if len(self.viewer_samples) > MAX_VIEWER_SAMPLES:
                                    self.viewer_samples = self.viewer_samples[-MAX_VIEWER_SAMPLES:]
                            if self.verbose:
                                logger.info(f"[ROOMUSER] 当前在线 = {online_now:,}")
                        if cum_views > self.cumulative_views:
                            self.cumulative_views = cum_views
                            if self.verbose:
                                logger.info(f"[ROOMUSER] 场观(累计) = {cum_views:,}")
                    except Exception as e:
                        if self.verbose:
                            logger.debug(f"[ROOMUSER] parse failed: {e}")

        except Exception:
            pass

    def _on_error(self, ws, error):
        logger.error(f"[StatsRecorder] WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        if self._intentional_stop:
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
            print(f"    follower_before   = {self.follower_before:,}")
            print(f"    follower_after    = {self.follower_after:,}")
            print(f"    new_members       = {self.new_members}")
            print(f"    member_count      = {self.member_count:,}")
            print(f"    light_badges      = {self.light_badges}")
            print(f"    fan_club_joins    = {self._get_fan_club_joins()} (events={self.fan_club_joins}, start={self.fan_club_start_count}, end={self.fan_club_end_count})")
            print(f"    peak_viewers      = {self.peak_viewers:,}")
            print(f"    viewer_samples    = {len(self.viewer_samples)}")
            print(f"    gift_events       = {len(self.gift_events):,}")
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

        _delta = self._get_new_follows()
        _fb = self.follower_before
        print(f"📈 关注涨幅度：{fmt_wan(_fb)} → {fmt_wan(_fb + _delta)}（+{fmt_wan(_delta)}）")

        peak_wan = self._try_get_wan('最高在线')
        if peak_wan:
            print(f"🔥 最高在线：{peak_wan}人")
        elif self.peak_viewers:
            print(f"🔥 最高在线：{fmt_wan(self.peak_viewers)}人")
        else:
            print(f"🔥 最高在线：--")

        print(f"📊 平均在线：{fmt_wan(avg_viewers)}人")

        # Fan club joins: content-based delta (end - start) with WS event fallback
        joins = self._get_fan_club_joins()
        if joins > 0:
            if self.fan_club_start_count > 0 and self.fan_club_end_count > 0:
                print(f"🌟 新增粉丝团：{fmt_wan(self.fan_club_start_count)} → {fmt_wan(self.fan_club_end_count)}（+{fmt_wan(joins)}）")
            else:
                print(f"🌟 新增粉丝团：+{fmt_wan(joins)}人")
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

    def _gift_top5_csv(self) -> list:
        """Return [name1, count1, …, name5, count5] for CSV persistence."""
        summary = {}
        for g in self.gift_events:
            name = g['gift']
            if self._is_action_gift(name):
                continue
            summary[name] = summary.get(name, 0) + g['count']
        top = sorted(summary.items(), key=lambda x: -x[1])[:5]
        result = []
        for name, cnt in top:
            result.append(name)
            result.append(str(cnt))
        # Pad with empty strings if fewer than 5 gift types
        while len(result) < 10:
            result.append('')
        return result

    def _ws_follow_stalled(self) -> bool:
        """Return True if WS followCount hasn't updated in >5 minutes."""
        if self.ws_follow_last_time is None:
            return True  # never received a usable followCount
        staleness = (datetime.now() - self.ws_follow_last_time).total_seconds()
        return staleness > 300  # 5 min without any followCount update

    def _refresh_follower_count(self):
        """Periodically refresh the anchor's actual follower count from the HTTP API.

        Called every 5 min from _periodic_summary().  Updates follower_count
        and http_follow_last (highest seen).  Skips 万-rounded values to
        avoid ±500 precision loss.
        """
        try:
            from builder.auth import DouyinAuth
            auth = DouyinAuth()
            auth.perepare_auth(self.cookie_str, "", "")
            sec_uid = getattr(self, '_http_room_info', {}).get('sec_uid', '')
            if not sec_uid:
                fallback = getattr(self, '_anchor_sec_uid', '')
                # Try to get from the last http_room_info on the StreamMonitor
                sec_uid = fallback or ''
            if not sec_uid:
                return
            user_url = f"https://www.douyin.com/user/{sec_uid}"
            with _suppress_stdout():
                user_info = DouyinAPI.get_user_info(auth, user_url)
            if not isinstance(user_info, dict):
                return
            user_data = user_info.get('user', {})
            fc = user_data.get('follower_count', 0)
            if not fc:
                return
            _fc_precision_loss = False
            if isinstance(fc, str):
                if '万' in fc:
                    _fc_precision_loss = True
                    fc = float(fc.replace('万', '')) * 10000
                else:
                    fc = float(fc)
            if _fc_precision_loss:
                logger.debug(f"[FollowerRefresh] Skipped 万-rounded value: {fc:,.0f}")
                return
            fc_int = int(fc)
            # Track latest API value as the authoritative follower count
            # (NOT max() — max would freeze at yesterday's peak and never
            # reflect real follower losses or API refreshes).
            self.follower_count = fc_int
            # Track highest seen separately for peak analysis
            if fc_int > self.http_follow_last:
                self.http_follow_last = fc_int
            if self.verbose:
                logger.info(f"[FollowerRefresh] follower_count={fc_int:,} (delta={self._get_new_follows():,})")
        except Exception as e:
            logger.warning(f"[FollowerRefresh] Failed: {e}")

    def _get_new_follows(self) -> int:
        """New followers — HTTP API authoritative, WS supplements in real-time.

        max(http_delta, ws_delta) ensures:
          - HTTP captures the authoritative total periodically (every 5 min)
          - WS captures individual follow events in real-time between HTTP refreshes
          - If WS followCount stalls (>5 min without update), WS delta is dropped
            and only HTTP is used until WS recovers.

        The baseline for HTTP delta is http_follow_first (first precise API value)
        if available, which is immune to the ±500 万-rounding error.  Falls back
        to follower_before if no precise value has been captured yet.
        """
        baseline = self.http_follow_first if self.http_follow_first > 0 else self.follower_before
        http_delta = max(0, self.follower_count - baseline)
        ws_delta = 0
        if not self._ws_follow_stalled():
            ws_delta = max(0, self.ws_follow_last - self.ws_follow_first)
        return max(http_delta, ws_delta)

    def _get_fan_club_joins(self) -> int:
        """Fan club joins during this stream.

        Authoritative: content-based delta from FansclubMessage.
          total_members parsed from the content string is the absolute
          member count.  Delta = end - start gives the precise join count
          for this stream, immune to duplicate WS messages.

        Fallback: if start_count wasn't captured (reconnect gap), use
          event counter or gift counter as a floor.
        """
        if self.fan_club_start_count > 0 and self.fan_club_end_count > 0:
            return max(0, self.fan_club_end_count - self.fan_club_start_count)
        return max(self.fan_club_joins, self.fan_club_gift_joins)

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

        # Evict stale entries in-place (not via dict replacement) to avoid
        # a race: if _should_count_gift() adds an entry between the iteration
        # and the replacement, the new entry would be silently lost, causing
        # double-counting on the next duplicate frame.
        if not hasattr(self, '_gift_dedup_snapshot'):
            self._gift_dedup_snapshot = {}

        evicted = 0
        for key in list(self._gift_dedup.keys()):
            prev_rc = self._gift_dedup_snapshot.get(key, -1)
            if self._gift_dedup.get(key, 0) == prev_rc:
                del self._gift_dedup[key]
                evicted += 1

        # Update snapshot from surviving entries (safe after eviction)
        self._gift_dedup_snapshot = dict(self._gift_dedup)
        self._gift_dedup_last_cleanup = now

        if evicted > 0:
            logger.debug(f"[Dedup] Evicted {evicted} stale entries, "
                         f"{len(self._gift_dedup)} active entries remain")

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

    # Max time to keep returning cached status after cookie expiry before
    # forcing OFFLINE so a real stream-end event isn't lost.
    _COOKIE_EXPIRY_GRACE_SECONDS = 300  # 5 minutes

    def __init__(self, live_id: str, cookie_str: str = ""):
        self.live_id = live_id
        self.cookie = self._parse_cookie(cookie_str) if cookie_str else {}
        self._cookie_expired_since: Optional[float] = None

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
                # Strip s_v_web_id from page request — Douyin returns empty
                # responses when this device-fingerprint cookie is present
                # on a non-browser HTTP client.  The cookie is still needed
                # for API calls (DouyinAuth) but NOT for the HTML page fetch.
                page_cookies = {
                    k: v for k, v in self.cookie.items()
                    if k != "s_v_web_id"
                }
                resp = requests.get(url, headers=self.HEADERS, cookies=page_cookies, verify=False, timeout=15)
                resp.raise_for_status()

                # ── Cookie/session health check ──────────────────────────
                # Detect expired cookies, login redirects, and CAPTCHA pages
                # BEFORE parsing — these are NOT "stream offline" events.
                _final_url = resp.url or ""
                _body_lower = (resp.text or "").lower()
                _is_cookie_expired = (
                    "passport.douyin.com" in _final_url
                    or "sso.douyin.com" in _final_url
                    or "login" in _final_url
                    or "captcha" in _body_lower[:2000]
                    or "verify" in _body_lower[:2000]
                    or len(resp.text or "") < 200  # empty/blocked response
                )
                if _is_cookie_expired:
                    now = time.time()
                    if self._cookie_expired_since is None:
                        self._cookie_expired_since = now
                    elapsed = now - self._cookie_expired_since

                    logger.error(
                        f"[CheckStatus] Cookie appears EXPIRED "
                        f"({elapsed:.0f}s ago) — "
                        f"final_url={_final_url[:100]}, "
                        f"body_len={len(resp.text or '')}"
                    )
                    # Mark cookie unhealthy for the refresher to see
                    try:
                        from cookies import CookieManager
                        CookieManager().mark_unhealthy()
                    except Exception:
                        pass
                    # Telegram alert — cookie expiry is critical
                    self._send_health_alert(
                        f"Douyin cookie EXPIRED for room {self.live_id} "
                        f"(url={_final_url[:80]}, body_len={len(resp.text or '')})",
                        state="douyin_cookie_expired",
                    )

                    # If the cookie has been expired beyond the grace period,
                    # force OFFLINE so a real stream-end doesn't get lost.
                    if elapsed > self._COOKIE_EXPIRY_GRACE_SECONDS:
                        logger.error(
                            f"[CheckStatus] Cookie expired for {elapsed:.0f}s "
                            f"(grace={self._COOKIE_EXPIRY_GRACE_SECONDS}s) — "
                            f"forcing OFFLINE to avoid missing stream-end"
                        )
                        self._cookie_expired_since = None
                        return {"room_status": self.STATUS_OFFLINE}

                    # Otherwise return cached status to avoid false offline
                    last = getattr(self, '_last_status', None)
                    if last:
                        logger.warning(
                            f"[CheckStatus] Returning last known status "
                            f"({last}) — cookie expired {elapsed:.0f}s ago"
                        )
                        return {"room_status": last}
                    return {"room_status": self.STATUS_OFFLINE}

                # Cookie was valid (not redirected to login) — reset expiry timer
                self._cookie_expired_since = None

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
                # Fallback: try broader patterns that may survive HTML changes
                # Pattern 1: Look for RENDER_DATA JSON blob (often contains room status)
                render_data_match = re.search(r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
                if render_data_match:
                    try:
                        import urllib.parse
                        raw = render_data_match.group(1)
                        decoded = urllib.parse.unquote(raw)
                        render_json = json.loads(decoded)
                        # Navigate RENDER_DATA to find room status
                        room_data = (render_json.get('app', {})
                                     .get('initialState', {})
                                     .get('roomStore', {})
                                     .get('roomInfo', {})
                                     .get('room', {}))
                        if room_data:
                            status = str(room_data.get('status', self.STATUS_OFFLINE))
                            title = room_data.get('title', '')
                            rid = str(room_data.get('id_str', ''))
                            anchor_info = room_data.get('anchor', {})
                            sec_uid = anchor_info.get('sec_uid', '')
                            nickname = anchor_info.get('nickname', '')
                            user_id = str(anchor_info.get('id_str', ''))
                            logger.info(f"[Fallback] RENDER_DATA room: status={status}, title={title}")
                            return {
                                "room_id": rid,
                                "user_id": user_id,
                                "user_unique_id": user_id,
                                "anchor_id": user_id,
                                "sec_uid": sec_uid,
                                "ttwid": ttwid,
                                "room_status": status,
                                "room_title": title,
                                "anchor_nickname": nickname,
                            }
                    except Exception as e:
                        logger.debug(f"[Fallback] RENDER_DATA parse failed: {e}")

                # Pattern 2: Try __INITIAL_STATE__ JSON blob (brace-counting parse
                # to handle nested objects — regex {.*?} breaks on nesting)
                init_start = resp.text.find('window.__INITIAL_STATE__')
                if init_start != -1:
                    try:
                        # Find the first '{' after the '=' sign
                        eq_pos = resp.text.find('=', init_start)
                        brace_start = resp.text.find('{', eq_pos)
                        if brace_start != -1:
                            # Count braces to find matching closing brace
                            depth = 1
                            pos = brace_start + 1
                            while depth > 0 and pos < len(resp.text):
                                if resp.text[pos] == '{':
                                    depth += 1
                                elif resp.text[pos] == '}':
                                    depth -= 1
                                pos += 1
                            json_str = resp.text[brace_start:pos]
                            init_json = json.loads(json_str)
                            room = init_json.get('roomInfo', {}).get('room', {})
                            if room:
                                status = str(room.get('status', self.STATUS_OFFLINE))
                                title = room.get('title', '')
                                rid = str(room.get('id_str', ''))
                                anchor = room.get('anchor', {})
                                sec_uid = anchor.get('sec_uid', '')
                                nickname = anchor.get('nickname', '')
                                user_id = str(anchor.get('id_str', ''))
                                logger.info(f"[Fallback] __INITIAL_STATE__ room: status={status}, title={title}")
                                return {
                                    "room_id": rid,
                                    "user_id": user_id,
                                    "user_unique_id": user_id,
                                    "anchor_id": user_id,
                                    "sec_uid": sec_uid,
                                    "ttwid": ttwid,
                                    "room_status": status,
                                    "room_title": title,
                                    "anchor_nickname": nickname,
                                }
                    except Exception as e:
                        logger.debug(f"[Fallback] __INITIAL_STATE__ parse failed: {e}")

                # All patterns failed — log a snippet for debugging
                snippet = resp.text[:500] if resp.text else "(empty response)"
                logger.error(f"[CheckStatus] ALL patterns failed. HTML snippet: {snippet}...")
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

    # Weibo error codes that indicate cookie/auth expiry
    _AUTH_CODES = {"100001", "100005", "21301", "21315", "21332"}

    def __init__(self, web_cookie: str):
        self.web_cookie = web_cookie

    def check_validity(self) -> bool:
        """Verify the Weibo cookie is valid via the desktop site.

        Tests against weibo.com (same endpoint the poster uses) rather than
        the mobile API (m.weibo.cn/api/config), which can reject valid
        cookies that work fine for posting on the desktop site.

        Returns True if the cookie is valid (not redirected to passport/visitor).
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
                "https://weibo.com",
                headers=headers,
                timeout=15,
                allow_redirects=True,
            )
            # If we end up on weibo.com (not passport/visitor/login), cookie is valid
            if "passport.weibo.com" not in resp.url and "visitor" not in resp.url.lower():
                logger.debug("[WeiboPoster] Cookie is valid on weibo.com")
                return True
            logger.warning(
                f"[WeiboPoster] Cookie INVALID — "
                f"weibo.com redirected to {resp.url[:80]}"
            )
            return False
        except Exception as e:
            logger.warning(f"[WeiboPoster] Cookie check error: {e}")
            return False

    def post_tweet(self, content: str, max_retries: int = 3) -> bool:
        # Remove emoji characters before posting to Weibo
        content = strip_emoji(content)
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

        last_error = None
        last_error_code = None
        for attempt in range(max_retries):
            try:
                resp = requests.post(self.POST_URL, headers=headers, data=data, timeout=15)
                result = resp.json()
                if result.get("ok") == 1:
                    logger.info(f"Weibo posted successfully!")
                    return True
                else:
                    error_code = result.get('errno', result.get('code', 'unknown'))
                    error_msg = result.get('msg', str(result))
                    logger.error(f"Weibo API returned error (attempt {attempt + 1}/{max_retries}): errno={error_code}, msg={error_msg}")
                    last_error = f"errno={error_code}"
                    last_error_code = str(error_code)

                    # Rate-limit errors: back off longer
                    if error_code in (100005, 100006, 100100):
                        if attempt < max_retries - 1:
                            wait = 30 * (attempt + 1)
                            logger.warning(f"Weibo rate-limited, waiting {wait}s before retry...")
                            time.sleep(wait)
                            continue
                    elif attempt < max_retries - 1:
                        wait = 5 * (attempt + 1)
                        logger.warning(f"Retrying Weibo post in {wait}s...")
                        time.sleep(wait)
                        continue
            except requests.Timeout:
                last_error = "timeout"
                if attempt < max_retries - 1:
                    wait = 5 * (attempt + 1)
                    logger.warning(f"Weibo post timeout (attempt {attempt + 1}/{max_retries}), retrying in {wait}s...")
                    time.sleep(wait)
                    continue
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    wait = 5 * (attempt + 1)
                    logger.warning(f"Weibo post failed (attempt {attempt + 1}/{max_retries}): {e}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue

        logger.error(f"Failed to post Weibo after {max_retries} attempts: {last_error}")

        # Telegram alert if the final error looks like a cookie/auth issue
        if last_error_code and str(last_error_code) in self._AUTH_CODES:
            self._send_weibo_health_alert(
                f"Weibo post FAILED after {max_retries} retries — "
                f"auth error code={last_error_code}"
            )
        return False

    @staticmethod
    def _send_weibo_health_alert(message: str):
        """Send a Telegram alert for Weibo cookie/auth issues (best-effort)."""
        try:
            from telegram_notifier import TelegramNotifier
            notifier = TelegramNotifier()
            notifier.send(
                f"[StreamMonitor] {message}",
                state="weibo_cookie_expired",
            )
        except Exception:
            pass

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
            "{name} 直播结束\n场观：{views}\n点赞：{likes}\n最高在线：{peak}\n平均在线：{avg}\n关注涨幅度：{followers}\n新增粉丝团：{members}\n点亮灯牌：{badges}\n热门礼物：{gifts}"

        self.checker = DouyinLiveChecker(live_id, dy_cookie_str)
        self.poster = WeiboPoster(weibo_cookie)

        self.stats_recorder = None
        self.log_file = NOTIF_CSV_FILE  # CSV log — efficient O(1) append

        # FIX: Store anchor nickname from HTTP page data as fallback
        # Stats recorder (WebSocket/API) may fail to fetch nickname,
        # but HTTP page HTML parsing always gets it for live_start.
        self._anchor_nickname = ""

        # Track consecutive HTTP offline detections to handle the case where
        # Douyin WebSocket stays connected indefinitely after stream ends.
        # After CONSECUTIVE_OFFLINE_LIMIT consecutive detections, force handle_offline.
        self._consecutive_offline_count = 0
        self.CONSECUTIVE_OFFLINE_LIMIT = 2

        # Cookie reload: periodically re-read cookies.json so mid-stream
        # cookie expiry doesn't silently break monitoring.  The
        # CookieRefresher (separate PM2 process) writes refreshed cookies.
        self._last_cookie_reload = datetime.now()
        self._cookie_reload_interval = 300  # every 5 minutes

        # Telegram health alerts (fire-and-forget, best-effort)
        try:
            from telegram_notifier import TelegramNotifier
            self._telegram = TelegramNotifier()
        except Exception:
            self._telegram = None

        # Validate templates at startup
        self._validate_template(self.live_template, self._get_live_template_keys())
        self._validate_template(self.offline_summary_template, self._get_offline_template_keys())

        logger.info("StreamMonitor initialised: live.douyin.com/{}", live_id)

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
        if r.viewer_samples:
            avg = fmt_wan(sum(r.viewer_samples) // len(r.viewer_samples))
        elif r.peak_viewers > 0:
            avg = fmt_wan(r.peak_viewers)
        else:
            avg = ""

        # ── followers (关注涨幅度) ──
        # Baseline: ws_follow_first (earliest SocialMessage.followCount),
        # falling back to API follower_before.
        # Derive fa = fb + delta so the displayed range is always consistent
        # with the delta number (whether it came from HTTP or WS).
        fb = r.ws_follow_first if r.ws_follow_first > 0 else r.follower_before
        delta = r._get_new_follows()
        if fb > 0:
            followers_str = f"{fmt_wan(fb)} → {fmt_wan(fb + delta)}（+{fmt_wan(delta)}）"
        elif delta > 0:
            followers_str = f"+{fmt_wan(delta)}"
        else:
            followers_str = "0"

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

    def _reload_cookies(self):
        """Re-read cookies.json + weibo_cookies.json and propagate to
        checker, stats recorder, and Weibo poster.

        Safe to call frequently — only reloads if the interval has elapsed.
        Returns True if ANY cookie was reloaded, False otherwise.
        """
        now = datetime.now()
        if (now - self._last_cookie_reload).total_seconds() < self._cookie_reload_interval:
            return False
        self._last_cookie_reload = now

        reloaded_any = False

        # ── Douyin cookie ──────────────────────────────────────────
        try:
            from cookies import CookieManager
            mgr = CookieManager()
            data = mgr.load()
            new_cookie = data.get('cookie_str', '')
            if new_cookie != self.dy_cookie_str:
                if not new_cookie:
                    logger.warning(
                        f"[CookieReload] Douyin cookie JSON has empty cookie_str — "
                        f"keeping current cookie"
                    )
                else:
                    logger.info(f"[CookieReload] Refreshing Douyin cookie "
                                f"(health={data.get('health', 'unknown')}, "
                                f"refresh_count={data.get('refresh_count', 0)})")
                    self.dy_cookie_str = new_cookie
                    self.checker.cookie = DouyinLiveChecker._parse_cookie(new_cookie)
                    if self.stats_recorder and self.stats_recorder.is_running():
                        self.stats_recorder.cookie_str = new_cookie
                    reloaded_any = True
        except Exception as e:
            logger.warning(f"[CookieReload] Douyin cookie reload failed: {e}")

        # ── Weibo cookie ───────────────────────────────────────────
        try:
            from cookies import WeiboCookieManager
            wmgr = WeiboCookieManager()
            wdata = wmgr.load()
            new_weibo = wdata.get('cookie_str', '')
            if new_weibo != self.poster.web_cookie:
                if not new_weibo:
                    logger.warning(
                        f"[CookieReload] Weibo cookie JSON has empty cookie_str — "
                        f"keeping current cookie"
                    )
                else:
                    logger.info(f"[CookieReload] Refreshing Weibo cookie "
                                f"(health={wdata.get('health', 'unknown')}, "
                                f"refresh_count={wdata.get('refresh_count', 0)})")
                    self.poster.web_cookie = new_weibo
                    reloaded_any = True
        except Exception as e:
            logger.warning(f"[CookieReload] Weibo cookie reload failed: {e}")

        return reloaded_any

    def _send_health_alert(self, message: str, state: str = None):
        """Send a health alert via Telegram (best-effort, never raises)."""
        if self._telegram:
            try:
                self._telegram.send(f"[StreamMonitor] {message}", state=state)
            except Exception:
                pass

    def log_notification(self, event_type: str, content: str, success: bool):
        """Append a notification event to the CSV log.

        Uses atomic append (temp + rename) for crash safety.
        Much more efficient than the old JSON approach (O(1) vs O(n²)).
        """
        import csv as _csv
        try:
            _exists = os.path.exists(NOTIF_CSV_FILE)
            _tmp = NOTIF_CSV_FILE + '.tmp'

            if _exists:
                with open(NOTIF_CSV_FILE, 'r', encoding='utf-8', newline='') as _rf:
                    _existing = _rf.read()
                with open(_tmp, 'w', encoding='utf-8', newline='') as _wf:
                    _wf.write(_existing)
            else:
                open(_tmp, 'w').close()

            with open(_tmp, 'a', encoding='utf-8', newline='') as _af:
                _writer = _csv.writer(_af)
                if not _exists:
                    _writer.writerow([
                        'timestamp', 'live_id', 'event_type',
                        'content_preview', 'success',
                    ])
                # Truncate content to avoid CSV-breaking newlines in cells
                _preview = content.replace('\n', '\\n').replace('\r', '')[:200]
                _writer.writerow([
                    datetime.now().isoformat(),
                    self.live_id,
                    event_type,
                    _preview,
                    1 if success else 0,
                ])
            os.replace(_tmp, NOTIF_CSV_FILE)
        except Exception as e:
            logger.error(f"Error writing notification CSV: {e}")

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
            # Pass HTTP-parsed room_info as fallback for WebSocket setup,
            # so _connect_ws doesn't need to re-fetch via get_live_info
            self.stats_recorder._http_room_info = room_info
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
            if not self.poster.check_validity():
                logger.error("Weibo cookie invalid — skipping live notification")
                self._send_health_alert(
                    f"Weibo cookie invalid for room {self.live_id} — "
                    f"live notification skipped",
                    state="weibo_cookie_invalid_live",
                )
                success = False
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

            # ── Final follower count (stream-end post-snapshot) ───────
            # Priority order:
            #   1. WebSocket followCount (uint64 from SocialMessage payload)
            #      — the anchor's ABSOLUTE precise follower count.  This is
            #      the ground truth; no API call needed.
            #   2. API precise int64 (when follower_count field is an integer,
            #      not a 万-rounded string).  Used as fallback when the WS
            #      never emitted a SocialMessage.
            #   3. Per-event estimate (follower_before + new_follows).
            #
            # 万-rounded values are EXPLICITLY REJECTED for the final delta
            # — they bake in ±500 precision loss and freeze the count.

            r = self.stats_recorder

            # ── Choose the newer data source: WS or HTTP ─────────────
            # At stream end, the source with the more recent timestamp
            # is most trustworthy.  WS may have stalled hours ago while
            # HTTP kept refreshing.
            ws_fresh = r.ws_follow_last_time if r.ws_follow_last > 0 else None
            http_fresh = r._last_http_refresh if r.http_follow_last > 0 else None
            use_ws = False
            if ws_fresh and http_fresh:
                use_ws = ws_fresh > http_fresh  # newer wins
            elif ws_fresh:
                use_ws = True
            # else: both None → no data, will retry

            if use_ws:
                r.follower_after = r.ws_follow_last
                post_snapshot_ok = True
                logger.info(
                    f"[Offline] Using WS followCount (newer): "
                    f"follower_after={r.follower_after:,}, "
                    f"ws_time={ws_fresh.isoformat() if ws_fresh else 'N/A'}, "
                    f"ws_delta={r.ws_follow_last - r.ws_follow_first}"
                )
            elif http_fresh:
                r.follower_after = r.http_follow_last
                post_snapshot_ok = True
                logger.info(
                    f"[Offline] Using HTTP followCount (newer): "
                    f"follower_after={r.follower_after:,}, "
                    f"http_time={http_fresh.isoformat() if http_fresh else 'N/A'}, "
                    f"http_delta={r.http_follow_last - r.http_follow_first}"
                )
            elif r.http_follow_last > 0:
                # http_fresh was None (_last_http_refresh not set, e.g. CSV
                # recovery path where http_follow_last was restored from CSV
                # but no live HTTP refresh timestamp exists yet).  The data
                # is valid — use it rather than falling through to the API
                # retry loop (which may fail on repeated 万-rounded values).
                r.follower_after = r.http_follow_last
                post_snapshot_ok = True
                logger.info(
                    f"[Offline] Using http_follow_last (no timestamp, "
                    f"CSV recovery): "
                    f"follower_after={r.follower_after:,}, "
                    f"http_delta={r.http_follow_last - r.http_follow_first}"
                )
            else:
                # ── No WS or HTTP — try API fetch ──────────────────
                logger.info(
                    "[Offline] No WS SocialMessage received — "
                    "falling back to API follower fetch"
                )
                for attempt in range(3):
                    if attempt > 0:
                        wait = 10 * attempt
                        logger.info(
                            f"Waiting {wait}s before follower retry "
                            f"{attempt + 1}/3..."
                        )
                        time.sleep(wait)

                    # Call API directly (not via _get_follower) so we can
                    # inspect the RAW follower_count before parsing.
                    try:
                        import sys as _sys
                        user_url = f"https://www.douyin.com/user/{sec_uid}"
                        with _suppress_stdout():
                            _ui = DouyinAPI.get_user_info(auth, user_url)
                        _ud = _ui.get('user', {})
                        _raw_fc = _ud.get('follower_count', 0)

                        # REJECT 万-rounded strings — they silently
                        # corrupt the follower delta with ±500 error.
                        if isinstance(_raw_fc, str) and '万' in _raw_fc:
                            logger.warning(
                                f"[Offline] API returned 万-rounded "
                                f"follower_count=\"{_raw_fc}\" — "
                                f"REJECTED for final snapshot"
                            )
                            # Use WS per-event estimate as floor.
                            # DON'T BREAK — retry may give precise int.
                            r.follower_after = r.ws_follow_last or r.follower_before
                        else:
                            fc = int(float(_raw_fc.replace('万', ''))
                                     if isinstance(_raw_fc, str)
                                     else _raw_fc)
                            if fc > 0:
                                r.follower_after = r.ws_follow_last if r.ws_follow_last > 0 else int(fc)
                                logger.info(
                                    f"[Offline] API follower fetch OK: "
                                    f"raw={_raw_fc}, "
                                    f"follower_after={r.follower_after:,}"
                                )
                            else:
                                logger.warning(
                                    f"[Offline] API returned follower_count=0"
                                )
                                continue
                        post_snapshot_ok = True
                        break
                    except Exception as _e:
                        logger.warning(
                            f"[Offline] Follower fetch attempt "
                            f"{attempt + 1}/3 failed: {_e}"
                        )

            # If the API never returned a usable precise value but
            # follower_after was already populated (WS followCount or
            # per-event estimate from periodic refreshes), proceed anyway.
            if not post_snapshot_ok and r.follower_after <= 0:
                logger.error(
                    "All 3 follower fetch attempts failed AND "
                    "follower_after is still 0. "
                    "SKIPPING Weibo post — follower delta would be unreliable."
                )
                self._send_health_alert(
                    f"Follower fetch FAILED for room {self.live_id} — Weibo summary was skipped",
                    state="follower_fetch_failed")
                self.log_notification(
                    "live_end_skipped_no_snapshot",
                    f"follower_after still 0 after 3 attempts",
                    False,
                )
                # Still stop the recorder so it doesn't linger
                self.stats_recorder.stop()
                return
            elif not post_snapshot_ok and r.follower_after > 0:
                logger.warning(
                    f"[Offline] API returned only 万-rounded values, "
                    f"but follower_after={r.follower_after:,} from "
                    f"WS/estimate — proceeding with offline summary"
                )

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
            if not self.poster.check_validity():
                logger.error("Weibo cookie invalid — skipping offline summary")
                self._send_health_alert(
                    f"Weibo cookie invalid for room {self.live_id} — "
                    f"offline summary skipped",
                    state="weibo_cookie_invalid_offline",
                )
                success = False
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
        safe_print(f"\n[{now.strftime('%H:%M:%S')}] Checking stream status...", flush=True)

        # Periodically reload cookies from cookies.json so mid-stream
        # cookie expiry doesn't silently break monitoring.
        self._reload_cookies()

        try:
            # Suppress stdout from check_status() which may print debug dicts
            with _suppress_stdout():
                room_info = self.checker.check_status()

            logger.debug(f"check_status() returned: {list(room_info.keys())}")
        except Exception as e:
            logger.error(f"Failed to check stream status: {e}")
            return False

        new_status = str(room_info.get('room_status', DouyinLiveChecker.STATUS_OFFLINE))
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
                                self._send_health_alert(
                                    f"WS recovery FAILED for room {self.live_id} — data may be incomplete",
                                    state="ws_recovery_failed")
                        else:
                            # Recorder thread died silently (WS connection failed, run_forever
                            # exited without calling _on_close, etc.).  ws_disconnected is False
                            # so the WS-recovery path never triggers.  Replace the dead recorder
                            # with a fresh instance to resume data flow.
                            logger.warning("Recorder thread died (non-WS reason) — replacing with fresh instance.")
                            try:
                                self.stats_recorder.stop()
                            except Exception:
                                pass
                            self.stats_recorder = LiveStatsRecorder(
                                self.live_id, self.dy_cookie_str, verbose=self.verbose
                            )
                            self.stats_recorder.start_background()
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
                    safe_print(f"[{now.strftime('%H:%M:%S')}] Stream is LIVE (restart) – resuming stats without notification", flush=True)
                else:
                    safe_print(f"[{now.strftime('%H:%M:%S')}] Stream is LIVE – starting stats recording + notification", flush=True)
                self.handle_live(room_info)
                # Wait for _take_pre_snapshot() to finish (max 15s).
                # follower_before always comes from a fresh API capture on
                # each stream start — never from stale seed/saved data.
                if self.stats_recorder:
                    self.stats_recorder._pre_snapshot_done.wait(timeout=15)
                changed = True
            else:
                safe_print(f"[{now.strftime('%H:%M:%S')}] Stream is OFFLINE – waiting for live", flush=True)
            self.current_status = new_status
        elif new_status != self.current_status:
            changed = True
            if new_status == DouyinLiveChecker.STATUS_LIVE:
                # New stream cycle — reset guards from the previous stream.
                self._live_posted = False
                self._summary_posted = False
                safe_print(f"[{now.strftime('%H:%M:%S')}] Stream went LIVE – starting stats recording + notification", flush=True)
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
                        safe_print(f"[{now.strftime('%H:%M:%S')}] ⚠ HTTP says OFFLINE but WS still running ({self._consecutive_offline_count}/{self.CONSECUTIVE_OFFLINE_LIMIT})", flush=True)
                        time.sleep(5)
                        return False
                    else:
                        logger.warning(
                            f"HTTP OFFLINE detected {self.CONSECUTIVE_OFFLINE_LIMIT} consecutive times. "
                            f"Forcing offline event despite WS being connected."
                        )
                        safe_print(f"[{now.strftime('%H:%M:%S')}] Stream went OFFLINE (confirmed by {self.CONSECUTIVE_OFFLINE_LIMIT} HTTP checks) – posting notification", flush=True)
                        # handle_offline will stop the recorder and take post-snapshot internally
                        self.handle_offline(room_info)
                        self.current_status = new_status
                        return True

                safe_print(f"[{now.strftime('%H:%M:%S')}] Stream went OFFLINE – posting notification", flush=True)
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
                viewers = fmt_wan(r._get_current_viewers()) if r._get_current_viewers() else "?"
                stats_info = f" | Viewers: {viewers} | Likes: {fmt_wan(r.total_likes) if r.total_likes else '?'}{uptime}"
                safe_print(f"[{now.strftime('%H:%M:%S')}] Status: {status_text} | Title: {title}{stats_info}", flush=True)
                
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
                    safe_print(f"\n📋 WEIBO PREVIEW (will post when stream ends):")
                    safe_print(strip_emoji(preview))
                    safe_print()
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
                    # If WS followCount has stalled (>5 min), force an HTTP
                    # refresh right away so http_delta takes over as authoritative.
                    elif r._ws_follow_stalled():
                        logger.debug("[Main Loop] WS followCount stalled — forcing HTTP refresh...")
                        try:
                            r.fetch_cumulative_via_http(mark_recovery=False)
                        except Exception:
                            pass
                        # Also restart WS to get a fresh session where
                        # followCount might work again.  Limited to once
                        # per 10 min to avoid restart loops.
                        cooldown_ok = (r._ws_last_restart_time is None or
                                       (now - r._ws_last_restart_time).total_seconds() > 600)
                        if cooldown_ok and not r.ws_disconnected and r._ws_restart_count < 5:
                            r._ws_restart_count += 1
                            r._ws_last_restart_time = now
                            # Reset followCount counters so new session starts clean
                            r.ws_follow_first = 0
                            r.ws_follow_last = 0
                            r.ws_follow_last_time = None
                            logger.warning(
                                f"[Main Loop] WS followCount stagnant — restarting WS "
                                f"(attempt {r._ws_restart_count}/5) for fresh session..."
                            )
                            try:
                                r.ws.close()
                                r.ws_disconnected = True
                            except Exception:
                                pass
            else:
                safe_print(f"[{now.strftime('%H:%M:%S')}] Status: {status_text} | Title: {title}", flush=True)
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

        # ── SIGTERM handler: graceful shutdown on PM2 restart ──────────
        # PM2 sends SIGTERM first, then SIGKILL after 1.6s timeout.
        # This handler flushes stats to disk so restarts don't lose data.
        def _handle_sigterm(signum, frame):
            logger.warning("SIGTERM received — shutting down gracefully...")
            if self.stats_recorder and self.stats_recorder.is_running():
                logger.info("Writing final stats before exit...")
                self.stats_recorder._write_live_stats_json(live=False)
                self.stats_recorder._write_stats_csv()
                self.stats_recorder.stop()
                logger.info("Stats flushed. Exiting.")
            sys.exit(0)
        signal.signal(signal.SIGTERM, _handle_sigterm)

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

    # Override with values from shared cookie JSON files (refresher output)
    # so the monitor picks up fresh cookies immediately — no need to wait
    # for the first _reload_cookies() call (5-min cooldown).
    if not args.dy_cookie:
        try:
            from cookies import CookieManager
            _dy_data = CookieManager().load()
            _dy_json = _dy_data.get('cookie_str', '')
            if _dy_json:
                config['DY_LIVE_COOKIES'] = _dy_json
                logger.info("Loaded Douyin cookie from cookies.json at startup")
        except Exception as e:
            logger.debug(f"Could not load Douyin cookie from JSON: {e}")

    if not args.weibo_cookie:
        try:
            from cookies import WeiboCookieManager
            _wb_data = WeiboCookieManager().load()
            _wb_json = _wb_data.get('cookie_str', '')
            if _wb_json:
                config['WEIBO_COOKIE'] = _wb_json
                logger.info("Loaded Weibo cookie from weibo_cookies.json at startup")
        except Exception as e:
            logger.debug(f"Could not load Weibo cookie from JSON: {e}")

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
            offline_summary_tpl = "{name} 直播结束\n场观：{views}\n点赞：{likes}\n最高在线：{peak}\n平均在线：{avg}\n关注涨幅度：{followers}\n新增粉丝团：{members}\n点亮灯牌：{badges}\n热门礼物：{gifts}"

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
            offline_summary_tpl = "{name} 直播结束\n场观：{views}\n点赞：{likes}\n最高在线：{peak}\n平均在线：{avg}\n关注涨幅度：{followers}\n新增粉丝团：{members}\n点亮灯牌：{badges}\n热门礼物：{gifts}"

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
        logger.warning("No Weibo cookie configured — notifications will be logged but not posted. "
                       "Set WEIBO_COOKIE in .env or pass --weibo-cookie to enable posting.")

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