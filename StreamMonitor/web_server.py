#!/usr/bin/env python3
# coding=utf-8
"""
StreamMonitor Web Server - Health check and status page.
Serves an HTML page and JSON API endpoints to verify the service is running.
Run on a different port (default 5000) from the main monitor process.
"""
import os
import sys
import json
import time
import socket
import argparse
import platform
from datetime import datetime

from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv
from loguru import logger

# ── App startup timestamp ────────────────────────────────────────────
APP_START_TIME = time.time()

# ── Flask app ────────────────────────────────────────────────────────
app = Flask(__name__)


def get_host_ip() -> str:
    """Get the server's primary non-loopback IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        # Doesn't actually connect, just used to determine route
        s.connect(("10.254.254.254", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_uptime() -> str:
    """Get human-readable uptime."""
    elapsed = int(time.time() - APP_START_TIME)
    days, remainder = divmod(elapsed, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


# ── Routes ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the HTML health check page."""
    return render_template("index.html")


@app.route("/api/health")
def api_health():
    """JSON health check endpoint."""
    return jsonify({
        "status": "ok",
        "service": "StreamMonitor",
        "version": "1.0.0",
        "uptime": get_uptime(),
        "uptime_seconds": int(time.time() - APP_START_TIME),
        "server_time": datetime.now().isoformat(),
        "host": get_host_ip(),
        "port": request.host,
    })


@app.route("/api/status")
def api_status():
    """Stream monitor status endpoint - checks if the main monitor process is running."""
    import subprocess
    
    monitor_running = None  # Unknown by default
    monitor_pids = []
    
    # Only check process status on Linux/Unix servers, not on Windows
    if platform.system() != "Windows":
        try:
            # Try PM2 first
            result = subprocess.run(
                ["pm2", "info", "streammonitor"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and "online" in result.stdout.lower():
                monitor_running = True
            else:
                # Try pgrep as fallback
                result = subprocess.run(
                    ["pgrep", "-fa", "StreamMonitor|main.py"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    pids = result.stdout.strip().split("\n")
                    # Filter out grep and web_server processes
                    monitor_pids = [p.split()[0] for p in pids if "web_server" not in p and "grep" not in p]
                    monitor_running = len(monitor_pids) > 0
                else:
                    monitor_running = False
                    
        except Exception as e:
            logger.warning(f"Could not check monitor status: {e}")
            monitor_running = None  # Unknown
    else:
        # On Windows, skip process check or provide simulated status
        logger.debug("Running on Windows - skipping PM2 process check")
        monitor_running = None

    # Load notification log for recent activity
    log_path = os.path.join(os.path.dirname(__file__), "notification_log.json")
    recent_events = []
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                all_events = json.load(f)
            recent_events = all_events[-10:]  # Last 10 events
    except Exception:
        pass

    return jsonify({
        "server": {
            "status": "running",
            "uptime": get_uptime(),
            "uptime_seconds": int(time.time() - APP_START_TIME),
        },
        "monitor": {
            "running": monitor_running,  # Can be True, False, or None (unknown)
            "pids": monitor_pids if isinstance(monitor_running, bool) and monitor_running else [],
        },
        "platform": platform.system(),
        "recent_events": recent_events,
        "time": datetime.now().isoformat(),
    })


@app.errorhandler(404)
def not_found(e):
    """Custom 404 for API routes."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "not_found", "message": "Endpoint not found"}), 404
    return render_template("index.html"), 404


@app.errorhandler(500)
def server_error(e):
    """Custom 500 for API routes."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal_error", "message": "Internal server error"}), 500
    return "Internal Server Error", 500


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="StreamMonitor Web Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port number (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")

    # Load .env config
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)

    args = parser.parse_args()

    # Allow port override from env
    port = int(os.getenv("WEB_PORT", str(args.port)))
    host = os.getenv("WEB_HOST", args.host)
    debug = args.debug or os.getenv("WEB_DEBUG", "").lower() in ("1", "true", "yes")

    logger.info("=" * 50)
    logger.info("  StreamMonitor Web Server")
    logger.info(f"  Listening on {host}:{port}")
    logger.info(f"  Health page: http://{host}:{port}/")
    logger.info(f"  Health API:  http://{host}:{port}/api/health")
    logger.info("=" * 50)

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()