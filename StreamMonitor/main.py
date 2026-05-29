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
    Live_pb2 = None
    DouyinAPI = None
    HeaderBuilder = None
    Params = None
    generate_signature = None
    HAS_LIVE_DETAILS = False


# ======================================================================
# Formatters for human-readable numbers
# ======================================================================

def fmt_wan(val: float) -> str:
    """Format a number into 万 (10k) unit.
    e.g. 48830000 -> "4883.0万",  523600 -> "52.36万"
    """
    if val >= 10000:
        s = f"{val / 10000:.2f}万"
        s = re.sub(r'(\d+\.\d*?)0+万', r'\g<1>万', s)
        s = re.sub(r'\.0万', '.0万', s)
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
        self.light_badges = 0
        self.fan_club_joins = 0
        self.gift_events = []

        # viewer tracking
        self.viewer_samples = []
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
            room_info = DouyinAPI.get_live_info(auth, self.live_id)
            room_id = room_info['room_id']
            user_id = room_info['user_id']
            ttwid = room_info['ttwid']
            self._take_pre_snapshot(auth, room_info)
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
            if self.stream_end_time is None:
                self.stream_end_time = datetime.now()
                self._take_post_snapshot(auth)
                self._generate_summary()
                if hasattr(self, '_callback') and self._callback:
                    self._callback(self)

    def _take_pre_snapshot(self, auth, room_info):
        try:
            sec_uid = room_info.get('sec_uid', '')
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                user_info = DouyinAPI.get_user_info(auth, user_url)
                user_data = user_info.get('user', {})
                self.anchor_nickname = user_data.get('nickname', '')
                fc = user_data.get('follower_count', 0)
                if isinstance(fc, str):
                    fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
                self.follower_before = int(fc)
                if self.verbose:
                    logger.info(f"[StatsRecorder] Pre-snapshot: nickname={self.anchor_nickname}, followers={self.follower_before}")
            else:
                self.anchor_nickname = room_info.get('room_title', '')
        except Exception as e:
            logger.warning(f"[StatsRecorder] Pre-snapshot failed: {e}")

    def _take_post_snapshot(self, auth):
        try:
            room_info = DouyinAPI.get_live_info(auth, self.live_id)
            sec_uid = room_info.get('sec_uid', '')
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                user_info = DouyinAPI.get_user_info(auth, user_url)
                user_data = user_info.get('user', {})
                fc = user_data.get('follower_count', 0)
                if isinstance(fc, str):
                    fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
                self.follower_after = int(fc)
                if self.verbose:
                    logger.info(f"[StatsRecorder] Post-snapshot: followers={self.follower_after}, delta={self.follower_after - self.follower_before}")
        except Exception as e:
            logger.warning(f"[StatsRecorder] Post-snapshot failed: {e}")

    def _on_open(self, ws):
        logger.info(f"[StatsRecorder] WebSocket connected to room {self.live_id}")
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
            time.sleep(5)

    def _periodic_summary(self, ws):
        counter = 0
        while not self._stop_event.is_set():
            time.sleep(60)
            if self._stop_event.is_set():
                break
            counter += 1
            print(f"\n{'─'*60}")
            print(f"  📊 直播中概览 (已直播{counter}分钟) - {datetime.now().strftime('%H:%M:%S')}")
            print(f"{'─'*60}")

            avg_viewers = sum(self.viewer_samples) // len(self.viewer_samples) if self.viewer_samples else 0
            peak = self.peak_viewers
            likes = self.total_likes
            badge = self.light_badges
            follows = self.new_follows
            gifts = len(self.gift_events)

            views_wan = self._try_get_wan('观看')
            likes_wan = self._try_get_wan('点赞')
            peak_wan = self._try_get_wan('最高在线')
            current_online = self.viewer_samples[-1] if self.viewer_samples else 0

            print(f"  👀 当前在线：{fmt_wan(current_online)}人")
            if self.cumulative_views:
                print(f"  👁 场观：{fmt_wan(self.cumulative_views)}人")
            print(f"  👍 点赞：{likes_wan or fmt_wan(likes)}")
            print(f"  🔥 最高在线：{peak_wan or fmt_wan(peak)}")
            print(f"  📊 平均在线：{fmt_wan(avg_viewers)}")
            print(f"  📈 新增关注：{follows}")
            print(f"  🌟 新增粉丝团：{self.fan_club_joins}人 (点亮粉丝团)")
            print(f"  💡 点亮灯牌：{self.light_badges}个 (粉丝团灯牌)")
            print(f"  🎁 礼物事件：{gifts}")
            if gifts > 0 and self.gift_events:
                gift_summary = {}
                for g in self.gift_events:
                    name = g['gift']
                    gift_summary[name] = gift_summary.get(name, 0) + g['count']
                top = sorted(gift_summary.items(), key=lambda x: -x[1])[:3]
                if top:
                    print(f"  🎀 热门礼物：{' | '.join([f'{n}×{c}' for n,c in top])}")
            print(f"{'─'*60}\n")

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
                    if self.verbose:
                        logger.info(f"[MEMBER] user={msg.user.nickname}, memberCount={msg.memberCount}, total={self.new_members}")

                elif method == 'WebcastGiftMessage':
                    msg = Live_pb2.GiftMessage()
                    msg.ParseFromString(payload)
                    gift_name = msg.gift.name
                    self.gift_events.append({
                        'user': msg.user.nickname,
                        'gift': gift_name,
                        'count': msg.comboCount,
                    })
                    if '粉丝团灯牌' in gift_name:
                        self.light_badges += 1
                    elif '点亮粉丝团' in gift_name:
                        self.fan_club_joins += 1
                    if self.verbose:
                        logger.info(f"[GIFT] {msg.user.nickname} × {gift_name} x{msg.comboCount}")

                elif method == 'WebcastRoomStatsMessage':
                    msg = Live_pb2.RoomStatsMessage()
                    msg.ParseFromString(payload)
                    self.display_long_history.append(msg.displayLong)
                    current_viewers = msg.displayValue
                    if current_viewers > self.peak_viewers:
                        self.peak_viewers = current_viewers
                        self.peak_viewer_time = datetime.now().strftime('%H:%M:%S')
                    self.viewer_samples.append(current_viewers)

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
                        elif key == '灯牌':
                            badge_val = int(val * 10000) if val < 1e6 else int(val)
                            self.light_badges = max(self.light_badges, badge_val)

                elif method in (
                    'WebcastFansClubMessage', 'WebcastLightMessage',
                    'WebcastFanBadgeMessage', 'WebcastAudienceMessage',
                ):
                    self.light_badges += 1
                    if self.verbose:
                        logger.info(f"[BADGE/{method}] total={self.light_badges}")

                elif method == 'WebcastRoomUserSeqMessage':
                    try:
                        proto_path = os.path.join(os.path.expanduser('~'), 'DouyinFetcherapp1', 'protobuf', 'douyin.py')
                        if os.path.exists(proto_path):
                            from importlib.machinery import SourceFileLoader
                            proto_mod = SourceFileLoader("douyin_proto", proto_path).load_module()
                            seq_msg = proto_mod.RoomUserSeqMessage().parse(payload)
                            pv = parse_chinese_number(seq_msg.total_pv_for_anchor)
                            if pv > self.cumulative_views:
                                self.cumulative_views = pv
                            if self.verbose and pv > 0:
                                logger.info(f"[VIEWS] 场观 = {pv:,}")
                    except Exception as e:
                        if self.verbose:
                            logger.debug(f"[VIEWS] parse failed: {e}")

        except Exception:
            pass

    def _on_error(self, ws, error):
        logger.error(f"[StatsRecorder] WebSocket error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        if self._stop_event.is_set():
            return
        logger.info(f"[StatsRecorder] WebSocket closed (status={close_status_code})")
        self.stream_end_time = datetime.now()
        try:
            from builder.auth import DouyinAuth  # type: ignore[import-untyped]
            auth = DouyinAuth()
            auth.perepare_auth(self.cookie_str, "", "")
            self._take_post_snapshot(auth)
        except Exception:
            pass
        self._generate_summary()
        if hasattr(self, '_callback') and self._callback:
            self._callback(self)

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
            print(f"    light_badges      = {self.light_badges}")
            print(f"    peak_viewers      = {self.peak_viewers:,}")
            print(f"    viewer_samples    = {len(self.viewer_samples)}")
            print(f"    gift_events        = {len(self.gift_events)}")
            print()

        avg_viewers = 0
        if self.viewer_samples:
            avg_viewers = sum(self.viewer_samples) // len(self.viewer_samples)

        follower_delta = self.follower_after - self.follower_before
        if follower_delta < 0:
            follower_delta = self.new_follows

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

        likes_wan = self._try_get_wan('点赞')
        if likes_wan:
            print(f"👍 本场直播点赞数据：{likes_wan}")
        elif self.total_likes:
            print(f"👍 本场直播点赞数据：{fmt_wan(self.total_likes)}")
        else:
            print(f"👍 本场直播点赞数据：--")

        print(f"📈 本场新增粉丝：{fmt_wan(follower_delta)}人")
        print(f"（粉丝：{fmt_wan(self.follower_before)} ➡️ {fmt_wan(self.follower_after)}）")

        peak_wan = self._try_get_wan('最高在线')
        if peak_wan:
            print(f"🔥 最高在线：{peak_wan}人")
        elif self.peak_viewers:
            print(f"🔥 最高在线：{fmt_wan(self.peak_viewers)}人")
        else:
            print(f"🔥 最高在线：--")

        print(f"📊 平均在线：{fmt_wan(avg_viewers)}人")

        members_wan = self._try_get_wan('粉丝团')
        if members_wan:
            raw_num = members_wan.replace('万', '').replace(',', '')
            print(f"🌟 今日新增粉丝团：{raw_num}人")
        else:
            print(f"🌟 今日新增粉丝团：{self.fan_club_joins}人")

        badge_wan = self._try_get_wan('灯牌')
        if badge_wan:
            raw_num = badge_wan.replace('万', '').replace(',', '')
            print(f"💡 今日点亮灯牌：{raw_num}人")
        else:
            print(f"💡 今日点亮灯牌：{self.light_badges}人")

        if duration_str:
            print(f"⏱ 直播时长：{duration_str}")

        if self.gift_events:
            gift_summary = {}
            for g in self.gift_events:
                name = g['gift']
                gift_summary[name] = gift_summary.get(name, 0) + g['count']
            top_gifts = sorted(gift_summary.items(), key=lambda x: -x[1])[:5]
            if top_gifts:
                print(f"\n🎁 热门礼物 TOP{len(top_gifts)}:")
                for name, cnt in top_gifts:
                    print(f"   · {name} × {cnt}")

        print(f"\n{'='*60}")
        print(f"  🏁 总结完毕")
        print(f"{'='*60}\n")

    def _try_get_wan(self, key: str):
        for dl in reversed(self.display_long_history):
            parsed = parse_display_long(dl)
            if key in parsed:
                val = parsed[key]
                return f"{val:.1f}万" if val == int(val) else f"{val:.2f}万"
        return None


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
        url = f"https://live.douyin.com/{self.live_id}"
        try:
            resp = requests.get(url, headers=self.HEADERS, cookies=self.cookie, verify=False, timeout=15)
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
                    logger.debug(f"Douyin check result: status={room_status}, title={room_title}")
                    return result
                except Exception:
                    continue

            logger.warning("Could not find room info in page HTML")
            return {"room_status": self.STATUS_OFFLINE}

        except requests.RequestException as e:
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
        self.live_template = live_template or "【开播提醒】主播已开播！直播主题：{title}"
        # Replace literal \n with actual newlines so users can write single-line
        # templates in .env like: {name} 直播结束\n场观：{views}
        if offline_summary_template:
            offline_summary_template = offline_summary_template.replace('\\n', '\n')
        self.offline_summary_template = offline_summary_template or \
            "直播结束\n场观：{views}\n点赞：{likes}\n最高在线：{peak}\n平均在线：{avg}\n新增粉丝：{followers}\n新增粉丝团：{members}\n点亮灯牌：{badges}\n热门礼物：{gifts}"

        self.checker = DouyinLiveChecker(live_id, dy_cookie_str)
        self.poster = WeiboPoster(weibo_cookie)

        self.stats_recorder = None
        self.log_file = os.path.join(os.path.dirname(__file__), 'notification_log.json')

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

        # Stop stats recorder and generate summary first
        if self.stats_recorder and self.stats_recorder.is_running():
            logger.info("Stopping stats recorder and generating summary...")
            self.stats_recorder.stream_end_time = datetime.now()
            try:
                from builder.auth import DouyinAuth  # type: ignore[import-untyped]
                auth = DouyinAuth()
                auth.perepare_auth(self.dy_cookie_str, "", "")
                self.stats_recorder._take_post_snapshot(auth)
            except Exception:
                pass

        # Build summary text for Weibo (no emojis)
        if self.stats_recorder:
            r = self.stats_recorder
            # Compute template variables
            pv = r._try_get_wan('观看') or (fmt_wan(r.cumulative_views) if r.cumulative_views else "")
            likes = r._try_get_wan('点赞') or (fmt_wan(r.total_likes) if r.total_likes else "")
            peak = r._try_get_wan('最高在线') or (fmt_wan(r.peak_viewers) if r.peak_viewers else "")
            avg = fmt_wan(sum(r.viewer_samples) // len(r.viewer_samples)) if r.viewer_samples else ""
            delta = r.follower_after - r.follower_before
            if delta <= 0:
                delta = r.new_follows  # fallback to event count if API failed
            followers_str = str(delta)
            members_str = str(r.fan_club_joins)
            badges_str = str(r.light_badges)

            # Compute duration
            duration_str = ""
            if r.stream_start_time and r.stream_end_time:
                delta_dur = r.stream_end_time - r.stream_start_time
                mins, secs = divmod(int(delta_dur.total_seconds()), 60)
                hours, mins = divmod(mins, 60)
                if hours > 0:
                    duration_str = f"{hours}小时{mins}分钟"
                else:
                    duration_str = f"{mins}分钟{secs}秒"

            # Top gifts string
            gifts_str = ""
            if r.gift_events:
                gift_summary = {}
                for g in r.gift_events:
                    name = g['gift']
                    gift_summary[name] = gift_summary.get(name, 0) + g['count']
                top = sorted(gift_summary.items(), key=lambda x: -x[1])[:3]
                if top:
                    gifts_str = " | ".join([f"{n}×{c}" for n, c in top])

            try:
                content = self.offline_summary_template.format(
                    name=r.anchor_nickname or "",
                    views=pv or "--",
                    likes=likes or "--",
                    peak=peak or "--",
                    avg=avg or "--",
                    followers=followers_str,
                    members=members_str,
                    badges=badges_str,
                    gifts=gifts_str if gifts_str else "--",
                    duration=duration_str or "--",
                )
            except KeyError as e:
                logger.error(f"Offline template format error: missing key {e}. Template: {self.offline_summary_template!r}")
                content = f"直播结束 live.douyin.com/{self.live_id}"
        else:
            content = f"直播结束 live.douyin.com/{self.live_id}"

        # Post to Weibo
        logger.info("Stream is OFFLINE → posting summary")
        if self._dry_run:
            logger.info(f"[DRY-RUN] Would post offline summary:\n{content}")
            success = True
        else:
            success = self.poster.post_tweet(content)
        self.log_notification("live_end", content, success)

        # Now stop and clean up
        if self.stats_recorder:
            self.stats_recorder.stop()
            self.stats_recorder = None

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
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking stream status...", flush=True)

        try:
            room_info = self.checker.check_status()
        except Exception as e:
            logger.error(f"Failed to check stream status: {e}")
            return False

        new_status = str(room_info.get('room_status', DouyinLiveChecker.STATUS_OFFLINE))
        status_text = "LIVE" if new_status == DouyinLiveChecker.STATUS_LIVE else "OFFLINE"
        title = room_info.get('room_title', '')
        if title:
            self.live_title = title

        changed = False

        # Self-healing: if stream is LIVE but stats recorder died (e.g. WS disconnected),
        # restart it automatically
        if new_status == DouyinLiveChecker.STATUS_LIVE:
            if not self.stats_recorder or not self.stats_recorder.is_running():
                if HAS_LIVE_DETAILS and self.current_status == DouyinLiveChecker.STATUS_LIVE:
                    logger.info("Stats recorder was dead, restarting for live stream...")
                    self.stats_recorder = LiveStatsRecorder(
                        self.live_id, self.dy_cookie_str, verbose=self.verbose
                    )
                    self.stats_recorder.start_background()

        if self.current_status is None:
            if new_status == DouyinLiveChecker.STATUS_LIVE:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Stream is LIVE – starting stats recording + notification", flush=True)
                self.handle_live(room_info)
                changed = True
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Stream is OFFLINE – waiting for live", flush=True)
            self.current_status = new_status
        elif new_status != self.current_status:
            changed = True
            if new_status == DouyinLiveChecker.STATUS_LIVE:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Stream went LIVE – starting stats recording + notification", flush=True)
                self.handle_live(room_info)
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Stream went OFFLINE – posting notification", flush=True)
                self.handle_offline(room_info)
            self.current_status = new_status
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Status unchanged: {status_text}", flush=True)

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
        'CHECK_INTERVAL': int(os.getenv('CHECK_INTERVAL', '60')),
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
    parser.add_argument('--interval', type=int, default=60, help='Check interval in seconds (default: 60)')
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
            offline_summary_tpl = "直播结束\n场观：{views}\n点赞：{likes}\n最高在线：{peak}\n平均在线：{avg}\n新增粉丝：{followers}\n新增粉丝团：{members}\n点亮灯牌：{badges}\n热门礼物：{gifts}"

        r = recorder

        # Fallback: if recorder didn't get the anchor nickname, try page HTML
        if not r.anchor_nickname:
            try:
                checker = DouyinLiveChecker(live_id, dy_cookie)
                room_info = checker.check_status()
                r.anchor_nickname = room_info.get('anchor_nickname', '') or r.anchor_nickname
            except Exception:
                pass

        pv = r._try_get_wan('观看') or (fmt_wan(r.cumulative_views) if r.cumulative_views else "")
        likes = r._try_get_wan('点赞') or (fmt_wan(r.total_likes) if r.total_likes else "")
        peak = r._try_get_wan('最高在线') or (fmt_wan(r.peak_viewers) if r.peak_viewers else "")
        avg = fmt_wan(sum(r.viewer_samples) // len(r.viewer_samples)) if r.viewer_samples else ""
        delta = r.follower_after - r.follower_before
        if delta <= 0:
            delta = r.new_follows
        followers_str = str(delta)
        members_str = str(r.fan_club_joins)

        duration_str = ""
        if r.stream_start_time and r.stream_end_time:
            delta_dur = r.stream_end_time - r.stream_start_time
            mins, secs = divmod(int(delta_dur.total_seconds()), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                duration_str = f"{hours}小时{mins}分钟"
            else:
                duration_str = f"{mins}分钟{secs}秒"

        gifts_str = ""
        if r.gift_events:
            gift_summary = {}
            for g in r.gift_events:
                name = g['gift']
                gift_summary[name] = gift_summary.get(name, 0) + g['count']
            top = sorted(gift_summary.items(), key=lambda x: -x[1])[:3]
            if top:
                gifts_str = " | ".join([f"{n}×{c}" for n, c in top])

        try:
            preview = offline_summary_tpl.format(
                name=r.anchor_nickname or "",
                views=pv or "--",
                likes=likes or "--",
                peak=peak or "--",
                avg=avg or "--",
                followers=followers_str,
                members=members_str,
                badges=str(r.light_badges),
                gifts=gifts_str if gifts_str else "--",
                duration=duration_str or "--",
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