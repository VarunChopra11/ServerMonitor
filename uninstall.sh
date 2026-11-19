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

# Stop systemd service ONLY if it belongs to this installation
UNIT_FILE="/etc/systemd/system/servermonitor.service"
if [ -f "$UNIT_FILE" ]; then
    EXISTING_EXEC=$(grep -E "^ExecStart=" "$UNIT_FILE" | head -n1 || true)
    # Strictly match script server.py or servermonitor within this installation directory boundary
    if echo "$EXISTING_EXEC" | grep -E -q "(^|[[:space:]=\"'])$SCRIPT_DIR/(server\.py|servermonitor)([[:space:]\"']|$)"; then
        echo "Found systemd service matching this installation ($SCRIPT_DIR). Stopping and removing..."
        if command -v systemctl >/dev/null 2>&1; then
            systemctl stop servermonitor.service || true
            systemctl disable servermonitor.service || true
        fi
        rm -f "$UNIT_FILE"
        if command -v systemctl >/dev/null 2>&1; then
            systemctl daemon-reload || true
        fi
        echo "[✓] Matching systemd service removed."
    else
        echo "[!] /etc/systemd/system/servermonitor.service does not match this directory ($SCRIPT_DIR)."
        echo "    Leaving systemd service untouched to protect other installations."
    fi
fi

# Stop PM2 process ONLY if its executable path matches this installation
if command -v pm2 >/dev/null 2>&1; then
    MATCHING_PM2_IDS=$(python3 -c "
import json, os, subprocess, sys
try:
    p = subprocess.run(['pm2', 'jlist'], capture_output=True, text=True, check=True)
    proc_list = json.loads(p.stdout)
    target_dir = os.path.abspath(sys.argv[1])
    matches = []
    for proc in proc_list:
        pm_env = proc.get('pm2_env', {})
        exec_path = os.path.abspath(pm_env.get('pm_exec_path', '')) if pm_env.get('pm_exec_path') else ''
        cwd = os.path.abspath(pm_env.get('pm_cwd', '')) if pm_env.get('pm_cwd') else ''
        pm_id = proc.get('pm_id')
        name = proc.get('name', '')

        is_in_target = False
        if exec_path:
            try:
                if os.path.commonpath([target_dir, exec_path]) == target_dir and os.path.basename(exec_path) == 'server.py':
                    is_in_target = True
            except ValueError:
                pass
        if cwd and not is_in_target:
            try:
                if os.path.commonpath([target_dir, cwd]) == target_dir and name.startswith('servermonitor'):
                    is_in_target = True
            except ValueError:
                pass

        if is_in_target:
            matches.append((str(pm_id), name, exec_path))
    for mid, name, ep in matches:
        print(f'{mid}:{name}:{ep}')
except Exception:
    pass
" "$SCRIPT_DIR")

    if [ -n "$MATCHING_PM2_IDS" ]; then
        echo "$MATCHING_PM2_IDS" | while IFS=: read -r mid mname mpath; do
            echo "Stopping matching PM2 process [ID $mid: '$mname' -> $mpath]..."
            pm2 stop "$mid" || true
            pm2 delete "$mid" || true
            echo "[✓] PM2 process [ID $mid: '$mname'] removed."
        done
        pm2 save || true
    else
        echo "[✓] No running PM2 processes match this installation ($SCRIPT_DIR). Existing PM2 processes left untouched."
    fi
fi

# Clean up /etc/servermonitor if it belongs to this installation
if [ -d "/etc/servermonitor" ]; then
    INSTALLED_BY=""
    if [ -f "/etc/servermonitor/.installed_by" ]; then
        INSTALLED_BY=$(cat "/etc/servermonitor/.installed_by" 2>/dev/null || true)
    fi
    if [ "$INSTALLED_BY" = "$SCRIPT_DIR" ] || [ -z "$INSTALLED_BY" ]; then
        read -p "Remove service configuration (/etc/servermonitor)? [y/N] " rm_etc
        if [[ "$rm_etc" =~ ^[Yy]$ ]]; then
            rm -rf "/etc/servermonitor"
            echo "[✓] /etc/servermonitor removed."
        fi
    fi
fi

# Clean up /var/lib/servermonitor if it belongs to this installation
if [ -d "/var/lib/servermonitor" ]; then
    INSTALLED_BY=""
    if [ -f "/var/lib/servermonitor/.installed_by" ]; then
        INSTALLED_BY=$(cat "/var/lib/servermonitor/.installed_by" 2>/dev/null || true)
    fi
    if [ "$INSTALLED_BY" = "$SCRIPT_DIR" ] || [ -z "$INSTALLED_BY" ]; then
        read -p "Remove service state directory (/var/lib/servermonitor)? [y/N] " rm_var
        if [[ "$rm_var" =~ ^[Yy]$ ]]; then
            rm -rf "/var/lib/servermonitor"
            echo "[✓] /var/lib/servermonitor removed."
        fi
    fi
fi

read -p "Remove local configuration ($SCRIPT_DIR/config.json)? [y/N] " rm_config
if [[ "$rm_config" =~ ^[Yy]$ ]]; then
    rm -f "$SCRIPT_DIR/config.json"
    echo "[✓] config.json removed."
fi

echo -e "\n${GREEN}[✓] ServerMonitor uninstallation completed safely.${NC}"
