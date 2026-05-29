#!/bin/bash
# =============================================
# StreamMonitor Deployment Script (Linux)
# Installs and configures the systemd service
# Run as root: sudo bash deploy.sh
# =============================================

set -e

# --- Configuration ---
PROJECT_SOURCE="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$PROJECT_SOURCE/.." && pwd)"
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

# --- 0. Detect pip command ---
echo "[0/7] Checking Python/pip..."
if command -v pip3 &> /dev/null; then
    PIP_CMD="pip3"
elif command -v pip &> /dev/null; then
    PIP_CMD="pip"
else
    echo "  pip not found. Installing python3-pip..."
    apt-get update -qq
    apt-get install -y -qq python3-pip
    PIP_CMD="pip3"
fi
echo "  Using: $PIP_CMD"

# --- 1. Install Python dependencies ---
echo "[1/7] Installing Python dependencies..."
$PIP_CMD install --upgrade pip -q
$PIP_CMD install -r "$PROJECT_SOURCE/requirements.txt" -q
echo "  Done."

# --- 2. Copy project files ---
echo "[2/7] Copying project files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$PROJECT_SOURCE"/*.py "$INSTALL_DIR/" 2>/dev/null || true
cp "$PROJECT_SOURCE"/.env "$INSTALL_DIR/" 2>/dev/null || true

# Copy the Douyin_Spider dependency (needed for protobuf/WebSocket modules)
if [ -d "$PROJECT_ROOT/Douyin_Spider" ]; then
    echo "  Also copying Douyin_Spider dependency..."
    cp -r "$PROJECT_ROOT/Douyin_Spider" "$INSTALL_DIR/../" 2>/dev/null || true
    # Install its requirements too
    if [ -f "$PROJECT_ROOT/Douyin_Spider/requirements.txt" ]; then
        $PIP_CMD install -r "$PROJECT_ROOT/Douyin_Spider/requirements.txt" -q
    fi
fi

# --- 3. Create log directory ---
echo "[3/7] Creating log directory..."
mkdir -p "$LOG_DIR"
chmod 755 "$LOG_DIR"

# --- 4. Install systemd service ---
echo "[4/7] Installing systemd service..."
cp "$PROJECT_SOURCE/streammonitor.service" "/etc/systemd/system/$SERVICE_NAME.service"

# Update paths in the service file
sed -i "s|WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" "/etc/systemd/system/$SERVICE_NAME.service"
sed -i "s|ExecStart=.*|ExecStart=$PYTHON_BIN $INSTALL_DIR/main.py --record-stats|" "/etc/systemd/system/$SERVICE_NAME.service"

systemctl daemon-reload

# --- 5. Enable service to start on boot ---
echo "[5/7] Enabling service to start on boot..."
systemctl enable "$SERVICE_NAME.service"

# --- 6. Start the service ---
echo "[6/7] Starting service..."
systemctl restart "$SERVICE_NAME.service"

# --- 7. Print status ---
echo "[7/7] Checking status..."
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME.service"; then
    echo "[OK] Service is running."
else
    echo "[WARN] Service may not have started. Check: sudo journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

echo ""
echo "========================================"
echo "  Deployment complete!"
echo "========================================"
echo ""
echo "  Check status:  sudo systemctl status $SERVICE_NAME"
echo "  View logs:     sudo journalctl -u $SERVICE_NAME -f"
echo "  Stop service:  sudo systemctl stop $SERVICE_NAME"
echo "  Restart:       sudo systemctl restart $SERVICE_NAME"
echo ""
echo "  Log file:      $LOG_DIR/streammonitor.log"
echo "  Error log:     $LOG_DIR/streammonitor-error.log"
echo ""