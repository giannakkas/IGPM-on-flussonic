#!/bin/bash
# ──────────────────────────────────────────────
# IGMP Watcher Installer
# Run on the Flussonic server as root
# ──────────────────────────────────────────────
set -e

INSTALL_DIR="/opt/igmp-watcher"
SERVICE_NAME="igmp-watcher"

echo "=================================="
echo " IGMP Watcher — Installer"
echo "=================================="

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Must run as root"
    exit 1
fi

# Create install directory
echo "[1/5] Creating install directory..."
mkdir -p "$INSTALL_DIR"

# Copy files
echo "[2/5] Copying files..."
cp igmp_watcher.py "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/igmp_watcher.py"

# Only copy config if it doesn't exist (don't overwrite user edits)
if [ ! -f "$INSTALL_DIR/config.ini" ]; then
    cp config.ini "$INSTALL_DIR/"
    echo "       config.ini installed — EDIT THIS FILE before starting!"
else
    echo "       config.ini already exists — not overwriting"
    echo "       New config saved as config.ini.new for reference"
    cp config.ini "$INSTALL_DIR/config.ini.new"
fi

# Create log file
echo "[3/5] Setting up logging..."
touch /var/log/igmp-watcher.log

# Install systemd service
echo "[4/5] Installing systemd service..."
cp igmp-watcher.service /etc/systemd/system/
systemctl daemon-reload

# Detect network interface
echo ""
echo "[5/5] Network interface detection:"
echo "────────────────────────────────────"
ip -brief addr show | grep -v "lo " | grep UP
echo "────────────────────────────────────"
echo ""

echo "=================================="
echo " Installation complete!"
echo "=================================="
echo ""
echo " NEXT STEPS:"
echo ""
echo " 1. Edit the config file:"
echo "    nano $INSTALL_DIR/config.ini"
echo ""
echo "    → Set 'interface' to your LAN interface (from the list above)"
echo "    → Set Flussonic credentials if needed"
echo ""
echo " 2. Make sure Flussonic streams have on_demand 30:"
echo "    (In flussonic.conf or via the UI)"
echo ""
echo " 3. Start the service:"
echo "    systemctl start $SERVICE_NAME"
echo "    systemctl enable $SERVICE_NAME"
echo ""
echo " 4. Check logs:"
echo "    journalctl -u $SERVICE_NAME -f"
echo "    tail -f /var/log/igmp-watcher.log"
echo ""
echo " 5. Test: tune a MAG to any channel and watch the log"
echo ""
