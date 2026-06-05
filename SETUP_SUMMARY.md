# StreamMonitor Setup Complete ✓

**Server:** 167.99.73.192  
**Status:** Running via PM2 (auto-restart enabled)  
**Location:** ~/StreamCheck

## Current Setup

- ✅ Virtual environment created: ~/StreamCheck/venv
- ✅ Dependencies installed (Python 3.12.3)
- ✅ StreamMonitor running on PM2 (Process ID: 2)
- ✅ Configuration file: ~/StreamCheck/StreamMonitor/.env
- ✅ Auto-restart on boot: Enabled
- ✅ Log files: ~/StreamCheck/logs/streammonitor-*.log

## Next Steps: Configure Your Credentials

### 1. Edit Configuration File
SSH into server and edit .env:
\\\ash
ssh root@167.99.73.192
nano ~/StreamCheck/StreamMonitor/.env
\\\

### 2. Required Values to Update

Replace these placeholders:

| Variable | How to Get | Example |
|----------|-----------|---------|
| **DY_LIVE_COOKIES** | 1. Open https://douyin.com (logged in) <br>2. Open DevTools (F12) <br>3. Go to Application → Cookies <br>4. Find and copy the full cookie string | __ac_nonce=... |
| **WEIBO_COOKIE** | 1. Open https://weibo.com (logged in) <br>2. Open DevTools (F12) <br>3. Go to Application → Cookies <br>4. Copy the full cookie string | SINA_FP=... |
| **DY_LIVE_ID** | Open your live room link: https://live.douyin.com/YOUR_LIVE_ID <br>Copy just the number | 571821134948 |
| **CHECK_INTERVAL** | (Optional) How often to check stream status in seconds | 60 (default) |

### 3. Restart the Service
After updating .env:
\\\ash
pm2 restart streammonitor
pm2 logs streammonitor  # View logs
\\\

## Useful PM2 Commands

\\\ash
# View service status
pm2 list

# View real-time logs
pm2 logs streammonitor

# Stop service
pm2 stop streammonitor

# Start service
pm2 start streammonitor

# Restart service
pm2 restart streammonitor

# View service info
pm2 info streammonitor

# Clear logs
pm2 flush

# Delete service
pm2 delete streammonitor
\\\

## Notification Log

All notifications are recorded in:
\\\
~/StreamCheck/StreamMonitor/notification_log.json
\\\

## Important Notes

⚠️ **Disk Usage:** Server is at 88.9% capacity. Consider:
- Archiving old notification logs
- Removing old browser caches
- Running: \pt autoremove\

⚠️ **System Restart Required:** Ubuntu shows a system restart is pending. After updating, consider rebooting.

🔐 **Security:** Keep your cookies secret! Never share .env file or commit it to git.
