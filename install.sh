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

# Check for Python 3.8+
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}Error: python3 is not installed. Please install Python 3.8 or newer before continuing.${NC}" >&2
    exit 1
fi

PY_OK=$(python3 -c "import sys; print('1' if sys.version_info >= (3, 8) else '0')")
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")

if [ "$PY_OK" != "1" ]; then
    echo -e "${RED}Error: ServerMonitor requires Python 3.8 or newer (detected Python ${PY_VERSION}).${NC}" >&2
    exit 1
fi
echo -e "Detected Python: ${GREEN}Python ${PY_VERSION}${NC} (compatible)"

# Helper: Create timestamped backup of an existing file
backup_file() {
    local file="$1"
    if [ -f "$file" ]; then
        local timestamp
        timestamp=$(date +%Y%m%d%H%M%S)
        cp -p "$file" "${file}.bak.${timestamp}"
        echo -e "${YELLOW}[!] Created backup of existing file: ${file}.bak.${timestamp}${NC}"
    fi
}

# Determine installation directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${SERVERMONITOR_DIR:-$SCRIPT_DIR}"

echo -e "Installation Directory: ${GREEN}${TARGET_DIR}${NC}"

# Determine service user for non-root execution
SERVICE_USER="${SUDO_USER:-$USER}"
if [ "$SERVICE_USER" = "root" ] && [ "$EUID" -eq 0 ]; then
    SERVICE_USER="nobody"
fi

# Prepare system directories if running as root
if [ "$EUID" -eq 0 ]; then
    mkdir -p /etc/servermonitor /var/lib/servermonitor
    chown -R "$SERVICE_USER":"$SERVICE_USER" /etc/servermonitor /var/lib/servermonitor 2>/dev/null || chown -R "$SERVICE_USER" /etc/servermonitor /var/lib/servermonitor
    chmod 750 /etc/servermonitor /var/lib/servermonitor
    echo "$TARGET_DIR" > /etc/servermonitor/.installed_by
    echo "$TARGET_DIR" > /var/lib/servermonitor/.installed_by
fi

# Copy files if installing to an external target directory
if [ "$SCRIPT_DIR" != "$TARGET_DIR" ]; then
    mkdir -p "$TARGET_DIR"
    for file_name in "server.py" "servermonitor" "config.example.json" "uninstall.sh"; do
        if [ -f "$TARGET_DIR/$file_name" ]; then
            backup_file "$TARGET_DIR/$file_name"
        fi
        if [ -f "$SCRIPT_DIR/$file_name" ]; then
            cp "$SCRIPT_DIR/$file_name" "$TARGET_DIR/"
        fi
    done
    chmod +x "$TARGET_DIR/server.py" "$TARGET_DIR/servermonitor"
fi

# Determine target config path
CONFIG_DEST=""
if [ "$EUID" -eq 0 ]; then
    CONFIG_DEST="/etc/servermonitor/config.json"
fi

# Run interactive setup wizard if configuration does not exist
if [ -n "$CONFIG_DEST" ] && [ ! -f "$CONFIG_DEST" ] && [ ! -f "$TARGET_DIR/config.json" ]; then
    echo -e "\n${YELLOW}No configuration found. Launching interactive setup...${NC}"
    python3 "$TARGET_DIR/server.py" --setup --config "$CONFIG_DEST"
    if [ -f "$CONFIG_DEST" ]; then
        chown "$SERVICE_USER":"$SERVICE_USER" "$CONFIG_DEST" 2>/dev/null || chown "$SERVICE_USER" "$CONFIG_DEST"
        chmod 600 "$CONFIG_DEST"
    fi
elif [ -z "$CONFIG_DEST" ] && [ ! -f "$TARGET_DIR/config.json" ]; then
    echo -e "\n${YELLOW}No config.json found. Launching interactive setup...${NC}"
    python3 "$TARGET_DIR/server.py" --setup
else
    echo -e "${GREEN}[✓] Existing configuration found.${NC}"
    # Ensure existing configuration is readable by SERVICE_USER if run as root
    if [ "$EUID" -eq 0 ]; then
        if [ -f "$CONFIG_DEST" ]; then
            chown "$SERVICE_USER":"$SERVICE_USER" "$CONFIG_DEST" 2>/dev/null || chown "$SERVICE_USER" "$CONFIG_DEST"
            chmod 600 "$CONFIG_DEST"
        fi
        if [ -f "$TARGET_DIR/config.json" ]; then
            chown "$SERVICE_USER":"$SERVICE_USER" "$TARGET_DIR/config.json" 2>/dev/null || chown "$SERVICE_USER" "$TARGET_DIR/config.json"
        fi
    fi
fi

# Offer systemd installation if root and systemd active
if [ "$EUID" -eq 0 ] && [ -d "/run/systemd/system" ]; then
    UNIT_FILE="/etc/systemd/system/servermonitor.service"
    can_install_unit=true

    if [ -f "$UNIT_FILE" ]; then
        echo -e "\n${YELLOW}[!] An existing systemd service was detected at ${UNIT_FILE}.${NC}"
        EXISTING_EXEC=$(grep -E "^ExecStart=" "$UNIT_FILE" | head -n1 | cut -d= -f2- || true)
        echo -e "    Existing ExecStart: ${EXISTING_EXEC}"
        EXISTING_SCRIPT=$(echo "$EXISTING_EXEC" | awk '{for(i=1;i<=NF;i++) if($i ~ /server\.py$/) print $i}')
        TARGET_SCRIPT="$TARGET_DIR/server.py"

        if [ "$EXISTING_SCRIPT" != "$TARGET_SCRIPT" ]; then
            echo -e "${RED}[WARNING] The existing unit does NOT point to this installation ('$EXISTING_SCRIPT' != '$TARGET_SCRIPT').${NC}"
            echo -e "Overwriting it may break an existing service or another installation."
            read -p "Overwrite existing service anyway? [y/N]: " overwrite_unit
            overwrite_unit=${overwrite_unit:-N}
            if [[ ! "$overwrite_unit" =~ ^[Yy]$ ]]; then
                echo "Skipping systemd installation to preserve the existing service."
                can_install_unit=false
            fi
        fi
    fi

    if [ "$can_install_unit" = true ]; then
        read -p "Install as systemd service (/etc/systemd/system/servermonitor.service)? [Y/n] " install_systemd
        install_systemd=${install_systemd:-Y}
        if [[ "$install_systemd" =~ ^[Yy]$ ]]; then
            if [ -f "$UNIT_FILE" ]; then
                backup_file "$UNIT_FILE"
            fi
            python3 "$TARGET_DIR/server.py" --generate-systemd --user "$SERVICE_USER" > "$UNIT_FILE"
            systemctl daemon-reload
            systemctl enable --now servermonitor.service
            echo -e "${GREEN}[✓] ServerMonitor systemd service enabled and started.${NC}"
            systemctl status servermonitor.service --no-pager
        fi
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
