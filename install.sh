#!/usr/bin/env bash
# ServerMonitor Installation Script
# Lightweight, universal live Linux server telemetry
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}        ServerMonitor v2.0 - Universal Linux Installer     ${NC}"
echo -e "${BLUE}============================================================${NC}"

# Check for Python 3
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}Error: python3 is not installed. Please install Python 3 before continuing.${NC}" >&2
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "Detected Python: ${GREEN}Python ${PY_VERSION}${NC}"

# Determine installation directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_TARGET_DIR="/opt/servermonitor"
if [ "$EUID" -ne 0 ]; then
    DEFAULT_TARGET_DIR="$HOME/.local/share/servermonitor"
fi

TARGET_DIR="${SERVERMONITOR_DIR:-$SCRIPT_DIR}"

echo -e "Installation Directory: ${GREEN}${TARGET_DIR}${NC}"

# Copy files if installing to an external target directory
if [ "$SCRIPT_DIR" != "$TARGET_DIR" ]; then
    mkdir -p "$TARGET_DIR"
    cp "$SCRIPT_DIR/server.py" "$TARGET_DIR/"
    cp "$SCRIPT_DIR/servermonitor" "$TARGET_DIR/"
    cp "$SCRIPT_DIR/config.example.json" "$TARGET_DIR/"
    if [ -f "$SCRIPT_DIR/uninstall.sh" ]; then
        cp "$SCRIPT_DIR/uninstall.sh" "$TARGET_DIR/"
    fi
    chmod +x "$TARGET_DIR/server.py" "$TARGET_DIR/servermonitor"
fi

# Run interactive setup wizard if config.json does not exist
if [ ! -f "$TARGET_DIR/config.json" ]; then
    echo -e "\n${YELLOW}No config.json found. Launching interactive setup...${NC}"
    python3 "$TARGET_DIR/server.py" --setup
else
    echo -e "${GREEN}[✓] Existing config.json found in ${TARGET_DIR}.${NC}"
fi

# Offer systemd installation if root and systemd active
if [ "$EUID" -eq 0 ] && [ -d "/run/systemd/system" ]; then
    read -p "Install as systemd service (/etc/systemd/system/servermonitor.service)? [Y/n] " install_systemd
    install_systemd=${install_systemd:-Y}
    if [[ "$install_systemd" =~ ^[Yy]$ ]]; then
        python3 "$TARGET_DIR/server.py" --generate-systemd > /etc/systemd/system/servermonitor.service
        systemctl daemon-reload
        systemctl enable --now servermonitor.service
        echo -e "${GREEN}[✓] ServerMonitor systemd service enabled and started.${NC}"
        systemctl status servermonitor.service --no-pager
    fi
fi

echo -e "\n${GREEN}============================================================${NC}"
echo -e "${GREEN}  Installation Complete!${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e "Run directly:        ${BLUE}python3 ${TARGET_DIR}/server.py${NC}"
echo -e "Or via CLI wrapper:  ${BLUE}${TARGET_DIR}/servermonitor start${NC}"
echo -e "Check status:        ${BLUE}${TARGET_DIR}/servermonitor status${NC}"
echo -e "SSH Port Forwarding: ${BLUE}${TARGET_DIR}/servermonitor tunnel${NC}"
echo ""
