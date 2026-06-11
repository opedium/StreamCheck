# StreamMonitor 常见故障排查指南

> 每次重启前确保通过 PM2 操作：`pm2 restart streammonitor`
> 不要直接 `kill` 进程——PM2 会立即启动一个新实例，导致双进程写入同一 JSON 文件造成数据覆盖。

---

## 1. 关注数（new_follows）不增长

**症状：** `新增关注` 一直卡在某个数字，直播中没有任何变化。

**原因：** `_get_new_follows()` 依赖 SocialMessage protobuf 中的 `followCount` 字段。
这个字段在 WebSocket 会话中可能只更新一两次后就停滞了。然后 `ws_follow_last - ws_follow_first`
差值不再变化，导致 `new_follows` 卡死。

**调试方法：**
```bash
# 看日志中每个关注事件的详细数据
grep '\[FOLLOW\]' /tmp/streammonitor.log | tail -10

# 关注事件计数器在增加（per_event_total）但是 ws_delta 没变
# 正常输出应该类似：
# [FOLLOW] user=xxx, followCount=8,079,120, per_event_total=15, ws_delta=628
```

**修复方法（代码已内置）：**
- `_get_new_follows()` 使用 `max(http_delta, ws_delta_if_not_stalled)`——HTTP API 作为权威基线，WS followCount 实时补充
- WS 会话重置检测：如果新 WS 会话的 followCount 低于旧会话的存储值，自动清空 WS 计数器

**手动校正方法：**
```bash
# 创建 seed_override.json 手动校正 follower_before
# follower_before = follower_after - 预计的丢失关注数
ssh root@167.99.73.192 'python3 -c "
import json
seed = {
    \"follower_before\": 8078494,    # ← 改为 follower_after - 628
    \"follower_after\": 8079122,     # ← 当前的 follower_after
    \"stream_start_time\": \"2026-06-09T21:51:29.368692\",
}
with open(\"/root/StreamCheck/StreamMonitor/seed_override.json\", \"w\") as f:
    json.dump(seed, f, ensure_ascii=False, indent=2)
print(\"OK\")
" && pm2 restart streammonitor'
```
种子文件会在下一次 `_write_live_stats_json()` 时自动应用并删除。

---

## 2. 礼物数据在进程重启后丢失

**症状：** `热门礼物` 在 PM2 重启后显示全新数据，之前的累计礼物全都消失。

**原因：** `gift_events` 仅保存在进程内存中，不写入 CSV。进程重启后礼物数据从零开始。

**影响范围：** PM2 重启、bug 修复部署、进程崩溃后都会丢失。

**诊断方法：**
```bash
# 查看 CSV 确认礼物事件计数
tail -5 stats_timeseries.csv | awk -F',' '{print $1, $18}'
# 第 18 列是 gift_event_count —— 可以看到重启前后数字回落到低位
```

**当前回避方案：**
- 尽量不在直播过程中部署更新。等直播结束、`live_stats.json` 写入最终状态后再操作。
- 如果必须重启，损失仅限于重启前最近一次 CSV 写入后的礼物（最多 60 秒）。

---

## 3. 双进程竞态写入 JSON

**症状：** `live_stats.json` 的数据在两种值之间来回跳变，或者种子覆盖值被另一个进程覆盖。

**原因：** StreamMonitor 同时被 PM2 和手动 SSH 命令启动。两个进程同时读取/写入 `live_stats.json`。

**诊断方法：**
```bash
# 检查是否有多个 main.py 进程
ps aux | grep 'main.py.*record-stats' | grep -v grep
# 应该有且仅有 1 行
```

**修复方法：**
```bash
# 永远通过 PM2 操作，不要直接 python3 main.py &
pm2 restart streammonitor

# 查看 PM2 管理的进程列表
pm2 status

# 如果发现有两个 PM2 进程，杀掉多余的
pm2 delete streammonitor
pm2 start main.py --name streammonitor -- --record-stats --verbose
```

---

## 4. 直播结束后 Weibo 没有发总结

**症状：** 主播下播了，但是 Weibo 没有自动发总结通知。

**原因链及排查：**

```bash
# 1. 检查 cookie 是否过期（最常见原因）
journalctl -u streammonitor -n 50 --no-pager | grep -i 'cookie\|auth\|expir'

# 2. 检查 WebSocket 是否正常断开
grep '_on_close\|handle_offline\|summary_posted' /tmp/streammonitor.log | tail -10

# 3. 检查 sec_uid 是否缺失
grep 'sec_uid\|SKIPPING' /tmp/streammonitor.log | tail -5

# 4. 检查模板格式错误
grep 'Template.*error\|missing key' /tmp/streammonitor.log | tail -5
```

**典型错误消息及含义：**

| 日志消息 | 原因 | 修复 |
|---|---|---|
| `sec_uid missing from room_info` | HTML 解析失败，没找到主播 sec_uid | 通常是抖音页面结构变化，等一等重新部署 |
| `follower_after still 0 after 3 attempts` | API 调用全部超时或返回空 | 检查网络 / cookie |
| `SKIPPING Weibo post: no stats recorder available` | 直播期间 WS 从未成功连接过 | 检查 `HAS_LIVE_DETAILS` 是否为 True |
| `Template contains unknown placeholder(s)` | 在 `.env` 中自定义了模板，但用了不存在的变量名 | 检查 `{name}` `{views}` 等变量名拼写 |
| `follower_count is 万-rounded — precision loss` | API 返回了 `"8.1万"` 这样的值，精度损失 ±500 | 代码已自动拒绝万-rounded 值 |

---

## 5. WebSocket 频繁断连

**症状：** 日志中出现大量 `WS died mid-stream`、`watchdog`、`WebSocket closed unexpectedly`。

**诊断：**
```bash
# 查看 WS 断开频率
grep -c 'WebSocket closed\|WS died' /tmp/streammonitor.log

# 查看是否有服务器资源瓶颈
free -h
df -h
```

**常见原因：**
- **服务器内存不足（最常见）：** 1GB 内存的 VPS 上，Python + protobuf 解析可能触发 OOM killer
- **抖音 WS 服务器限流：** 长时间连接后主动断开
- **网络波动：** 服务器到抖音机房的网络不稳定

**修复方法：**
- 代码内置了 `attempt_ws_recovery()` ——5 次重试（指数退避 + 抖动），失败后降级到 HTTP API
- CSV crash recovery 确保重启后累计数据不丢失
- 如果频繁断连，考虑升级服务器规格

---

## 6. Cookie 过期导致直播检测失效

**症状：** HTTP 检查返回 `cookie EXPIRED`，进程一直报告 `OFFLINE`。

**自动恢复机制：**
- `CookieRefresher`（单独的 PM2 进程）每隔 6 小时自动用 Playwright 刷新 cookie
- `CookieManager.mark_unhealthy()` 在检测到过期时通知 refresher
- 刷新后新 cookie 通过 `cookies.json` 共享，`_reload_cookies()` 每 5 分钟检查一次

**手动强制刷新：**
```bash
ssh root@167.99.73.192 'python3 -c "
from cookies import CookieManager
mgr = CookieManager()
data = mgr.load()
print(\"Current health:\", data.get(\"health\"))
print(\"Refresh count:\", data.get(\"refresh_count\"))
mgr.mark_unhealthy()
print(\"Marked unhealthy — refresher will pick this up within 5 minutes\")
"'
```

---

## 7. 系统化排障流程

当直播正在进行但数据看起来不对时，按此顺序排查：

```bash
# 步骤 1：确认进程健康
pm2 status
# → streammonitor 应为 online，uptime > 0

# 步骤 2：确认 WS 连接状态
ssh root@167.99.73.192 'cat /root/StreamCheck/StreamMonitor/live_stats.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(\"ws_connected:\", d[\"ws_connected\"]); print(\"live:\", d[\"live\"]); print(\"last_update:\", d[\"last_update\"]); print(\"new_follows:\", d[\"new_follows\"]); print(\"current_viewers:\", d[\"current_viewers\"]); print(\"peak_viewers:\", d[\"peak_viewers\"]); print(\"delta:\", d[\"follower_after\"] - d[\"follower_before\"])"'

# 步骤 3：查看最近错误
grep -i 'error\|fail\|expir\|warn\|FOLLOW' /tmp/streammonitor.log | tail -20

# 步骤 4：检查 CSV 数据一致性
tail -3 stats_timeseries.csv | awk -F',' '{print "time:",$1,"follow_delta:",$10,"viewers:",$4}'

# 步骤 5：如果需要修复数据，用 seed_override.json（见第 1 节）
```

---

## 8. 已知未修复的限制

| 问题 | 影响 | 状态 |
|---|---|---|---|
| 礼物数据不持久化 | PM2 重启后热门礼物丢失 | ✅ 已修复（CSV 持久化 top-3 + 崩溃恢复读取） |
| 关注数卡住（WS followCount 不更新） | `new_follows` 用事件计数器代替 | ✅ 已修复 |
| PM2 重启无优雅关闭 | 强行杀死进程，丢失最新数据 | ✅ 已修复（SIGTERM handler 写入最终状态再退出） |
| 内存无限制增长 | 长时间直播 OOM 崩溃 | ✅ 已修复（gift_events 上限 20000，viewer_samples 上限 1440） |
| Seed 被另一个进程覆盖 | 手动校正失效 | ✅ 已修复（通过 PM2 单进程管理） |
| protobuf 转储日志膨胀 | 日志文件快速增长 | ✅ 已修复（默认关闭，`PROTOBUF_DUMP=1` 启用） |
| cookie 文件明文存储 | 服务器被入侵时 cookie 泄露 | 需使用加密存储或环境变量 |
