# Bilibili Video Upload Checker

Monitors Bilibili uploaders and posts a Weibo notification when they upload a new video.

## Setup

1. Copy the example config:
   ```
   copy .env.example .env
   ```

2. Edit `.env` with your values:
   - `BILI_MIDS`: comma-separated Bilibili member IDs (from `https://space.bilibili.com/{mid}`)
   - `WEIBO_COOKIE`: your Weibo web cookie (must be logged into weibo.com)

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

## Usage

```
python checker.py
```

The script runs continuously. On first run, it records each uploader's latest video as a baseline (no notification). When a new video is detected, it posts to Weibo.

## How It Works

- Polls `api.bilibili.com/x/space/wbi/arc/search` every 5 minutes per uploader
- Uses WBI signing (keys rotate daily, auto-cached)
- Compares the latest video's `bvid` against stored state
- Only notifies on genuinely new uploads after monitoring starts

## Files

| File | Purpose |
|------|---------|
| `checker.py` | Main script — loop, state, notifications |
| `wbi_sign.py` | WBI signing (key fetch, mixin key, MD5 signature) |
| `state.json` | Auto-generated — last known video per uploader |
| `notification_log.json` | Auto-generated — notification history |
| `wbi_cache.json` | Auto-generated — cached WBI keys (24h TTL) |
