# StreamMonitor - Douyin Live Stream Weibo Notifier

Continuously monitors a Douyin live stream and automatically posts Weibo notifications when the stream goes **live** and when it goes **offline**.

## Architecture

This project integrates two existing codebases:
- **Douyin_Spider** - for checking live stream status via `DouyinAPI.get_live_info()`
- **WeiboClient** - for posting Weibo notifications via `WeiBoClient.post_tweet()`

## How It Works

1. On startup, checks the current status of the stream (live or offline) — no notification is sent for the initial state.
2. Periodically polls `DouyinAPI.get_live_info()` at a configurable interval (default: 60 seconds).
3. Detects transitions:
   - **Offline → Live**: Posts a "stream started" Weibo with the room title and link.
   - **Live → Offline**: Posts a "stream ended" Weibo.
4. Logs all notifications to `notification_log.json`.

## Setup

### 1. Install Dependencies

```bash
cd StreamMonitor
pip install -r ../Douyin_Spider/requirements.txt
pip install -r ../WeiboClient/requirements.txt
pip install python-dotenv loguru
```

### 2. Configure

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your:
- **DY_LIVE_COOKIES**: Douyin cookie (found via browser dev tools when on `live.douyin.com`)
- **WEIBO_COOKIE**: Weibo web cookie (found via browser dev tools when logged into `weibo.com`)
- **DY_LIVE_ID**: The numeric room ID from a Douyin live URL, e.g. `571821134948` from `https://live.douyin.com/571821134948`

### 3. Run

```bash
# Run continuously (default interval: 60s)
python main.py

# Run a single check (useful for testing)
python main.py --once

# All options
python main.py --live-id 571821134948 --dy-cookie "xxx" --weibo-cookie "yyy" --interval 30
```

## Custom Templates

You can customize the Weibo text using the `--live-template` and `--offline-template` options
or by setting `LIVE_TEMPLATE` / `OFFLINE_TEMPLATE` in `.env`.

Available template variables:
- `{title}` — Live room title
- `{live_id}` — Live room ID
- `{room_id}` — Internal room ID

## Notification Log

All notifications are recorded in `notification_log.json` with timestamp, event type, and success status.