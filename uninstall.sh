#!/usr/bin/env bash
# ServerMonitor Safe Uninstallation Script
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${YELLOW}============================================================${NC}"
echo -e "${YELLOW}        ServerMonitor Safe Uninstallation Wizard           ${NC}"
echo -e "${YELLOW}============================================================${NC}"
echo "This will safely stop ServerMonitor services and remove its files."
echo "Target directory: $SCRIPT_DIR"
echo ""

read -p "Are you sure you want to uninstall ServerMonitor? [y/N] " confirm
confirm=${confirm:-N}
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "Uninstallation cancelled."
    exit 0
fi

# Stop systemd service if present
if [ -f "/etc/systemd/system/servermonitor.service" ]; then
    echo "Stopping and disabling systemd service..."
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop servermonitor.service || true
        systemctl disable servermonitor.service || true
    fi
    rm -f "/etc/systemd/system/servermonitor.service"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload || true
    fi
    echo "[✓] systemd service removed."
fi

# Stop PM2 process if named servermonitor or servermonitor-prod
if command -v pm2 >/dev/null 2>&1; then
    for name in "servermonitor" "servermonitor-prod"; do
        if pm2 describe "$name" >/dev/null 2>&1; then
            echo "Stopping PM2 process '$name'..."
            pm2 stop "$name" || true
            pm2 delete "$name" || true
            echo "[✓] PM2 process '$name' removed."
        fi
    done
fi

read -p "Remove configuration (config.json)? [y/N] " rm_config
if [[ "$rm_config" =~ ^[Yy]$ ]]; then
    rm -f "$SCRIPT_DIR/config.json"
    echo "[✓] config.json removed."
fi

echo -e "\n${GREEN}[✓] ServerMonitor uninstallation completed safely.${NC}"
