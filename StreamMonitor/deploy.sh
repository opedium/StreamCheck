#!/bin/bash
# =============================================
# StreamMonitor Deployment Script (Linux)
# Installs and configures the systemd service
# Run as root: sudo bash deploy.sh
# =============================================

set -e

# --- Configuration (CHANGE THESE) ---
PROJECT_SOURCE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/StreamCheck/StreamMonitor"
SERVICE_NAME="streammonitor"
PYTHON_BIN="/usr/bin/python3"
LOG_DIR="/var/log/streammonitor"

echo "========================================"
echo "  StreamMonitor Deployment"
echo "========================================"
echo ""
echo "Source:  $PROJECT_SOURCE"
echo "Install: $INSTALL_DIR"
echo ""

# --- 1. Install system dependencies ---
echo "[1/6] Installing Python dependencies..."
pip3 install --upgrade pip -q
pip3 install -r "$PROJECT_SOURCE/requirements.txt" -q

# Also install deps from sibling projects if they exist
if [ -f "$PROJECT_SOURCE/../Douyin_Spider/requirements.txt" ]; then
    pip3 install -r "$PROJECT_SOURCE/../Douyin_Spider/requirements.txt" -q
fi

# --- 2. Copy project files ---
echo "[2/6] Copying project files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r "$PROJECT_SOURCE"/*.py "$INSTALL_DIR/" 2>/dev/null || true
cp -r "$PROJECT_SOURCE"/.env "$INSTALL_DIR/" 2>/dev/null || true
cp -r "$PROJECT_SOURCE"/.gitignore "$PROJECT_SOURCE" 2>/dev/null || true

# --- 3. Create log directory ---
echo "[3/6] Creating log directory..."
mkdir -p "$LOG_DIR"
chmod 755 "$LOG_DIR"

# --- 4. Install systemd service ---
echo "[4/6] Installing systemd service..."
cp "$PROJECT_SOURCE/streammonitor.service" "/etc/systemd/system/$SERVICE_NAME.service"

# Update paths in the service file to match INSTALL_DIR
sed -i "s|/opt/StreamCheck/StreamMonitor|$INSTALL_DIR|g" "/etc/systemd/system/$SERVICE_NAME.service"

# Set the working directory in the service to INSTALL_DIR
sed -i "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" "/etc/systemd/system/$SERVICE_NAME.service"

# Update ExecStart
sed -i "s|ExecStart=.*|ExecStart=$PYTHON_BIN $INSTALL_DIR/main.py --record-stats|" "/etc/systemd/system/$SERVICE_NAME.service"

systemctl daemon-reload

# --- 5. Enable service to start on boot ---
echo "[5/6] Enabling service to start on boot..."
systemctl enable "$SERVICE_NAME.service"

# --- 6. Start the service ---
echo "[6/6] Starting service..."
systemctl restart "$SERVICE_NAME.service"

echo ""
echo "========================================"
echo "  Deployment complete!"
echo "========================================"
echo ""
echo "Check status:  sudo systemctl status $SERVICE_NAME"
echo "View logs:     sudo journalctl -u $SERVICE_NAME -f"
echo "Stop service:  sudo systemctl stop $SERVICE_NAME"
echo "Restart:       sudo systemctl restart $SERVICE_NAME"
echo ""
echo "Log file:      $LOG_DIR/streammonitor.log"
echo "Error log:     $LOG_DIR/streammonitor-error.log"
echo ""

# Verify service is running
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME.service"; then
    echo "[OK] Service is running."
else
    echo "[WARN] Service may not have started. Check: sudo journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi