# coding=utf-8
"""
Live Stream Summary Recorder
─────────────────────────────
Connects to a Douyin live room via WebSocket, records all statistics
(likes, followers, member joins, viewer counts, gifts, etc.),
and prints a formatted summary when the stream ends.

Usage:
    python dy_live/live_summary.py <live_id>
"""

import gzip
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

from websocket import WebSocketApp

# ── project imports ──────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import static.Live_pb2 as Live_pb2
from dy_apis.douyin_api import DouyinAPI
from builder.header import HeaderBuilder
from builder.params import Params
from builder.auth import DouyinAuth
import utils.common_util as common_util
from utils.dy_util import generate_signature


# ======================================================================
# Formatters for human-readable numbers
# ======================================================================

def fmt_wan(val: float) -> str:
    """Format a number into 万 (10k) unit.
    e.g. 48830000 → "4883.0万",  523600 → "52.36万"
    """
    if val >= 10000:
        s = f"{val / 10000:.2f}万"
        # trim trailing zeros after decimal  e.g. 4883.00万 → 4883.0万
        s = re.sub(r'(\d+\.\d*?)0+万', r'\g<1>万', s)
        s = re.sub(r'\.0万', '.0万', s)  # keep .0
        return s
    return str(int(val))


def fmt_num(val: int) -> str:
    """Format integer with thousands separator."""
    return f"{val:,}"


# ======================================================================
# Data fields found in displayLong (RoomStatsMessage)
# ======================================================================
# Example displayLong values:
#   "点赞: 31566.0万"
#   "观看: 488.3万"
#   "在线: 75.7万"
#   "最高在线: 108.6万"
#   "总人数: 75.7万"
#   "粉丝团: 25.5万"
#   "灯牌: 36.8万"

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
            if '万' not in m.group(0) and '万' not in display_long:
                # raw number without 万
                pass
            # Most values in displayLong are in 万 already
            result[key] = val
    return result


# ======================================================================
# Live Summary Recorder
# ======================================================================

class LiveSummaryRecorder:
    """Connects to a Douyin live room, records all statistics during the
    stream, and prints a formatted summary when the stream ends."""

    def __init__(self, live_id: str, auth_, verbose: bool = True):
        self.live_id = live_id
        self.auth_ = auth_
        self.verbose = verbose
        self.ws = None

        # ── counters ────────────────────────────────────────────
        self.total_likes = 0          # cumulative like total from LikeMessage.total
        self.new_follows = 0          # count of follow events (SocialMessage action=1)
        self.new_members = 0          # count of member join events (MemberMessage)
        self.light_badges = 0         # count of fan badge light-up events
        self.gift_events = []         # list of gift dicts

        # viewer tracking
        self.viewer_samples = []      # periodic snapshots of online viewer count
        self.peak_viewers = 0
        self.peak_viewer_time = ""

        # cumulative stats from room stats messages
        self.cumulative_views = 0
        self.display_long_history = []

        # ── user info snapshot ──────────────────────────────────
        self.follower_before = 0
        self.follower_after = 0
        self.anchor_nickname = ""

        # ── timing ──────────────────────────────────────────────
        self.stream_start_time = None
        self.stream_end_time = None
        self.last_stats_time = time.time()

        # ── control ─────────────────────────────────────────────
        self._stop_event = threading.Event()

    # ──────────────────────────────────────────────────────────────

    def take_pre_snapshot(self):
        """Fetch user info & follower count BEFORE the stream starts (or at connect)."""
        if self.verbose:
            print("\n[Pre-Snapshot] Fetching room info and user profile...")
        try:
            room_info = DouyinAPI.get_live_info(self.auth_, self.live_id)
            if self.verbose:
                print(f"[Pre-Snapshot] room_id={room_info.get('room_id', '?')}, "
                      f"anchor_id={room_info.get('anchor_id', '?')}, "
                      f"sec_uid={room_info.get('sec_uid', '?')}, "
                      f"room_status={room_info.get('room_status', '?')}, "
                      f"room_title={room_info.get('room_title', '?')}")
            sec_uid = room_info.get('sec_uid', '')
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                user_info = DouyinAPI.get_user_info(self.auth_, user_url)
                user_data = user_info.get('user', {})
                self.anchor_nickname = user_data.get('nickname', '')
                fc_raw = user_data.get('follower_count', 0)
                if self.verbose:
                    print(f"[Pre-Snapshot] nickname={self.anchor_nickname}, "
                          f"follower_count_raw={fc_raw}")
                # follower_count can be a string like "3015.91万" or int
                fc = fc_raw
                if isinstance(fc, str):
                    fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
                self.follower_before = int(fc)
                if self.verbose:
                    print(f"[Pre-Snapshot] follower_before={self.follower_before}")
            else:
                self.anchor_nickname = room_info.get('room_title', '')
                if self.verbose:
                    print(f"[Pre-Snapshot] No sec_uid, using room_title as nickname")
        except Exception as e:
            print(f"[Pre-Snapshot] Warning: could not fetch pre-stream user info: {e}")

    def take_post_snapshot(self):
        """Fetch user info AFTER stream ends to compute follower delta."""
        if self.verbose:
            print("\n[Post-Snapshot] Fetching user profile after stream end...")
        try:
            room_info = DouyinAPI.get_live_info(self.auth_, self.live_id)
            sec_uid = room_info.get('sec_uid', '')
            if sec_uid:
                user_url = f"https://www.douyin.com/user/{sec_uid}"
                user_info = DouyinAPI.get_user_info(self.auth_, user_url)
                user_data = user_info.get('user', {})
                fc_raw = user_data.get('follower_count', 0)
                if self.verbose:
                    print(f"[Post-Snapshot] follower_count_raw={fc_raw}")
                fc = fc_raw
                if isinstance(fc, str):
                    fc = float(fc.replace('万', '')) * 10000 if '万' in fc else float(fc)
                self.follower_after = int(fc)
                if self.verbose:
                    print(f"[Post-Snapshot] follower_after={self.follower_after}, "
                          f"delta={self.follower_after - self.follower_before}")
        except Exception as e:
            print(f"[Post-Snapshot] Warning: could not fetch post-stream user info: {e}")

    # ──────────────────────────────────────────────────────────────

    def on_open(self, ws):
        print(f"\n{'='*60}")
        print(f"  📡 Connected to live room: {self.live_id}")
        print(f"{'='*60}\n")
        self.stream_start_time = datetime.now()
        print(f"  [Time] Stream start recorded at {self.stream_start_time.strftime('%H:%M:%S')}")
        print(f"  [Verbose] Live event detail logging: ON\n")
        threading.Thread(target=self._ping, args=(ws,), daemon=True).start()
        threading.Thread(target=self._periodic_stats, args=(ws,), daemon=True).start()

    def _ping(self, ws):
        while not self._stop_event.is_set():
            frame = Live_pb2.PushFrame()
            frame.payloadType = "hb"
            try:
                ws.send(frame.SerializeToString(), opcode=0x02)
            except Exception:
                break
            time.sleep(5)

    def _periodic_stats(self, ws):
        """Every 10 seconds, log current stats."""
        while not self._stop_event.is_set():
            time.sleep(10)
            elapsed = int(time.time() - self.last_stats_time)
            print(f"\n{'─'*50}")
            print(f"  [LIVE STATS @ {datetime.now().strftime('%H:%M:%S')}]")
            print(f"    Like total  : {self.total_likes:,}")
            print(f"    New follows : {self.new_follows}")
            print(f"    New members : {self.new_members}")
            print(f"    Badge lights: {self.light_badges}")
            print(f"    Peak viewers: {self.peak_viewers:,}")
            print(f"    Gift events : {len(self.gift_events)}")
            avg = sum(self.viewer_samples) // len(self.viewer_samples) if self.viewer_samples else 0
            print(f"    Avg viewers : {avg:,}")
            print(f"    Samples     : {len(self.viewer_samples)}")
            print(f"{'─'*50}")

    # ──────────────────────────────────────────────────────────────

    def on_message(self, ws, message):
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
                    old = self.total_likes
                    self.total_likes = max(self.total_likes, msg.total)
                    if self.verbose:
                        print(f"  [LIKE] user={msg.user.nickname}, count={msg.count}, "
                              f"total={msg.total}, cumulative={self.total_likes}")

                elif method == 'WebcastSocialMessage':
                    msg = Live_pb2.SocialMessage()
                    msg.ParseFromString(payload)
                    if msg.action == 1:           # follow
                        self.new_follows += 1
                        if self.verbose:
                            print(f"  [FOLLOW] user={msg.user.nickname}, "
                                  f"followCount={msg.followCount}, total={self.new_follows}")
                    else:
                        if self.verbose:
                            print(f"  [SOCIAL] user={msg.user.nickname}, action={msg.action}, "
                                  f"shareType={msg.shareType}")

                elif method == 'WebcastMemberMessage':
                    msg = Live_pb2.MemberMessage()
                    msg.ParseFromString(payload)
                    self.new_members += 1
                    if self.verbose:
                        print(f"  [MEMBER] user={msg.user.nickname}, "
                              f"memberCount={msg.memberCount}, total={self.new_members}")

                elif method == 'WebcastGiftMessage':
                    msg = Live_pb2.GiftMessage()
                    msg.ParseFromString(payload)
                    self.gift_events.append({
                        'user': msg.user.nickname,
                        'gift': msg.gift.name,
                        'count': msg.comboCount,
                    })
                    if self.verbose:
                        print(f"  [GIFT] {msg.user.nickname} sent {msg.gift.name} x{msg.comboCount}")

                elif method == 'WebcastRoomStatsMessage':
                    msg = Live_pb2.RoomStatsMessage()
                    msg.ParseFromString(payload)
                    self.display_long_history.append(msg.displayLong)

                    # Track viewer count (displayValue usually = current online)
                    current_viewers = msg.displayValue
                    if current_viewers > self.peak_viewers:
                        self.peak_viewers = current_viewers
                        self.peak_viewer_time = datetime.now().strftime('%H:%M:%S')
                    self.viewer_samples.append(current_viewers)

                    # Parse displayLong for cumulative stats
                    parsed = parse_display_long(msg.displayLong)
                    if self.verbose:
                        print(f"  [ROOMSTATS] displayLong=\"{msg.displayLong}\"")
                        print(f"              displayShort=\"{msg.displayShort}\" displayMiddle=\"{msg.displayMiddle}\"")
                        print(f"              displayValue={msg.displayValue:,} total={msg.total:,}")
                        if parsed:
                            for k, v in parsed.items():
                                print(f"              parsed[{k}] = {v}")

                    for key, val in parsed.items():
                        if key == '点赞':
                            like_val = int(val * 10000) if val < 1e6 else int(val)
                            self.total_likes = max(self.total_likes, like_val)
                            if self.verbose:
                                print(f"              -> update total_likes = {self.total_likes:,}")
                        elif key == '观看':
                            view_val = int(val * 10000) if val < 1e6 else int(val)
                            self.cumulative_views = max(self.cumulative_views, view_val)
                            if self.verbose:
                                print(f"              -> update cumulative_views = {self.cumulative_views:,}")
                        elif key == '灯牌':
                            badge_val = int(val * 10000) if val < 1e6 else int(val)
                            self.light_badges = max(self.light_badges, badge_val)
                            if self.verbose:
                                print(f"              -> update light_badges = {self.light_badges:,}")

                elif method in ('WebcastFansClubMessage', 'WebcastLightMessage',
                                'WebcastFanBadgeMessage', 'WebcastAudienceMessage'):
                    # Various fan-badge / light-up events
                    self.light_badges += 1
                    if self.verbose:
                        print(f"  [BADGE/{method}] total badges now = {self.light_badges}")

                else:
                    # Unknown message – just log method name the first time
                    pass

        except Exception as e:
            # Silently handle parse errors on unknown message types
            pass

    def on_error(self, ws, error):
        print(f"\n[WebSocket Error] {error}")

    def on_close(self, ws, close_status_code, close_msg):
        if self._stop_event.is_set():
            return   # already handled
        print(f"\n[WebSocket Closed] status={close_status_code}, msg={close_msg}")
        self.stream_end_time = datetime.now()
        self.take_post_snapshot()
        self._generate_summary()

    # ──────────────────────────────────────────────────────────────

    def _generate_summary(self):
        """Print the formatted live stream summary."""
        print(f"\n{'='*60}")
        print(f"  📊 直播总结 / Stream Summary")
        print(f"{'='*60}\n")

        if self.verbose:
            print(f"  [Raw counters before formatting]")
            print(f"    total_likes       = {self.total_likes:,}")
            print(f"    cumulative_views  = {self.cumulative_views:,}")
            print(f"    new_follows       = {self.new_follows}")
            print(f"    follower_before   = {self.follower_before:,}")
            print(f"    follower_after    = {self.follower_after:,}")
            print(f"    new_members       = {self.new_members}")
            print(f"    light_badges      = {self.light_badges}")
            print(f"    peak_viewers      = {self.peak_viewers:,}")
            print(f"    viewer_samples    = {len(self.viewer_samples)}")
            print(f"    displayLong entries = {len(self.display_long_history)}")
            print(f"    gift_events        = {len(self.gift_events)}")
            print()

        # ── compute averages ──────────────────────────────────────
        avg_viewers = 0
        if self.viewer_samples:
            avg_viewers = sum(self.viewer_samples) // len(self.viewer_samples)

        # ── follower delta ────────────────────────────────────────
        follower_delta = self.follower_after - self.follower_before
        if follower_delta < 0:
            follower_delta = self.new_follows   # fallback

        # ── stream duration ───────────────────────────────────────
        duration_str = ""
        if self.stream_start_time and self.stream_end_time:
            delta = self.stream_end_time - self.stream_start_time
            mins, secs = divmod(int(delta.total_seconds()), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                duration_str = f"{hours}小时{mins}分钟"
            else:
                duration_str = f"{mins}分钟{secs}秒"

        # ── Format output ─────────────────────────────────────────

        # 主播名称
        anchor = self.anchor_nickname or f"直播 {self.live_id}"
        print(f"📺 主播：【{anchor}】直播结束")

        # 累计观看
        views_wan = self._try_get_wan('观看')
        if views_wan:
            print(f"👀 本场直播累计观看：{views_wan}人")
        elif self.cumulative_views:
            print(f"👀 本场直播累计观看：{fmt_wan(self.cumulative_views)}人")
        else:
            print(f"👀 本场直播累计观看：--")

        # 点赞
        likes_wan = self._try_get_wan('点赞')
        if likes_wan:
            print(f"👍 本场直播点赞数据：{likes_wan}")
        elif self.total_likes:
            print(f"👍 本场直播点赞数据：{fmt_wan(self.total_likes)}")
        else:
            print(f"👍 本场直播点赞数据：--")

        # 新增粉丝 (in 万 unit like "52.36万人")
        print(f"📈 本场新增粉丝：{fmt_wan(follower_delta)}人")

        # 粉丝变化
        print(f"（粉丝：{fmt_wan(self.follower_before)} ➡️ {fmt_wan(self.follower_after)}）")

        # 最高在线
        peak_wan = self._try_get_wan('最高在线')
        if peak_wan:
            print(f"🔥 最高在线：{peak_wan}人")
        elif self.peak_viewers:
            print(f"🔥 最高在线：{fmt_wan(self.peak_viewers)}人")
        else:
            print(f"🔥 最高在线：--")

        # 平均在线
        print(f"📊 平均在线：{fmt_wan(avg_viewers)}人")

        # 今日新增粉丝团 (raw number like "254677人")
        members_wan = self._try_get_wan('粉丝团')
        if members_wan:
            raw_num = members_wan.replace('万', '').replace(',', '')
            print(f"🌟 今日新增粉丝团：{raw_num}人")
        else:
            print(f"🌟 今日新增粉丝团：{self.new_members}人")

        # 今日点亮灯牌 (raw number like "368118人")
        badge_wan = self._try_get_wan('灯牌')
        if badge_wan:
            raw_num = badge_wan.replace('万', '').replace(',', '')
            print(f"💡 今日点亮灯牌：{raw_num}人")
        else:
            print(f"💡 今日点亮灯牌：{self.light_badges}人")

        # Duration
        if duration_str:
            print(f"⏱ 直播时长：{duration_str}")

        # Top gifts (bonus)
        if self.gift_events:
            # Aggregate gifts by name
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
        """Try to extract a 万-formatted value from displayLong history."""
        for dl in reversed(self.display_long_history):
            parsed = parse_display_long(dl)
            if key in parsed:
                val = parsed[key]
                return f"{val:.1f}万" if val == int(val) else f"{val:.2f}万"
        return None

    # ──────────────────────────────────────────────────────────────

    def start(self):
        """Connect to the live room and start recording."""
        print(f"\n{'='*60}")
        print(f"  🎬 Live Summary Recorder (verbose mode)")
        print(f"  🔗 https://live.douyin.com/{self.live_id}")
        print(f"{'='*60}\n")
        self.take_pre_snapshot()
        self._connect_ws()

    def _connect_ws(self):
        """Establish WebSocket connection."""
        try:
            room_info = DouyinAPI.get_live_info(self.auth_, self.live_id)
            room_id = room_info['room_id']
            user_id = room_info['user_id']
            ttwid = room_info['ttwid']
        except Exception as e:
            print(f"[Error] Failed to get live room info: {e}")
            print("  Make sure the live room exists and cookies are valid.")
            return

        params = Params()
        res = DouyinAPI.get_webcast_detail(
            self.auth_, str(user_id), room_id,
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
            cookie=self.auth_.cookie_str,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )

        print(f"  Connecting to live.douyin.com/{self.live_id} ...")
        try:
            self.ws.run_forever(origin='https://live.douyin.com')
        except KeyboardInterrupt:
            print("\n\n[User interrupted]")
        finally:
            self._stop_event.set()
            # If stream didn't close naturally, generate summary anyway
            if self.stream_end_time is None:
                self.stream_end_time = datetime.now()
                self.take_post_snapshot()
                if self.total_likes > 0 or self.viewer_samples:
                    self._generate_summary()


# ======================================================================
# CLI entry point
# ======================================================================

def main():
    if len(sys.argv) < 2:
        print("Usage: python dy_live/live_summary.py <live_id>")
        print("")
        print("Examples:")
        print("  python dy_live/live_summary.py 571821134948")
        print("")
        print("Environment variables required:")
        print("  DY_LIVE_COOKIES  – Douyin cookie string with access to live rooms")
        print("  (set them in a .env file or export them)")
        sys.exit(1)

    live_id = sys.argv[1]

    # Load auth from environment
    common_util.load_env()
    auth = common_util.dy_live_auth or common_util.dy_auth
    if auth is None:
        print("[Error] Could not initialise Douyin auth. Check your .env file.")
        print("        Required: DY_LIVE_COOKIES=xxx")
        sys.exit(1)

    verbose = '--quiet' not in sys.argv
    recorder = LiveSummaryRecorder(live_id, auth, verbose=verbose)
    recorder.start()


if __name__ == '__main__':
    main()