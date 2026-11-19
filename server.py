#!/usr/bin/env python3
"""
ServerMonitor: Production-Ready Universal Linux Monitoring Application
Lightweight, zero-dependency, on-demand live system telemetry for Linux servers,
VMs, VPS instances, bare-metal hardware, and containers.
"""

import argparse
import collections
import copy
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Python version compatibility enforcement (Python 3.8+)
if sys.version_info < (3, 8):
    sys.stderr.write(
        f"Error: ServerMonitor requires Python 3.8 or newer "
        f"(detected Python {sys.version_info.major}.{sys.version_info.minor})\n"
    )
    sys.exit(1)

# ============================================================
# CONSTANTS & GLOBAL STATE
# ============================================================

VERSION = "2.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

HOST = DEFAULT_HOST
PORT = DEFAULT_PORT

# Sampling state
_previous_cpu = None
_previous_cpu_detailed = None
_previous_network = None
_previous_sample_time = None

# PM2 Cache state (PR #1 compatible)
_pm2_cache_data = None
_pm2_cache_time = 0.0
_pm2_cache_lock = threading.Lock()
PM2_CACHE_TTL = 5.0

# LLM state (PR #1 compatible)
_llm_lock = threading.Lock()

# Speedtest state & cooldown
_speedtest_lock = threading.Lock()
_speedtest_last_run = 0.0
_speedtest_cooldown = 60.0

# Global active collector instance
_global_collector = None
_global_config = None


# ============================================================
MIN_TOKEN_LENGTH = 24

# ============================================================
# CONFIGURATION MANAGEMENT
# ============================================================

class Config:
    """Manages application configuration with sensible defaults."""

    DEFAULT_CONFIG = {
        "host": "127.0.0.1",
        "port": 8080,
        "auth_token": "",
        "sample_interval": 2.0,
        "inactivity_timeout": 15.0,
        "history_points": 60,
        "speedtest_enabled": True,
        "speedtest_cooldown": 60.0,
        "modules": {
            "cpu": True,
            "memory": True,
            "storage": True,
            "network": True,
            "thermals": True,
            "battery": True,
            "services": True,
            "pm2": True,
            "systemd": True,
            "docker": False,
            "antigravity": False,
        },
        "monitored_systemd_units": ["ssh", "sshd", "nginx", "caddy", "docker"],
        "monitored_processes": ["sshd"],
        "monitored_http_urls": [],
        "trusted_proxies": ["127.0.0.1", "::1"],
        "allowed_hosts": ["127.0.0.1", "localhost", "::1"]
    }

    def __init__(self, config_file=None):
        self.config_file = config_file or self._find_config_file()
        self.data = copy.deepcopy(self.DEFAULT_CONFIG)
        if self.config_file and os.path.exists(self.config_file):
            self.load()

    def _find_config_file(self):
        env_cfg = os.environ.get("SERVERMONITOR_CONFIG")
        if env_cfg and os.path.exists(env_cfg):
            return env_cfg

        # Support systemd-provided state and configuration directories
        for env_var in ("CONFIGURATION_DIRECTORY", "STATE_DIRECTORY"):
            d = os.environ.get(env_var)
            if d:
                cfg_path = os.path.join(d, "config.json")
                if os.path.exists(cfg_path):
                    return cfg_path

        candidates = [
            "/etc/servermonitor/config.json",
            "/var/lib/servermonitor/config.json",
            os.path.expanduser("~/.config/servermonitor/config.json"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c

        if os.environ.get("CONFIGURATION_DIRECTORY"):
            return os.path.join(os.environ["CONFIGURATION_DIRECTORY"], "config.json")
        if os.environ.get("STATE_DIRECTORY"):
            return os.path.join(os.environ["STATE_DIRECTORY"], "config.json")

        if os.path.exists("/etc/servermonitor") and os.access("/etc/servermonitor", os.W_OK):
            return "/etc/servermonitor/config.json"
        if os.path.exists("/var/lib/servermonitor") and os.access("/var/lib/servermonitor", os.W_OK):
            return "/var/lib/servermonitor/config.json"
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

    def load(self):
        try:
            with open(self.config_file, "r") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    # Recursive deep merge for nested dictionaries
                    for k, v in loaded.items():
                        if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                            self.data[k].update(copy.deepcopy(v))
                        else:
                            self.data[k] = copy.deepcopy(v)
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(f"Fatal: Corrupt or unparseable configuration file '{self.config_file}': {e}") from e
        except OSError as e:
            raise RuntimeError(f"Fatal: Failed to read configuration file '{self.config_file}': {e}") from e

    def save(self):
        try:
            dirname = os.path.dirname(os.path.abspath(self.config_file))
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname, mode=0o700, exist_ok=True)
            tmp_path = f"{self.config_file}.tmp.{os.getpid()}_{int(time.time() * 1000)}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(tmp_path, flags, 0o600)
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_file)
            return True
        except Exception as e:
            sys.stderr.write(f"Error: Failed to save config to {self.config_file}: {e}\n")
            return False

    @property
    def host(self):
        return self.data.get("host", self.DEFAULT_CONFIG["host"])

    @host.setter
    def host(self, val):
        self.data["host"] = str(val)

    @property
    def port(self):
        return int(self.data.get("port", self.DEFAULT_CONFIG["port"]))

    @port.setter
    def port(self, val):
        self.data["port"] = int(val)

    @property
    def auth_token(self):
        return self.data.get("auth_token", "")

    @auth_token.setter
    def auth_token(self, val):
        self.data["auth_token"] = str(val or "")

    @property
    def sample_interval(self):
        return float(self.data.get("sample_interval", self.DEFAULT_CONFIG["sample_interval"]))

    @property
    def inactivity_timeout(self):
        return float(self.data.get("inactivity_timeout", self.DEFAULT_CONFIG["inactivity_timeout"]))

    @property
    def history_points(self):
        return int(self.data.get("history_points", self.DEFAULT_CONFIG["history_points"]))

    @property
    def speedtest_enabled(self):
        return bool(self.data.get("speedtest_enabled", True))

    @property
    def speedtest_cooldown(self):
        return float(self.data.get("speedtest_cooldown", 60.0))

    @property
    def modules(self):
        return self.data.get("modules", self.DEFAULT_CONFIG["modules"])

    @property
    def trusted_proxies(self):
        return self.data.get("trusted_proxies", self.DEFAULT_CONFIG["trusted_proxies"])

    @property
    def monitored_systemd_units(self):
        return self.data.get("monitored_systemd_units", self.DEFAULT_CONFIG["monitored_systemd_units"])

    @property
    def monitored_processes(self):
        return self.data.get("monitored_processes", self.DEFAULT_CONFIG["monitored_processes"])

    @property
    def monitored_http_urls(self):
        return self.data.get("monitored_http_urls", self.DEFAULT_CONFIG["monitored_http_urls"])

    @property
    def allowed_hosts(self):
        return self.data.get("allowed_hosts", self.DEFAULT_CONFIG.get("allowed_hosts", ["127.0.0.1", "localhost", "::1"]))


# ============================================================
# SECURITY & AUTHENTICATION
# ============================================================

def verify_token(expected_token, provided_token):
    """Constant-time token validation. If expected_token is empty, auth is disabled."""
    if not expected_token:
        return True
    if not provided_token or not isinstance(provided_token, str):
        return False
    return hmac.compare_digest(expected_token.encode("utf-8"), provided_token.encode("utf-8"))


def extract_token(headers, path=""):
    """
    Extracts authentication token strictly from secure headers (Authorization or X-Monitor-Token).
    URL query-string token authentication is intentionally not supported to prevent credential leakage.
    """
    # Check Authorization: Bearer <token>
    auth_header = headers.get("authorization", "") if hasattr(headers, "get") else ""
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    # Check X-Monitor-Token
    custom_header = headers.get("x-monitor-token", "") if hasattr(headers, "get") else ""
    if custom_header:
        return custom_header.strip()

    return ""


def get_security_headers():
    """Returns strict security response headers."""
    return {
        "Content-Security-Policy": "default-src 'self' 'unsafe-inline' data:; connect-src 'self';",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Server": "ServerMonitor/2.0",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }


class RateLimiter:
    """Sliding-window in-memory IP rate limiter with bounded memory and automatic cleanup."""

    def __init__(self, max_requests=60, window_seconds=60.0, max_clients=10000):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self.clients = {}
        self.lock = threading.Lock()

    def allow_request(self, client_ip):
        now = time.monotonic()
        with self.lock:
            # Capacity guard: prune expired entries if map grows large
            if len(self.clients) >= self.max_clients:
                self._prune_unlocked(now)
                # If still over limit, evict oldest inactive entries (never currently throttled entries)
                if len(self.clients) >= self.max_clients:
                    candidates = []
                    for k, ts in self.clients.items():
                        if len(ts) < self.max_requests:
                            candidates.append((ts[-1] if ts else 0, k))
                    candidates.sort()
                    evict_count = max(1, self.max_clients // 5)
                    for _, k in candidates[:evict_count]:
                        del self.clients[k]

            timestamps = self.clients.get(client_ip, [])
            valid_timestamps = [t for t in timestamps if now - t < self.window_seconds]

            if len(valid_timestamps) >= self.max_requests:
                self.clients[client_ip] = valid_timestamps
                return False

            valid_timestamps.append(now)
            self.clients[client_ip] = valid_timestamps
            return True

    def _prune_unlocked(self, now):
        dead_keys = [k for k, ts in self.clients.items() if not ts or (now - ts[-1] >= self.window_seconds)]
        for k in dead_keys:
            del self.clients[k]

    def cleanup(self):
        """Explicitly purges expired IP entries."""
        now = time.monotonic()
        with self.lock:
            self._prune_unlocked(now)


_global_rate_limiter = RateLimiter(max_requests=120, window_seconds=60.0)
_speedtest_rate_limiter = RateLimiter(max_requests=2, window_seconds=60.0)


# ============================================================
# BASIC FILE READER
# ============================================================

def read_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except (OSError, IOError):
        return ""


# ============================================================
# DYNAMIC HARDWARE DETECTION ENGINE
# ============================================================

def get_hostname():
    return read_file("/etc/hostname").strip() or os.uname().nodename


def get_cpu_architecture():
    return os.uname().machine


def get_cpu_model():
    """Dynamically detects CPU model or SoC platform across ARM, x86, and cloud VMs."""
    # 1. Check /proc/cpuinfo for model name (standard x86 and modern ARM)
    cpuinfo = read_file("/proc/cpuinfo")
    for line in cpuinfo.splitlines():
        if line.startswith("model name") and ":" in line:
            model = line.split(":", 1)[1].strip()
            if model:
                return model

    # 2. Check ARM SoC machine identifiers
    for soc_path in ["/sys/devices/soc0/machine", "/proc/device-tree/model"]:
        if os.path.exists(soc_path):
            val = read_file(soc_path).strip("\x00\n\r ")
            if val:
                return val

    # 3. Check DMI product name / hypervisor (e.g., KVM, VMware, AWS, GCP, Oracle)
    dmi_prod = read_file("/sys/class/dmi/id/product_name").strip()
    dmi_vend = read_file("/sys/class/dmi/id/sys_vendor").strip()
    if dmi_prod or dmi_vend:
        parts = [p for p in (dmi_vend, dmi_prod) if p]
        return " ".join(parts)

    # 4. Check ARM Hardware or Processor lines
    for line in cpuinfo.splitlines():
        if (line.startswith("Hardware") or line.startswith("Processor")) and ":" in line:
            val = line.split(":", 1)[1].strip()
            if val:
                return val

    # 5. Check ARM CPU part mapping
    for line in cpuinfo.splitlines():
        if line.startswith("CPU part") and ":" in line:
            part = line.split(":", 1)[1].strip().lower()
            arm_parts = {
                "0xd03": "ARM Cortex-A53",
                "0xd07": "ARM Cortex-A57",
                "0xd08": "ARM Cortex-A72",
                "0xd09": "ARM Cortex-A73",
                "0xd0a": "ARM Cortex-A75",
                "0xd0b": "ARM Cortex-A76",
                "0xd40": "ARM Neoverse V1",
                "0xd49": "ARM Neoverse N2",
            }
            if part in arm_parts:
                return f"{arm_parts[part]} ({os.uname().machine})"

    return f"Generic {os.uname().machine} Processor"


def get_cpu_count():
    return os.cpu_count() or 1


def get_load():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return [0.0, 0.0, 0.0]


def get_uptime():
    try:
        return float(read_file("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        return 0.0


# ============================================================
# DYNAMIC CPU STATS & STEAL TIME
# ============================================================

def get_cpu_usage():
    """Backward-compatible total CPU percentage calculator avoiding guest double-counting."""
    global _previous_cpu

    try:
        values = None
        for line in read_file("/proc/stat").splitlines():
            if line.startswith("cpu "):
                values = list(map(int, line.split()[1:]))
                break

        if not values:
            return 0.0

        # In Linux /proc/stat, guest is already accounted in user, and guest_nice in nice.
        # Sum only the first 8 fields (user, nice, system, idle, iowait, irq, softirq, steal)
        # to avoid double-counting guest/guest_nice.
        total = sum(values[:8]) if len(values) >= 8 else sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        current = (total, idle)

        if _previous_cpu is None:
            _previous_cpu = current
            return 0.0

        previous_total, previous_idle = _previous_cpu
        _previous_cpu = current

        total_delta = total - previous_total
        idle_delta = idle - previous_idle

        if total_delta <= 0:
            return 0.0

        return round((1 - idle_delta / total_delta) * 100, 1)

    except (OSError, ValueError, IndexError):
        return 0.0


def get_container_cpu():
    """Detect cgroup v1 and v2 CPU quota limits."""
    container_cpu = {"active": False, "limit_cores": None}
    if os.path.exists("/sys/fs/cgroup/cpu.max"):
        try:
            raw = read_file("/sys/fs/cgroup/cpu.max").strip().split()
            if len(raw) >= 2 and raw[0] != "max":
                quota = int(raw[0])
                period = int(raw[1])
                if period > 0:
                    container_cpu = {
                        "active": True,
                        "limit_cores": round(quota / period, 2)
                    }
        except Exception:
            pass
    elif os.path.exists("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):
        try:
            quota_raw = read_file("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").strip()
            period_raw = read_file("/sys/fs/cgroup/cpu/cpu.cfs_period_us").strip()
            if quota_raw.isdigit() and period_raw.isdigit():
                quota = int(quota_raw)
                period = int(period_raw)
                if quota > 0 and period > 0:
                    container_cpu = {
                        "active": True,
                        "limit_cores": round(quota / period, 2)
                    }
        except Exception:
            pass
    return container_cpu


def get_cpu_stats():
    """Universal CPU calculation including total usage, steal time, and per-core utilization."""
    global _previous_cpu_detailed

    container_cpu = get_container_cpu()
    stat_lines = read_file("/proc/stat").splitlines()
    cpu_lines = [l for l in stat_lines if l.startswith("cpu")]

    if not cpu_lines:
        return {
            "usage": 0.0,
            "steal": 0.0,
            "cores": get_cpu_count(),
            "per_core": [],
            "model": get_cpu_model(),
            "load": get_load(),
            "container": container_cpu
        }

    parsed = {}
    for line in cpu_lines:
        parts = line.split()
        name = parts[0]
        try:
            vals = list(map(int, parts[1:]))
            parsed[name] = vals
        except ValueError:
            continue

    total_vals = parsed.get("cpu", [])
    if not total_vals:
        return {
            "usage": 0.0,
            "steal": 0.0,
            "cores": get_cpu_count(),
            "per_core": [],
            "model": get_cpu_model(),
            "load": get_load(),
            "container": container_cpu
        }

    current_snapshot = {}
    for name, vals in parsed.items():
        # In Linux /proc/stat, guest is already accounted in user, and guest_nice in nice.
        total = sum(vals[:8]) if len(vals) >= 8 else sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        steal = vals[7] if len(vals) > 7 else 0
        current_snapshot[name] = {
            "total": total,
            "idle": idle,
            "steal": steal
        }

    usage = 0.0
    steal_pct = 0.0
    per_core = []

    if _previous_cpu_detailed is not None:
        prev_total = _previous_cpu_detailed.get("cpu", {})
        curr_total = current_snapshot.get("cpu", {})
        total_delta = curr_total.get("total", 0) - prev_total.get("total", 0)
        idle_delta = curr_total.get("idle", 0) - prev_total.get("idle", 0)
        steal_delta = curr_total.get("steal", 0) - prev_total.get("steal", 0)

        if total_delta > 0:
            usage = round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)
            steal_pct = round(max(0.0, min(100.0, (steal_delta / total_delta) * 100)), 1)

        core_names = sorted([k for k in current_snapshot.keys() if k != "cpu" and k.startswith("cpu")],
                            key=lambda x: int(x[3:]) if x[3:].isdigit() else 0)

        for core in core_names:
            p_core = _previous_cpu_detailed.get(core, {})
            c_core = current_snapshot.get(core, {})
            c_total_delta = c_core.get("total", 0) - p_core.get("total", 0)
            c_idle_delta = c_core.get("idle", 0) - p_core.get("idle", 0)
            if c_total_delta > 0:
                c_usage = round(max(0.0, min(100.0, (1 - c_idle_delta / c_total_delta) * 100)), 1)
            else:
                c_usage = 0.0
            per_core.append(c_usage)

    _previous_cpu_detailed = current_snapshot

    return {
        "usage": usage,
        "steal": steal_pct,
        "cores": len(per_core) or get_cpu_count(),
        "per_core": per_core,
        "model": get_cpu_model(),
        "load": get_load(),
        "container": container_cpu
    }


# ============================================================
# MEMORY & CONTAINER CGROUP DETECTION
# ============================================================

def get_memory():
    """Universal memory inspection with cgroup v1/v2 container awareness."""
    values = {}
    for line in read_file("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                key = parts[0].rstrip(":")
                values[key] = int(parts[1]) * 1024
            except ValueError:
                pass

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    free = values.get("MemFree", 0)
    buffers = values.get("Buffers", 0)
    cached = values.get("Cached", 0)
    used = max(0, total - available)

    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)

    # Detect Container Cgroups (v1 and v2)
    container_info = {"active": False, "limit": None, "used": None, "percent": None}

    # cgroup v2
    if os.path.exists("/sys/fs/cgroup/memory.max"):
        try:
            max_raw = read_file("/sys/fs/cgroup/memory.max").strip()
            cur_raw = read_file("/sys/fs/cgroup/memory.current").strip()
            if max_raw and max_raw != "max":
                limit_val = int(max_raw)
                used_val = int(cur_raw) if cur_raw.isdigit() else 0
                pct = round((used_val / limit_val) * 100, 1) if limit_val > 0 else 0.0
                container_info = {
                    "active": True,
                    "limit": limit_val,
                    "used": used_val,
                    "percent": min(100.0, pct)
                }
        except (ValueError, OSError):
            pass

    # cgroup v1 fallback
    elif os.path.exists("/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            limit_raw = read_file("/sys/fs/cgroup/memory/memory.limit_in_bytes").strip()
            used_raw = read_file("/sys/fs/cgroup/memory/memory.usage_in_bytes").strip()
            if limit_raw.isdigit():
                limit_val = int(limit_raw)
                # Filter out unconstrained limit (e.g. 9223372036854771712)
                if limit_val < 9000000000000000000:
                    used_val = int(used_raw) if used_raw.isdigit() else 0
                    pct = round((used_val / limit_val) * 100, 1) if limit_val > 0 else 0.0
                    container_info = {
                        "active": True,
                        "limit": limit_val,
                        "used": used_val,
                        "percent": min(100.0, pct)
                    }
        except (ValueError, OSError):
            pass

    return {
        "total": total,
        "used": used,
        "available": available,
        "free": free,
        "buffers_cached": buffers + cached,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "container": container_info,
    }


# ============================================================
# DYNAMIC STORAGE MOUNTS DISCOVERY
# ============================================================

def get_storage():
    """Backward-compatible root storage metrics."""
    try:
        stat = os.statvfs("/")
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free
        return {"total": total, "used": used, "free": free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


def get_storage_mounts():
    """Discovers and inspects all real mounted storage filesystems including ZFS, NFS, and CIFS."""
    mounts = []
    seen_mounts = set()

    # Filesystems to exclude (virtual, pseudo, kernel filesystems)
    ignored_fstypes = {
        "proc", "sysfs", "devpts", "devtmpfs", "tmpfs", "securityfs",
        "cgroup", "cgroup2", "pstore", "bpf", "configfs", "autofs",
        "mqueue", "hugetlbfs", "debugfs", "tracefs", "fusectl", "ramfs",
        "binfmt_misc", "nsfs", "efivarfs", "squashfs", "rpc_pipefs", "rootfs",
        "pipefs", "sockfs", "anon_inodefs"
    }

    try:
        content = read_file("/proc/mounts")
        for line in content.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mountpoint, fstype = parts[0], parts[1], parts[2]

            if fstype in ignored_fstypes:
                continue

            # Skip read-only snaps or loop devices not mounted as root
            if mountpoint.startswith("/snap") or (device.startswith("/dev/loop") and mountpoint != "/"):
                continue

            # Skip dummy device markers unless root
            if device in ("none", "nodev") and mountpoint != "/":
                continue

            if mountpoint in seen_mounts:
                continue

            try:
                stat = os.statvfs(mountpoint)
                total = stat.f_blocks * stat.f_frsize
                free = stat.f_bavail * stat.f_frsize
                used = total - free
                if total <= 0:
                    continue

                pct = round((used / total) * 100, 1)
                seen_mounts.add(mountpoint)
                mounts.append({
                    "mount": mountpoint,
                    "device": device,
                    "fstype": fstype,
                    "total": total,
                    "used": used,
                    "free": free,
                    "percent": pct
                })
            except OSError:
                continue

    except Exception:
        pass

    if not mounts:
        # Fallback to root
        root_stat = get_storage()
        total = root_stat["total"]
        used = root_stat["used"]
        pct = round((used / total) * 100, 1) if total > 0 else 0.0
        mounts.append({
            "mount": "/",
            "device": "/dev/root",
            "fstype": "ext4",
            "total": total,
            "used": used,
            "free": root_stat["free"],
            "percent": pct
        })

    # Sort root first, then by mount path
    mounts.sort(key=lambda x: (x["mount"] != "/", x["mount"]))
    return mounts


# ============================================================
# DYNAMIC NETWORK TRAFFIC
# ============================================================

def get_network():
    """Universal network traffic inspection with accurate delta calculation and virtual interface filtering."""
    rx = 0
    tx = 0
    interfaces = {}
    virtual_prefixes = ("lo", "veth", "docker", "br-", "virbr", "vnet", "dummy", "tun", "tap", "flannel", "cni", "calico", "tailscale", "wg")

    raw_ifaces = {}
    try:
        with open("/proc/net/dev", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ":" not in line:
                    continue
                interface, data = line.split(":", 1)
                interface = interface.strip()
                if interface == "lo":
                    continue

                values = data.split()
                if len(values) >= 9:
                    iface_rx = int(values[0])
                    iface_tx = int(values[8])
                    raw_ifaces[interface] = (iface_rx, iface_tx)
    except (OSError, ValueError):
        pass

    # Detect physical hardware devices backed by /sys/class/net/<iface>/device
    has_physical = any(os.path.exists(f"/sys/class/net/{iface}/device") for iface in raw_ifaces)

    for iface, (iface_rx, iface_tx) in raw_ifaces.items():
        is_virt = iface.startswith(virtual_prefixes)
        interfaces[iface] = {
            "rx": iface_rx,
            "tx": iface_tx,
            "is_virtual": is_virt
        }

        # Aggregate traffic without double-counting:
        # If physical devices are detected, aggregate only physical interfaces.
        # Otherwise (containers / VMs), aggregate all non-virtual interfaces.
        is_phys = os.path.exists(f"/sys/class/net/{iface}/device")
        should_aggregate = is_phys if has_physical else (not is_virt)
        if should_aggregate:
            rx += iface_rx
            tx += iface_tx

    global _previous_network
    global _previous_sample_time

    now = time.monotonic()
    rx_rate = 0.0
    tx_rate = 0.0
    iface_rates = {}

    if _previous_network is not None and _previous_sample_time is not None:
        elapsed = now - _previous_sample_time
        if elapsed > 0:
            rx_rate = max(0.0, (rx - _previous_network["rx"]) / elapsed)
            tx_rate = max(0.0, (tx - _previous_network["tx"]) / elapsed)

            prev_ifaces = _previous_network.get("interfaces", {})
            for iface, data in interfaces.items():
                if iface in prev_ifaces:
                    irx = max(0.0, (data["rx"] - prev_ifaces[iface]["rx"]) / elapsed)
                    itx = max(0.0, (data["tx"] - prev_ifaces[iface]["tx"]) / elapsed)
                    iface_rates[iface] = {
                        "rx_rate": round(irx, 1),
                        "tx_rate": round(itx, 1),
                        "rx_total": data["rx"],
                        "tx_total": data["tx"],
                        "is_virtual": data.get("is_virtual", False)
                    }

    _previous_network = {
        "rx": rx,
        "tx": tx,
        "interfaces": interfaces
    }
    _previous_sample_time = now

    # Determine primary interface: prefer physical/non-virtual with highest traffic
    primary = "eth0"
    candidates = iface_rates or interfaces
    if candidates:
        non_virt = [k for k in candidates.keys() if not candidates[k].get("is_virtual", False)]
        search_pool = non_virt if non_virt else list(candidates.keys())
        if iface_rates:
            primary = max(search_pool, key=lambda k: iface_rates[k]["rx_rate"] + iface_rates[k]["tx_rate"])
        else:
            primary = max(search_pool, key=lambda k: interfaces[k]["rx"] + interfaces[k]["tx"])

    return {
        "rx": rx,
        "tx": tx,
        "rx_rate": round(rx_rate, 1),
        "tx_rate": round(tx_rate, 1),
        "primary": primary,
        "interfaces": iface_rates or interfaces,
    }


# ============================================================
# HARDWARE THERMALS & SENSORS
# ============================================================

def get_temperature():
    """Backward-compatible max temperature float or None."""
    details = get_thermal_details()
    return details["temperature"]


def get_thermal_details():
    """Dynamic sensor discovery across /sys/class/thermal and /sys/class/hwmon."""
    sensors = []

    # 1. Thermal Zones
    if os.path.exists("/sys/class/thermal"):
        try:
            for zone in sorted(os.listdir("/sys/class/thermal")):
                if not zone.startswith("thermal_zone"):
                    continue
                type_val = read_file(f"/sys/class/thermal/{zone}/type").strip() or zone
                temp_str = read_file(f"/sys/class/thermal/{zone}/temp").strip()
                if not temp_str:
                    continue
                try:
                    val = float(temp_str)
                    # Normalize scale:
                    # Most drivers use millidegrees (e.g. 45000 -> 45.0 C)
                    # Some mobile kernels use tenths (e.g. 450 -> 45.0 C)
                    if val > 10000:
                        val /= 1000.0
                    elif 100 < val <= 1000:
                        val /= 10.0
                    if -30.0 <= val <= 130.0 and val != 0.0:
                        sensors.append({"name": type_val, "temp": round(val, 1)})
                except ValueError:
                    continue
        except OSError:
            pass

    # 2. Hwmon devices (x86 coretemp, amdgpu, k10temp, nvme)
    if os.path.exists("/sys/class/hwmon"):
        try:
            for h in sorted(os.listdir("/sys/class/hwmon")):
                hpath = f"/sys/class/hwmon/{h}"
                hname = read_file(f"{hpath}/name").strip() or h
                for item in sorted(os.listdir(hpath)):
                    if item.startswith("temp") and item.endswith("_input"):
                        prefix = item.split("_")[0]
                        label = read_file(f"{hpath}/{prefix}_label").strip() or f"{hname} {prefix}"
                        temp_str = read_file(f"{hpath}/{item}").strip()
                        if not temp_str:
                            continue
                        try:
                            val = float(temp_str)
                            if val > 10000:
                                val /= 1000.0
                            elif 100 < val <= 1000:
                                val /= 10.0
                            if -30.0 <= val <= 130.0 and val != 0.0:
                                sensors.append({"name": label, "temp": round(val, 1)})
                        except ValueError:
                            continue
        except OSError:
            pass

    if not sensors:
        return {
            "available": False,
            "temperature": None,
            "sensors": []
        }

    max_temp = max(s["temp"] for s in sensors)
    return {
        "available": True,
        "temperature": max_temp,
        "sensors": sensors[:8]  # Limit to 8 most relevant sensors
    }


# ============================================================
# BATTERY & POWER SUPPLY
# ============================================================

def get_battery():
    """Universal battery detection. Gracefully returns available: False for servers/VMs."""
    base_dir = "/sys/class/power_supply"
    if not os.path.exists(base_dir):
        return {
            "available": False,
            "capacity": None,
            "status": "No Battery",
            "health": "N/A",
            "voltage": None,
            "current": None,
            "temperature": None,
            "technology": "N/A"
        }

    battery_candidates = []
    try:
        for entry in os.listdir(base_dir):
            epath = f"{base_dir}/{entry}"
            type_val = read_file(f"{epath}/type").strip().lower()
            cap_val = read_file(f"{epath}/capacity").strip()
            if type_val == "battery" or (not type_val and cap_val.isdigit()):
                battery_candidates.append(epath)
    except OSError:
        pass

    if not battery_candidates:
        return {
            "available": False,
            "capacity": None,
            "status": "No Battery",
            "health": "N/A",
            "voltage": None,
            "current": None,
            "temperature": None,
            "technology": "N/A"
        }

    # Prefer 'battery' or 'BAT0'
    base = battery_candidates[0]
    for c in battery_candidates:
        if c.endswith("/battery") or c.endswith("/BAT0"):
            base = c
            break

    def read_b(name):
        return read_file(f"{base}/{name}").strip()

    capacity = read_b("capacity")
    status = read_b("status")
    health = read_b("health")
    voltage = read_b("voltage_now")
    current = read_b("current_now")
    temperature = read_b("temp")
    technology = read_b("technology")

    capacity_val = int(capacity) if capacity.isdigit() else None

    voltage_val = None
    if voltage and voltage.lstrip("-").isdigit():
        v = int(voltage)
        voltage_val = round(v / 1000000.0 if abs(v) > 10000 else v / 1000.0, 3)

    current_val = int(current) if current and current.lstrip("-").isdigit() else None

    temp_val = None
    if temperature and temperature.lstrip("-").isdigit():
        t = int(temperature)
        temp_val = round(t / 10.0 if t > 100 else float(t), 1)

    return {
        "available": True,
        "capacity": capacity_val,
        "status": status or "Unknown",
        "health": health or "Good",
        "voltage": voltage_val,
        "current": current_val,
        "temperature": temp_val,
        "technology": technology or "Li-ion",
    }


# ============================================================
# SERVICES & PROCESS MONITORING
# ============================================================

def get_process_status(process_name):
    """Checks whether a given process name is running via /proc."""
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            name = read_file(f"/proc/{pid}/comm").strip()
            if name == process_name:
                return True
    except OSError:
        pass
    return False


def get_monitored_processes(process_names=None):
    """Inspects configured process list directly via /proc in pure Python."""
    global _global_config
    if process_names is None:
        process_names = _global_config.monitored_processes if _global_config else ["sshd"]

    results = {p: {"running": False, "count": 0, "pids": []} for p in process_names}
    if not os.path.exists("/proc"):
        return results

    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            comm = read_file(f"/proc/{pid}/comm").strip()
            if comm in results:
                results[comm]["running"] = True
                results[comm]["count"] += 1
                if len(results[comm]["pids"]) < 5:
                    results[comm]["pids"].append(pid)
    except OSError:
        pass

    return results


def get_systemd_service_status(service_name):
    """Inspects a single systemd service state in a strictly read-only manner."""
    if not os.path.exists("/run/systemd/system") or not shutil.which("systemctl"):
        return "unavailable"
    clean_name = "".join(c for c in service_name if c.isalnum() or c in "@-_.")
    if not clean_name:
        return "unknown"
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", clean_name],
            capture_output=True,
            text=True,
            timeout=2,
            shell=False
        )
        out = proc.stdout.strip()
        if proc.returncode == 0 and out == "active":
            return "active"
        return out or "inactive"
    except Exception:
        return "unknown"


def get_systemd_services(units=None):
    """Inspects configured systemd units in a strictly read-only manner."""
    global _global_config
    if units is None:
        units = _global_config.monitored_systemd_units if _global_config else ["ssh", "sshd", "nginx", "caddy", "docker"]

    if not os.path.exists("/run/systemd/system") or not shutil.which("systemctl"):
        return {"available": False, "units": {}}

    results = {}
    for unit in units:
        clean_unit = "".join(c for c in unit if c.isalnum() or c in "@-_.")
        if not clean_unit:
            continue
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", clean_unit],
                capture_output=True,
                text=True,
                timeout=2,
                shell=False
            )
            state = proc.stdout.strip() or ("active" if proc.returncode == 0 else "inactive")
            results[clean_unit] = state
        except Exception:
            results[clean_unit] = "unknown"

    return {"available": True, "units": results}


class StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Restricts redirects to http/https only and caps maximum redirect depth to 3."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme.lower() not in ("http", "https"):
            raise urllib.error.HTTPError(req.full_url, code, f"Forbidden redirect scheme: {parsed.scheme}", headers, fp)
        count = getattr(req, "_redirect_count", 0) + 1
        if count > 3:
            raise urllib.error.HTTPError(req.full_url, code, "Maximum redirect limit (3) exceeded", headers, fp)
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            new_req._redirect_count = count
        return new_req


def get_health_checks(endpoints=None):
    """Performs lightweight HTTP and TCP endpoint health checks."""
    global _global_config
    if endpoints is None:
        endpoints = _global_config.monitored_http_urls if _global_config else []

    results = []
    for ep in endpoints:
        if isinstance(ep, str):
            ep = {"name": ep, "url": ep}
        name = ep.get("name", "Endpoint")
        url = ep.get("url")
        check_type = ep.get("type", "http")
        timeout = float(ep.get("timeout", 2.0))

        if check_type == "tcp" or (not url and "port" in ep):
            host = ep.get("host", "127.0.0.1")
            port = int(ep.get("port", 80))
            t_start = time.monotonic()
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    latency = round((time.monotonic() - t_start) * 1000.0, 1)
                    results.append({
                        "name": name,
                        "status": "up",
                        "latency_ms": latency,
                        "type": "tcp",
                        "target": f"{host}:{port}"
                    })
            except Exception as e:
                results.append({
                    "name": name,
                    "status": "down",
                    "error": str(e),
                    "type": "tcp",
                    "target": f"{host}:{port}"
                })
        elif url:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme.lower() not in ("http", "https"):
                results.append({
                    "name": name,
                    "status": "down",
                    "error": f"Invalid URL scheme '{parsed.scheme}'; only http and https are permitted",
                    "type": "http",
                    "target": url
                })
                continue

            t_start = time.monotonic()
            try:
                opener = urllib.request.build_opener(StrictRedirectHandler())
                req = urllib.request.Request(url, headers={"User-Agent": "ServerMonitor/2.0-HealthCheck"})
                with opener.open(req, timeout=timeout) as resp:
                    latency = round((time.monotonic() - t_start) * 1000.0, 1)
                    code = resp.getcode()
                    results.append({
                        "name": name,
                        "status": "up" if code < 400 else "warn",
                        "code": code,
                        "latency_ms": latency,
                        "type": "http",
                        "target": url
                    })
            except Exception as e:
                results.append({
                    "name": name,
                    "status": "down",
                    "error": str(e),
                    "type": "http",
                    "target": url
                })

    return results


def get_docker_containers():
    """Inspects Docker and Podman containers in a strictly read-only manner with fallback."""
    runtimes = []
    d_bin = shutil.which("docker")
    p_bin = shutil.which("podman")
    if d_bin:
        runtimes.append(d_bin)
    if p_bin:
        runtimes.append(p_bin)

    if not runtimes:
        return {
            "available": False,
            "runtime": None,
            "error": "Docker/Podman binary not found",
            "containers": []
        }

    last_error = ""
    for runtime in runtimes:
        bin_name = os.path.basename(runtime)
        try:
            proc = subprocess.run(
                [runtime, "ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.State}}"],
                capture_output=True,
                text=True,
                timeout=3,
                shell=False
            )
            if proc.returncode != 0:
                last_error = proc.stderr.strip() or f"Exited with code {proc.returncode}"
                continue

            containers = []
            running = 0
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                cid = parts[0] if len(parts) > 0 else ""
                cname = parts[1] if len(parts) > 1 else ""
                cimg = parts[2] if len(parts) > 2 else ""
                cstatus = parts[3] if len(parts) > 3 else ""
                cstate = parts[4] if len(parts) > 4 else ("running" if "Up" in cstatus else "exited")
                if cstate.lower() == "running" or "Up" in cstatus:
                    running += 1
                containers.append({
                    "id": cid[:12],
                    "name": cname,
                    "image": cimg,
                    "status": cstatus,
                    "state": cstate.lower()
                })

            return {
                "available": True,
                "runtime": bin_name,
                "total": len(containers),
                "running": running,
                "containers": containers
            }
        except Exception as e:
            last_error = str(e)
            continue

    return {
        "available": False,
        "runtime": os.path.basename(runtimes[0]),
        "error": last_error or "Container runtime unavailable",
        "containers": []
    }


def get_pm2_services():
    """PR #1 compatible PM2 service discovery with thread-safe TTL cache."""
    global _pm2_cache_data, _pm2_cache_time

    now = time.monotonic()
    if _pm2_cache_data is not None and (now - _pm2_cache_time) < PM2_CACHE_TTL:
        return _pm2_cache_data

    with _pm2_cache_lock:
        now = time.monotonic()
        if _pm2_cache_data is not None and (now - _pm2_cache_time) < PM2_CACHE_TTL:
            return _pm2_cache_data

        try:
            proc = subprocess.run(
                ["pm2", "jlist"],
                capture_output=True,
                text=True,
                timeout=5
            )

            if proc.returncode != 0:
                result = {
                    "status": "error",
                    "error": proc.stderr.strip() or f"PM2 exited with code {proc.returncode}",
                    "services": []
                }
            else:
                data = json.loads(proc.stdout)
                if not isinstance(data, list):
                    result = {
                        "status": "error",
                        "error": "Invalid PM2 output format",
                        "services": []
                    }
                else:
                    services = []
                    for item in data:
                        pm2_env = item.get("pm2_env") or {}
                        monit = item.get("monit") or {}
                        services.append({
                            "name": item.get("name", "unknown"),
                            "status": pm2_env.get("status", "unknown"),
                            "pm_id": item.get("pm_id"),
                            "cpu": monit.get("cpu", 0),
                            "memory": monit.get("memory", 0),
                        })

                    result = {
                        "status": "ok",
                        "services": services
                    }

        except FileNotFoundError:
            result = {
                "status": "unavailable",
                "error": "PM2 not found",
                "services": []
            }
        except subprocess.TimeoutExpired:
            result = {
                "status": "error",
                "error": "PM2 command timed out",
                "services": []
            }
        except json.JSONDecodeError:
            result = {
                "status": "error",
                "error": "Failed to parse PM2 output",
                "services": []
            }
        except Exception as e:
            result = {
                "status": "error",
                "error": str(e),
                "services": []
            }

        _pm2_cache_data = result
        _pm2_cache_time = time.monotonic()
        return result


# ============================================================
# SPEED TEST WITH MUTEX & COOLDOWN
# ============================================================

def run_speed_test():
    """On-demand speed test protected by mutex and mandatory cooldown."""
    global _speedtest_last_run, _global_config

    cfg = _global_config or Config()
    if not cfg.speedtest_enabled:
        return {
            "success": False,
            "download": None,
            "upload": None,
            "latency": None,
            "error": "Speed test is disabled in server configuration",
        }

    result = {
        "success": False,
        "download": None,
        "upload": None,
        "latency": None,
        "error": None,
    }

    if not _speedtest_lock.acquire(blocking=False):
        result["error"] = "Speed test is already running. Please wait."
        return result

    try:
        now = time.monotonic()
        cooldown = cfg.speedtest_cooldown
        if _speedtest_last_run > 0.0:
            elapsed_since_last = now - _speedtest_last_run
            if elapsed_since_last < cooldown:
                remaining = int(cooldown - elapsed_since_last)
                result["error"] = f"Cooldown active. Please wait {remaining} seconds before testing again."
                return result

        # 1. Latency Check
        latency_start = time.monotonic()
        with socket.create_connection(("speed.cloudflare.com", 443), timeout=5):
            pass
        latency = (time.monotonic() - latency_start) * 1000.0
        result["latency"] = round(latency, 1)

        # 2. Download Test (5 MB)
        download_url = "https://speed.cloudflare.com/__down?bytes=5000000"
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": "ServerMonitor/2.0"}
        )
        dl_start = time.monotonic()
        dl_bytes = 0
        with urllib.request.urlopen(req, timeout=12) as response:
            while True:
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                dl_bytes += len(chunk)
        dl_elapsed = time.monotonic() - dl_start
        if dl_elapsed > 0:
            result["download"] = round((dl_bytes * 8.0) / dl_elapsed / 1_000_000.0, 2)

        # 3. Upload Test (2 MB)
        ul_size = 2 * 1024 * 1024
        ul_data = b"0" * ul_size
        ul_req = urllib.request.Request(
            "https://speed.cloudflare.com/__up",
            data=ul_data,
            method="POST",
            headers={
                "User-Agent": "ServerMonitor/2.0",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(ul_size)
            }
        )
        ul_start = time.monotonic()
        with urllib.request.urlopen(ul_req, timeout=12) as resp:
            resp.read()
        ul_elapsed = time.monotonic() - ul_start
        if ul_elapsed > 0:
            result["upload"] = round((ul_size * 8.0) / ul_elapsed / 1_000_000.0, 2)

        result["success"] = True
        _speedtest_last_run = time.monotonic()

    except Exception as e:
        result["error"] = str(e)
    finally:
        _speedtest_lock.release()

    return result


# ============================================================
# ANTIGRAVITY LLM QUOTA (PR #1 COMPATIBLE)
# ============================================================

def get_agy_executable():
    path = shutil.which("agy")
    if path:
        return path
    user_home = os.environ.get("HOME") or os.path.expanduser("~")
    for candidate in [
        os.path.join(user_home, ".local", "bin", "agy"),
        "/usr/local/bin/agy",
        "/usr/bin/agy",
    ]:
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "agy"


def run_llm_usage():
    global _global_config
    cfg = _global_config or Config()
    if not cfg.modules.get("antigravity", True):
        return {
            "success": False,
            "groups": [],
            "error": "Antigravity module is disabled in configuration"
        }

    result = {
        "success": False,
        "groups": [],
        "error": None
    }

    if not _llm_lock.acquire(blocking=False):
        result["error"] = "LLM usage check already in progress"
        return result

    try:
        agy_bin = get_agy_executable()
        env = os.environ.copy()
        user_home = os.environ.get("HOME") or os.path.expanduser("~")
        local_bin = os.path.join(user_home, ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        if "HOME" not in env:
            env["HOME"] = user_home

        proc = subprocess.run(
            [agy_bin, "--output-format", "json", "-p", "/usage"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env
        )

        if proc.returncode != 0:
            result["error"] = proc.stderr.strip() or f"agy exited with code {proc.returncode}"
            return result

        formatted_groups = []
        try:
            raw = json.loads(proc.stdout)
            cmd_data = raw.get("command", {}).get("data", {})
            groups = cmd_data.get("groups", [])
            for g in groups:
                g_name = g.get("name", "Unknown Group")
                buckets = []
                for b in g.get("buckets", []):
                    b_name = b.get("name", b.get("id", "Limit"))
                    rem_frac = b.get("remaining_fraction", 0.0)
                    pct = round(rem_frac * 100, 1)
                    reset_time = b.get("reset_time", "")
                    buckets.append({
                        "name": b_name,
                        "percentage": pct,
                        "reset_time": reset_time
                    })
                formatted_groups.append({
                    "name": g_name,
                    "buckets": buckets
                })
        except (json.JSONDecodeError, AttributeError):
            # Fallback parser for plain text tab-separated output from agy
            lines = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
            groups_dict = collections.OrderedDict()
            for line in lines:
                parts = line.split("\t")
                if len(parts) >= 3:
                    g_name = parts[0].strip()
                    b_name = parts[1].strip()
                    pct_str = parts[2].strip().rstrip("%")
                    try:
                        pct = float(pct_str)
                    except ValueError:
                        pct = 0.0
                    reset_time = parts[3].strip() if len(parts) >= 4 else ""
                    if g_name not in groups_dict:
                        groups_dict[g_name] = []
                    groups_dict[g_name].append({
                        "name": b_name,
                        "percentage": pct,
                        "reset_time": reset_time
                    })
            for g_name, buckets in groups_dict.items():
                formatted_groups.append({
                    "name": g_name,
                    "buckets": buckets
                })

        if not formatted_groups:
            result["error"] = "Failed to parse Antigravity output"
            return result

        result["groups"] = formatted_groups
        result["success"] = True

    except FileNotFoundError:
        result["error"] = "Antigravity CLI (agy) not found"
    except subprocess.TimeoutExpired:
        result["error"] = "Antigravity CLI timed out"
    except json.JSONDecodeError:
        result["error"] = "Failed to parse Antigravity output"
    except Exception as e:
        result["error"] = str(e)
    finally:
        _llm_lock.release()

    return result


# ============================================================
# COMPLETE SYSTEM STATUS
# ============================================================

def collect_system_metrics(cfg=None):
    """Samples all enabled system telemetry modules according to configuration switches."""
    global _global_config
    cfg = cfg or _global_config or Config()
    mods = cfg.modules

    # CPU module
    if mods.get("cpu", True):
        cpu_info = get_cpu_stats()
    else:
        cpu_info = {
            "usage": 0.0,
            "steal": 0.0,
            "cores": get_cpu_count(),
            "per_core": [],
            "model": "Disabled",
            "load": [0.0, 0.0, 0.0],
            "disabled": True
        }

    # Memory module
    if mods.get("memory", True):
        memory = get_memory()
    else:
        memory = {
            "total": 0,
            "used": 0,
            "available": 0,
            "free": 0,
            "buffers_cached": 0,
            "swap_total": 0,
            "swap_used": 0,
            "container": {"active": False},
            "disabled": True
        }

    # Storage module
    if mods.get("storage", True):
        storage = get_storage()
        disks = get_storage_mounts()
    else:
        storage = {"total": 0, "used": 0, "free": 0, "disabled": True}
        disks = []

    # Network module
    if mods.get("network", True):
        network = get_network()
    else:
        network = {
            "rx": 0,
            "tx": 0,
            "rx_rate": 0.0,
            "tx_rate": 0.0,
            "primary": "disabled",
            "interfaces": {},
            "disabled": True
        }

    # Thermals module
    if mods.get("thermals", True):
        thermal = get_thermal_details()
    else:
        thermal = {
            "available": False,
            "temperature": None,
            "sensors": [],
            "disabled": True
        }

    # Battery module
    if mods.get("battery", True):
        battery = get_battery()
    else:
        battery = {
            "available": False,
            "capacity": None,
            "status": "Disabled",
            "health": "N/A",
            "voltage": None,
            "current": None,
            "temperature": None,
            "technology": "N/A",
            "disabled": True
        }

    # Services module
    services = {}
    if mods.get("services", True):
        services["sshd"] = get_process_status("sshd")
        services["pm2"] = get_pm2_services() if mods.get("pm2", True) else {"status": "disabled", "services": []}
        services["systemd"] = get_systemd_services() if mods.get("systemd", True) else {"available": False, "units": {}, "disabled": True}
        services["processes"] = get_monitored_processes()
        services["health_checks"] = get_health_checks()
        services["docker"] = get_docker_containers() if mods.get("docker", False) else {"available": False, "runtime": None, "disabled": True, "containers": []}
    else:
        services = {
            "sshd": False,
            "pm2": {"status": "disabled", "services": []},
            "systemd": {"available": False, "units": {}, "disabled": True},
            "processes": {},
            "health_checks": [],
            "docker": {"available": False, "runtime": None, "disabled": True, "containers": []}
        }

    mem_pct = 0.0
    if memory.get("total", 0) > 0:
        mem_pct = round((memory.get("used", 0) / memory["total"]) * 100, 1)

    return {
        "hostname": get_hostname(),
        "architecture": get_cpu_architecture(),
        "cpu": cpu_info["usage"],
        "cpu_count": cpu_info["cores"],
        "cpu_details": cpu_info,
        "load": cpu_info["load"],
        "memory": memory,
        "mem_pct": mem_pct,
        "storage": storage,
        "disks": disks,
        "uptime": get_uptime(),
        "network": network,
        "temperature": thermal["temperature"],
        "thermal": thermal,
        "battery": battery,
        "services": services,
        "timestamp": time.time(),
    }


def get_status(client_id=None):
    """Returns complete telemetry snapshot, waking collector if needed."""
    global _global_collector

    if _global_collector is not None:
        if client_id:
            _global_collector.touch_session(client_id)
        return _global_collector.get_snapshot()

    snapshot = collect_system_metrics()
    snapshot["collector_state"] = "standalone"
    snapshot.pop("mem_pct", None)
    return snapshot


# ============================================================
# ON-DEMAND LIVE METRICS COLLECTOR
# ============================================================

class OnDemandCollector:
    """
    On-demand background collector:
    Samples system metrics at sample_interval (default 2s) ONLY when an
    authenticated client dashboard is actively connected. Automatically suspends
    when no client requests or heartbeats are received within inactivity_timeout.
    """

    def __init__(self, sample_interval=2.0, inactivity_timeout=15.0, history_maxlen=60):
        self.sample_interval = float(sample_interval)
        self.inactivity_timeout = float(inactivity_timeout)
        self.history_maxlen = int(history_maxlen)

        self._active_sessions = {}
        self._lock = threading.RLock()
        self._sampling_lock = threading.Lock()
        self._wakeup_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread = None

        self._cached_snapshot = None
        self._last_sample_time = 0.0
        self._history = {
            "cpu": collections.deque(maxlen=self.history_maxlen),
            "memory": collections.deque(maxlen=self.history_maxlen),
            "steal": collections.deque(maxlen=self.history_maxlen),
            "rx_rate": collections.deque(maxlen=self.history_maxlen),
            "tx_rate": collections.deque(maxlen=self.history_maxlen),
        }
        self._is_active = False

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run_loop, name="OnDemandCollector", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._wakeup_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

    def touch_session(self, session_id):
        if not session_id:
            session_id = "default"
        now = time.monotonic()
        with self._lock:
            was_active = self._is_active
            self._active_sessions[session_id] = now
            if not was_active:
                self._is_active = True
                # Reset sampling baselines on wake so idle gap does not distort CPU/network rates
                global _previous_cpu_detailed, _previous_network, _previous_sample_time
                _previous_cpu_detailed = None
                _previous_network = None
                _previous_sample_time = None
                self._wakeup_event.set()

    def unregister_session(self, session_id):
        with self._lock:
            if session_id in self._active_sessions:
                del self._active_sessions[session_id]

    def is_active(self):
        now = time.monotonic()
        with self._lock:
            valid = {s: t for s, t in self._active_sessions.items() if now - t < self.inactivity_timeout}
            self._active_sessions = valid
            return len(valid) > 0

    def get_state(self):
        return "active" if self.is_active() else "idle"

    def get_history(self):
        with self._lock:
            return {
                "cpu": list(self._history["cpu"]),
                "memory": list(self._history["memory"]),
                "steal": list(self._history["steal"]),
                "rx_rate": list(self._history["rx_rate"]),
                "tx_rate": list(self._history["tx_rate"]),
            }

    def _sample(self):
        """Samples metrics outside self._lock, then acquires lock briefly to store result."""
        snapshot = collect_system_metrics()
        mem_pct = snapshot.pop("mem_pct", 0.0)
        now = time.monotonic()

        with self._lock:
            snapshot["collector_state"] = self.get_state()
            self._cached_snapshot = snapshot
            self._last_sample_time = now
            self._history["cpu"].append(snapshot["cpu"])
            self._history["memory"].append(mem_pct)
            self._history["steal"].append(snapshot["cpu_details"]["steal"])
            self._history["rx_rate"].append(snapshot["network"]["rx_rate"])
            self._history["tx_rate"].append(snapshot["network"]["tx_rate"])

    def get_snapshot(self):
        now = time.monotonic()
        need_sample = False
        with self._lock:
            if self._cached_snapshot is None or (self.is_active() and (now - self._last_sample_time > self.sample_interval)):
                need_sample = True

        if need_sample:
            with self._sampling_lock:
                recheck = False
                with self._lock:
                    if self._cached_snapshot is None or (self.is_active() and (time.monotonic() - self._last_sample_time > self.sample_interval)):
                        recheck = True
                if recheck:
                    self._sample()

        with self._lock:
            res = dict(self._cached_snapshot) if self._cached_snapshot else {}
            res["collector_state"] = self.get_state()
            res["history"] = {
                "cpu": list(self._history["cpu"]),
                "memory": list(self._history["memory"]),
                "steal": list(self._history["steal"]),
                "rx_rate": list(self._history["rx_rate"]),
                "tx_rate": list(self._history["tx_rate"]),
            }
            return res

    def _run_loop(self):
        while not self._stop_event.is_set():
            if not self.is_active():
                with self._lock:
                    self._is_active = False
                self._wakeup_event.clear()
                # Sleep until woken up by active client session
                self._wakeup_event.wait(timeout=None)
                if self._stop_event.is_set():
                    break
                # Woken up: if get_snapshot() already sampled recently on wakeup, wait for sample_interval before next sample
                if time.monotonic() - self._last_sample_time < (self.sample_interval * 0.5):
                    self._stop_event.wait(self.sample_interval)
                    continue

            # Active sampling
            try:
                with self._sampling_lock:
                    self._sample()
            except Exception as e:
                sys.stderr.write(f"Collector sampling error: {e}\n")

            self._stop_event.wait(self.sample_interval)


# ============================================================
# MODERN PRODUCTION HTML DASHBOARD
# ============================================================

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ServerMonitor &bull; Universal Linux Monitor</title>
<style>
* { box-sizing: border-box; }
:root {
    --bg: #0b0f14;
    --card: #111820;
    --card-border: #202938;
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #3fb950;
    --blue: #58a6ff;
    --orange: #f0883e;
    --red: #f85149;
    --purple: #bc8cff;
}
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
}
.container {
    width: min(1180px, 94%);
    margin: 24px auto;
}
.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--card-border);
}
.title-group {
    display: flex;
    flex-direction: column;
}
.title-row {
    display: flex;
    align-items: center;
    gap: 12px;
}
.title {
    font-size: 24px;
    font-weight: 700;
    letter-spacing: -0.5px;
}
.badge {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    padding: 3px 8px;
    border-radius: 20px;
    background: rgba(88, 166, 255, 0.15);
    color: var(--blue);
    border: 1px solid rgba(88, 166, 255, 0.3);
}
.subtitle {
    color: var(--muted);
    font-size: 13px;
    margin-top: 4px;
}
.header-actions {
    display: flex;
    align-items: center;
    gap: 12px;
}
.status-pill {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 20px;
    background: rgba(63, 185, 80, 0.1);
    color: var(--green);
    border: 1px solid rgba(63, 185, 80, 0.25);
}
.status-pill.idle {
    background: rgba(240, 136, 62, 0.1);
    color: var(--orange);
    border-color: rgba(240, 136, 62, 0.25);
}
.status-pill.offline {
    background: rgba(248, 81, 73, 0.1);
    color: var(--red);
    border-color: rgba(248, 81, 73, 0.25);
}
.dot {
    width: 8px;
    height: 8px;
    background: currentColor;
    border-radius: 50%;
    box-shadow: 0 0 8px currentColor;
}
.btn {
    background: #1b2430;
    color: var(--text);
    border: 1px solid #303d4f;
    padding: 8px 16px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
}
.btn:hover:not(:disabled) {
    background: #253344;
    border-color: var(--blue);
}
.btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
}
.grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 20px;
}
@media (max-width: 900px) {
    .grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 600px) {
    .grid { grid-template-columns: 1fr; }
}
.card {
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 12px;
    padding: 18px;
    display: flex;
    flex-direction: column;
    position: relative;
    overflow: hidden;
}
.card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
}
.card-title {
    color: var(--muted);
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.card-subtext {
    font-size: 11px;
    color: var(--muted);
}
.big-stat {
    font-size: 32px;
    font-weight: 700;
    letter-spacing: -1px;
    margin: 4px 0 8px;
    display: flex;
    align-items: baseline;
    gap: 4px;
}
.unit {
    font-size: 16px;
    font-weight: 500;
    color: var(--muted);
}
.progress-bar-bg {
    width: 100%;
    height: 6px;
    background: #1e2836;
    border-radius: 3px;
    overflow: hidden;
    margin: 8px 0;
}
.progress-bar-fill {
    height: 100%;
    background: var(--green);
    border-radius: 3px;
    transition: width 0.4s ease;
}
.progress-bar-fill.warn { background: var(--orange); }
.progress-bar-fill.danger { background: var(--red); }
.stat-rows {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 13px;
    margin-top: 8px;
}
.stat-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: var(--muted);
}
.stat-val {
    color: var(--text);
    font-weight: 500;
}
.sparkline-svg {
    width: 100%;
    height: 36px;
    margin-top: 10px;
    overflow: visible;
}
.sparkline-path {
    fill: none;
    stroke: var(--blue);
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
}
.sparkline-area {
    fill: rgba(88, 166, 255, 0.1);
}
.cores-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(28px, 1fr));
    gap: 4px;
    margin-top: 10px;
}
.core-pill {
    background: #19222c;
    border-radius: 4px;
    height: 24px;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    overflow: hidden;
    position: relative;
}
.core-fill {
    background: var(--blue);
    width: 100%;
    transition: height 0.3s ease;
}
.core-num {
    position: absolute;
    top: 2px;
    left: 0;
    right: 0;
    text-align: center;
    font-size: 9px;
    font-weight: 600;
    color: var(--muted);
}
/* Services Card styles required by tests */
.services {
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow-y: auto;
    max-height: 260px;
    scrollbar-width: none;
    -ms-overflow-style: none;
}
.services::-webkit-scrollbar {
    display: none;
}
.service-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    background: #161f2b;
    border: 1px solid #253344;
    border-radius: 8px;
    font-size: 13px;
}
.service-name {
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 8px;
}
.service-badge {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 12px;
}
.service-badge.online {
    background: rgba(63, 185, 80, 0.15);
    color: var(--green);
}
.service-badge.offline {
    background: rgba(248, 81, 73, 0.15);
    color: var(--red);
}
.service-meta {
    font-size: 11px;
    color: var(--muted);
}
/* LLM Card styles required by tests */
.llm-results {
    margin-top: 12px;
}
.llm-group {
    margin-bottom: 12px;
}
.llm-group-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 6px;
}
.llm-bucket {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 6px 10px;
    background: #161f2b;
    border-radius: 6px;
    margin-bottom: 4px;
    font-size: 12px;
}
.llm-bucket-header {
    display: flex;
    justify-content: space-between;
}
.llm-bucket-val.high { color: var(--green); font-weight: 600; }
.llm-bucket-val.medium { color: var(--orange); font-weight: 600; }
.llm-bucket-val.low { color: var(--red); font-weight: 600; }
.llm-bucket-reset { font-size: 10px; color: var(--muted); }
/* Auth Modal */
.modal-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(5, 8, 12, 0.85);
    backdrop-filter: blur(4px);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 1000;
}
.modal-card {
    background: #151d28;
    border: 1px solid var(--card-border);
    border-radius: 12px;
    padding: 28px;
    width: min(400px, 90%);
    box-shadow: 0 16px 32px rgba(0,0,0,0.5);
}
.modal-title { font-size: 18px; font-weight: 700; margin-bottom: 8px; }
.modal-text { font-size: 13px; color: var(--muted); margin-bottom: 16px; }
.modal-input {
    width: 100%;
    padding: 10px 14px;
    background: #0d131a;
    border: 1px solid #2b394c;
    border-radius: 8px;
    color: var(--text);
    font-size: 14px;
    margin-bottom: 16px;
    outline: none;
}
.modal-input:focus { border-color: var(--blue); }
</style>
</head>
<body>

<div class="container">
    <div class="header">
        <div class="title-group">
            <div class="title-row">
                <span class="title" id="serverHostname">ServerMonitor</span>
                <span class="badge" id="serverArch">Linux</span>
                <span class="badge" id="containerBadge" style="display:none;">Container</span>
            </div>
            <div class="subtitle" id="cpuModel">Universal Linux Telemetry Engine</div>
        </div>
        <div class="header-actions">
            <div class="status-pill" id="connectionPill">
                <span class="dot"></span>
                <span id="connectionText">Live (2s)</span>
            </div>
            <button class="btn" id="speedtestBtn" onclick="runSpeedTest()">Speed Test</button>
            <button class="btn" id="llmusage-button" onclick="runLlmUsage()">Check LLM Quota</button>
        </div>
    </div>

    <!-- MAIN METRICS GRID -->
    <div class="grid">
        <!-- CPU Card -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">Processor</span>
                <span class="card-subtext" id="cpuCoresCount">-- Cores</span>
            </div>
            <div class="big-stat">
                <span id="cpuPercent">--</span>
                <span class="unit">%</span>
            </div>
            <div class="progress-bar-bg">
                <div class="progress-bar-fill" id="cpuBar" style="width: 0%;"></div>
            </div>
            <div class="stat-rows">
                <div class="stat-row">
                    <span>Load Average (1/5/15m)</span>
                    <span class="stat-val" id="loadAvg">-- / -- / --</span>
                </div>
                <div class="stat-row" id="cpuStealRow">
                    <span>CPU Steal Time</span>
                    <span class="stat-val" id="cpuSteal">0.0%</span>
                </div>
                <div class="stat-row">
                    <span>System Uptime</span>
                    <span class="stat-val" id="uptime">--</span>
                </div>
            </div>
            <div class="cores-grid" id="cpuCoresList"></div>
            <svg class="sparkline-svg" id="cpuSparkline"></svg>
        </div>

        <!-- Memory Card -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">Memory</span>
                <span class="card-subtext" id="memoryAvailable">-- Available</span>
            </div>
            <div class="big-stat">
                <span id="memoryPercent">--</span>
                <span class="unit">%</span>
            </div>
            <div class="progress-bar-bg">
                <div class="progress-bar-fill" id="memoryBar" style="width: 0%;"></div>
            </div>
            <div class="stat-rows">
                <div class="stat-row">
                    <span>Used / Total</span>
                    <span class="stat-val" id="memoryUsage">-- / -- GB</span>
                </div>
                <div class="stat-row">
                    <span>Buffers / Cache</span>
                    <span class="stat-val" id="memoryCache">-- GB</span>
                </div>
                <div class="stat-row">
                    <span>Swap Usage</span>
                    <span class="stat-val" id="swapUsage">-- / -- GB</span>
                </div>
                <div class="stat-row" id="containerMemRow" style="display: none;">
                    <span>Container Limit</span>
                    <span class="stat-val" id="containerMemVal">--</span>
                </div>
            </div>
            <svg class="sparkline-svg" id="memorySparkline"></svg>
        </div>

        <!-- Storage Card -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">Storage</span>
                <span class="card-subtext" id="storageSummary">-- Real Mounts</span>
            </div>
            <div id="disksContainer" style="display: flex; flex-direction: column; gap: 10px;">
                <!-- Dynamically populated filesystems -->
                <div>
                    <div style="display: flex; justify-content: space-between; font-size: 13px; font-weight: 500;">
                        <span>Root (/)</span>
                        <span id="storagePercent">--%</span>
                    </div>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill" id="storageBar" style="width: 0%;"></div>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 11px; color: var(--muted);">
                        <span id="storageUsed">-- GB used</span>
                        <span id="storageFree">-- GB free</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Network Card -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">Network Activity</span>
                <span class="card-subtext" id="primaryInterface">--</span>
            </div>
            <div style="display: flex; justify-content: space-around; margin: 12px 0;">
                <div>
                    <div style="font-size: 11px; color: var(--muted); text-transform: uppercase;">Download Rate</div>
                    <div style="font-size: 20px; font-weight: 700; color: var(--blue);" id="rxRate">-- KB/s</div>
                </div>
                <div>
                    <div style="font-size: 11px; color: var(--muted); text-transform: uppercase;">Upload Rate</div>
                    <div style="font-size: 20px; font-weight: 700; color: var(--purple);" id="txRate">-- KB/s</div>
                </div>
            </div>
            <div class="stat-rows">
                <div class="stat-row">
                    <span>Total Received</span>
                    <span class="stat-val" id="rxTotal">-- GB</span>
                </div>
                <div class="stat-row">
                    <span>Total Sent</span>
                    <span class="stat-val" id="txTotal">-- GB</span>
                </div>
            </div>
            <svg class="sparkline-svg" id="networkSparkline"></svg>
        </div>

        <!-- Thermals Card (Dynamic) -->
        <div class="card" id="thermalCard">
            <div class="card-header">
                <span class="card-title">Thermals</span>
                <span class="card-subtext" id="thermalState">Hardware Sensors</span>
            </div>
            <div class="big-stat">
                <span id="temperature">--</span>
                <span class="unit">&deg;C</span>
            </div>
            <div class="stat-rows" id="thermalSensorsList">
                <div class="stat-row">
                    <span>CPU Package</span>
                    <span class="stat-val" id="cpuTemp">-- &deg;C</span>
                </div>
            </div>
        </div>

        <!-- Battery Card (Dynamic) -->
        <div class="card" id="batteryCard">
            <div class="card-header">
                <span class="card-title">Battery</span>
                <span class="card-subtext" id="batteryStatus">--</span>
            </div>
            <div class="big-stat">
                <span id="batteryCapacity">--</span>
                <span class="unit">%</span>
            </div>
            <div class="progress-bar-bg">
                <div class="progress-bar-fill" id="batteryBar" style="width: 0%;"></div>
            </div>
            <div class="stat-rows">
                <div class="stat-row">
                    <span>Health / Tech</span>
                    <span class="stat-val" id="batteryHealth">--</span>
                </div>
                <div class="stat-row">
                    <span>Voltage / Current</span>
                    <span class="stat-val" id="batteryPower">-- V</span>
                </div>
            </div>
        </div>

        <!-- Services Card (Tests require #sshd and #pm2Services) -->
        <div class="card">
            <div class="card-header">
                <span class="card-title">Monitored Services</span>
                <span class="card-subtext">Daemon Health</span>
            </div>
            <div class="services" id="servicesList">
                <!-- Static SSH row required by tests -->
                <div class="service-item" id="sshd">
                    <span class="service-name">SSH Server</span>
                    <span class="service-badge online" id="sshdStatus">Active</span>
                </div>
                <!-- Dynamic PM2 container required by tests -->
                <div id="pm2Services" style="display: flex; flex-direction: column; gap: 8px;"></div>
                <div id="systemdServices" style="display: flex; flex-direction: column; gap: 8px; margin-top: 4px;"></div>
                <div id="customProcesses" style="display: flex; flex-direction: column; gap: 8px; margin-top: 4px;"></div>
                <div id="healthChecksList" style="display: flex; flex-direction: column; gap: 8px; margin-top: 4px;"></div>
                <div id="dockerContainersList" style="display: flex; flex-direction: column; gap: 8px; margin-top: 4px;"></div>
            </div>
        </div>

        <!-- Speedtest Card -->
        <div class="card" id="speedtestCard">
            <div class="card-header">
                <span class="card-title">Internet Speed Test</span>
                <span class="card-subtext" id="speedtestCooldownNotice">On-Demand</span>
            </div>
            <div style="display: flex; justify-content: space-around; margin: 12px 0;">
                <div>
                    <div style="font-size: 11px; color: var(--muted);">Latency</div>
                    <div style="font-size: 18px; font-weight: 700;" id="stLatency">-- ms</div>
                </div>
                <div>
                    <div style="font-size: 11px; color: var(--muted);">Download</div>
                    <div style="font-size: 18px; font-weight: 700; color: var(--green);" id="stDownload">-- Mbps</div>
                </div>
                <div>
                    <div style="font-size: 11px; color: var(--muted);">Upload</div>
                    <div style="font-size: 18px; font-weight: 700; color: var(--blue);" id="stUpload">-- Mbps</div>
                </div>
            </div>
            <div style="font-size: 11px; color: var(--muted); text-align: center;" id="speedtestStatus">Ready to test.</div>
        </div>

        <!-- LLM Quota Card (Tests require #llm-results) -->
        <div class="card" id="llmCard">
            <div class="card-header">
                <span class="card-title">LLM Quota</span>
                <span class="card-subtext">Antigravity CLI</span>
            </div>
            <div id="llm-results" class="llm-results">
                <div style="font-size: 12px; color: var(--muted); text-align: center; padding: 12px 0;" id="llmNotice">
                    Click "Check LLM Quota" above to query quota limits.
                </div>
            </div>
        </div>
    </div>
</div>

<!-- AUTHENTICATION MODAL -->
<div class="modal-overlay" id="authModal">
    <div class="modal-card">
        <div class="modal-title">Authentication Required</div>
        <div class="modal-text">Enter your access token to view live server telemetry.</div>
        <input type="password" class="modal-input" id="tokenInput" placeholder="ServerMonitor Access Token" />
        <div style="display: flex; justify-content: flex-end; gap: 8px;">
            <button class="btn" onclick="saveAuthToken()">Authenticate</button>
        </div>
    </div>
</div>

<script>
// ============================================================
// CLIENT STATE & UTILITIES
// ============================================================

let isUpdating = false;
let sessionId = sessionStorage.getItem("sm_session_id");
if (!sessionId) {
    sessionId = "sm_" + Math.random().toString(36).substring(2, 10);
    sessionStorage.setItem("sm_session_id", sessionId);
}

function escapeHtml(str) {
    if (!str && str !== 0) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(1) + " " + units[i];
}

function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

function getStoredToken() {
    return sessionStorage.getItem("sm_token") || localStorage.getItem("sm_token") || "";
}

function saveAuthToken() {
    const input = document.getElementById("tokenInput");
    if (input && input.value.trim()) {
        sessionStorage.setItem("sm_token", input.value.trim());
        document.getElementById("authModal").style.display = "none";
        update();
    }
}

// Sparkline SVG renderer
function renderSparkline(svgId, points, color) {
    const svg = document.getElementById(svgId);
    if (!svg || !points || points.length < 2) return;
    const w = svg.clientWidth || 300;
    const h = svg.clientHeight || 36;
    const min = 0;
    const max = Math.max(...points, 100);
    const dx = w / (points.length - 1);

    let d = "";
    points.forEach((val, i) => {
        const x = i * dx;
        const y = h - ((val - min) / (max - min)) * (h - 4) - 2;
        d += (i === 0 ? "M" : "L") + `${x.toFixed(1)},${y.toFixed(1)} `;
    });

    svg.innerHTML = `
        <path class="sparkline-path" d="${d}" stroke="${color}" fill="none" stroke-width="1.5" />
        <path class="sparkline-area" d="${d} L ${w},${h} L 0,${h} Z" fill="${color}" fill-opacity="0.15" stroke="none" />
    `;
}

// ============================================================
// DASHBOARD UPDATE ENGINE
// ============================================================

async function update() {
    if (isUpdating) return;
    if (document.hidden) return;
    isUpdating = true;

    try {
        const token = getStoredToken();
        const headers = { "Content-Type": "application/json" };
        if (token) {
            headers["Authorization"] = "Bearer " + token;
            headers["X-Monitor-Token"] = token;
        }

        const res = await fetch(`/api/status?session=${encodeURIComponent(sessionId)}`, {
            headers: headers
        });

        if (res.status === 401) {
            document.getElementById("authModal").style.display = "flex";
            const pill = document.getElementById("connectionPill");
            pill.className = "status-pill offline";
            document.getElementById("connectionText").textContent = "Auth Required";
            return;
        }

        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();

        // 1. Header & Host Info
        document.getElementById("serverHostname").textContent = data.hostname || "Linux Host";
        document.getElementById("serverArch").textContent = data.architecture || "ARM64";
        if (data.cpu_details && data.cpu_details.model) {
            document.getElementById("cpuModel").textContent = data.cpu_details.model;
        }

        // Connection Pill
        const pill = document.getElementById("connectionPill");
        const connText = document.getElementById("connectionText");
        if (data.collector_state === "active") {
            pill.className = "status-pill";
            connText.textContent = "Live (2s)";
        } else {
            pill.className = "status-pill idle";
            connText.textContent = "Idle (Paused)";
        }

        // 2. CPU Card
        const cpuVal = data.cpu || 0;
        document.getElementById("cpuPercent").textContent = cpuVal.toFixed(1);
        const cpuBar = document.getElementById("cpuBar");
        cpuBar.style.width = Math.min(100, cpuVal) + "%";
        cpuBar.className = "progress-bar-fill" + (cpuVal > 85 ? " danger" : cpuVal > 65 ? " warn" : "");

        document.getElementById("cpuCoresCount").textContent = `${data.cpu_count || 1} Cores`;
        if (data.load && data.load.length >= 3) {
            document.getElementById("loadAvg").textContent = `${data.load[0]} / ${data.load[1]} / ${data.load[2]}`;
        }
        if (data.cpu_details && typeof data.cpu_details.steal === "number") {
            document.getElementById("cpuSteal").textContent = data.cpu_details.steal.toFixed(1) + "%";
        }
        document.getElementById("uptime").textContent = formatUptime(data.uptime || 0);

        // Per-core mini bars
        const coresList = document.getElementById("cpuCoresList");
        if (data.cpu_details && Array.isArray(data.cpu_details.per_core)) {
            coresList.innerHTML = data.cpu_details.per_core.map((val, idx) => `
                <div class="core-pill" title="Core ${idx}: ${val}%">
                    <span class="core-num">${idx}</span>
                    <div class="core-fill" style="height: ${val}%;"></div>
                </div>
            `).join("");
        }

        // 3. Memory Card
        if (data.memory) {
            const mem = data.memory;
            const memTotal = mem.total || 1;
            const memUsed = mem.used || 0;
            const memPct = ((memUsed / memTotal) * 100).toFixed(1);
            document.getElementById("memoryPercent").textContent = memPct;
            document.getElementById("memoryAvailable").textContent = formatBytes(mem.available) + " Available";

            const memBar = document.getElementById("memoryBar");
            memBar.style.width = Math.min(100, memPct) + "%";
            memBar.className = "progress-bar-fill" + (memPct > 85 ? " danger" : memPct > 70 ? " warn" : "");

            document.getElementById("memoryUsage").textContent = `${formatBytes(memUsed)} / ${formatBytes(memTotal)}`;
            document.getElementById("memoryCache").textContent = formatBytes(mem.buffers_cached || 0);

            const swapTotal = mem.swap_total || 0;
            const swapUsed = mem.swap_used || 0;
            document.getElementById("swapUsage").textContent = `${formatBytes(swapUsed)} / ${formatBytes(swapTotal)}`;

            // Container memory detection
            if (mem.container && mem.container.active) {
                document.getElementById("containerBadge").style.display = "inline-block";
                document.getElementById("containerMemRow").style.display = "flex";
                document.getElementById("containerMemVal").textContent =
                    `${formatBytes(mem.container.used)} / ${formatBytes(mem.container.limit)} (${mem.container.percent}%)`;
            }
        }

        // 4. Dynamic Storage Disks
        const disks = data.disks || [];
        const disksContainer = document.getElementById("disksContainer");
        if (disks.length > 0) {
            document.getElementById("storageSummary").textContent = `${disks.length} Mount${disks.length > 1 ? "s" : ""}`;
            disksContainer.innerHTML = disks.map(d => {
                const pct = d.percent || 0;
                const cls = pct > 90 ? "danger" : pct > 75 ? "warn" : "";
                return `
                    <div style="margin-bottom: 6px;">
                        <div style="display: flex; justify-content: space-between; font-size: 13px; font-weight: 500;">
                            <span>${escapeHtml(d.mount)} <small style="color:var(--muted);">(${escapeHtml(d.fstype)})</small></span>
                            <span>${pct}%</span>
                        </div>
                        <div class="progress-bar-bg">
                            <div class="progress-bar-fill ${cls}" style="width: ${Math.min(100, pct)}%;"></div>
                        </div>
                        <div style="display: flex; justify-content: space-between; font-size: 11px; color: var(--muted);">
                            <span>${formatBytes(d.used)} used</span>
                            <span>${formatBytes(d.free)} free</span>
                        </div>
                    </div>
                `;
            }).join("");
        }

        // 5. Network Card
        if (data.network) {
            const net = data.network;
            document.getElementById("primaryInterface").textContent = net.primary || "Active";
            document.getElementById("rxRate").textContent = formatBytes(net.rx_rate || 0) + "/s";
            document.getElementById("txRate").textContent = formatBytes(net.tx_rate || 0) + "/s";
            document.getElementById("rxTotal").textContent = formatBytes(net.rx || 0);
            document.getElementById("txTotal").textContent = formatBytes(net.tx || 0);
        }

        // 6. Thermals Card (Dynamic Hide)
        const thermalCard = document.getElementById("thermalCard");
        if (data.thermal && data.thermal.available === false) {
            thermalCard.style.display = "none";
        } else {
            thermalCard.style.display = "flex";
            const temp = data.temperature || (data.thermal && data.thermal.temperature);
            document.getElementById("temperature").textContent = temp ? temp.toFixed(1) : "--";
            if (data.thermal && Array.isArray(data.thermal.sensors)) {
                document.getElementById("thermalSensorsList").innerHTML = data.thermal.sensors.map(s => `
                    <div class="stat-row">
                        <span>${escapeHtml(s.name)}</span>
                        <span class="stat-val">${s.temp.toFixed(1)} &deg;C</span>
                    </div>
                `).join("");
            }
        }

        // 7. Battery Card (Dynamic Hide)
        const batteryCard = document.getElementById("batteryCard");
        if (data.battery && data.battery.available === false) {
            batteryCard.style.display = "none";
        } else if (data.battery) {
            batteryCard.style.display = "flex";
            const bat = data.battery;
            document.getElementById("batteryCapacity").textContent = bat.capacity !== null ? bat.capacity : "--";
            document.getElementById("batteryStatus").textContent = bat.status || "Unknown";
            document.getElementById("batteryBar").style.width = (bat.capacity || 0) + "%";
            document.getElementById("batteryHealth").textContent = `${bat.health || "Good"} (${bat.technology || "Li-ion"})`;
            const v = bat.voltage !== null ? bat.voltage + " V" : "-- V";
            const c = bat.current !== null ? bat.current + " mA" : "";
            document.getElementById("batteryPower").textContent = [v, c].filter(Boolean).join(" / ");
        }

        // 8. Services Card (PM2 & SSH, matching existing test assertions)
        const pm2Container = document.getElementById("pm2Services");
        const pm2 = (data.services && data.services.pm2) || {};
        if (pm2.status === "ok" && Array.isArray(pm2.services)) {
            if (pm2.services.length === 0) {
                pm2Container.innerHTML = `<div style="font-size:12px;color:var(--muted);padding:4px;">No PM2 services found.</div>`;
            } else {
                pm2Container.innerHTML = pm2.services.map(svc => {
                    const isOnline = svc.status === "online";
                    const badgeClass = isOnline ? "online" : "offline";
                    const memStr = formatBytes(svc.memory || 0);
                    return `
                        <div class="service-item">
                            <span class="service-name">
                                <span>${escapeHtml(svc.name)}</span>
                                <span class="service-meta">(${memStr})</span>
                            </span>
                            <span class="service-badge ${badgeClass}">${escapeHtml(svc.status)}</span>
                        </div>
                    `;
                }).join("");
            }
        } else if (pm2.status === "unavailable") {
            pm2Container.innerHTML = `<div style="font-size:12px;color:var(--muted);padding:4px;">PM2 is not installed or unavailable.</div>`;
        } else if (pm2.status === "error") {
            pm2Container.innerHTML = `<div style="font-size:12px;color:var(--red);padding:4px;">Error: ${escapeHtml(pm2.error || "PM2 error")}</div>`;
        }

        // Update SSH status
        const sshdActive = data.services && data.services.sshd;
        const sshdBadge = document.getElementById("sshdStatus");
        if (sshdBadge) {
            sshdBadge.className = "service-badge " + (sshdActive ? "online" : "offline");
            sshdBadge.textContent = sshdActive ? "Active" : "Inactive";
        }

        // Systemd Services
        const sysdContainer = document.getElementById("systemdServices");
        const sysd = (data.services && data.services.systemd) || {};
        if (sysdContainer && sysd.available && sysd.units) {
            const unitKeys = Object.keys(sysd.units);
            if (unitKeys.length > 0) {
                sysdContainer.innerHTML = unitKeys.map(unit => {
                    const state = sysd.units[unit];
                    const isActive = state === "active";
                    const badgeClass = isActive ? "online" : "offline";
                    return `
                        <div class="service-item">
                            <span class="service-name">${escapeHtml(unit)} <small style="color:var(--muted);">(systemd)</small></span>
                            <span class="service-badge ${badgeClass}">${escapeHtml(state)}</span>
                        </div>
                    `;
                }).join("");
            } else {
                sysdContainer.innerHTML = "";
            }
        }

        // Monitored Processes
        const procContainer = document.getElementById("customProcesses");
        const procs = (data.services && data.services.processes) || {};
        if (procContainer) {
            const procKeys = Object.keys(procs).filter(k => k !== "sshd");
            if (procKeys.length > 0) {
                procContainer.innerHTML = procKeys.map(name => {
                    const pinfo = procs[name];
                    const isRunning = pinfo && pinfo.running;
                    const badgeClass = isRunning ? "online" : "offline";
                    const countStr = isRunning ? `(${pinfo.count} proc)` : "";
                    return `
                        <div class="service-item">
                            <span class="service-name">${escapeHtml(name)} <small style="color:var(--muted);">${countStr}</small></span>
                            <span class="service-badge ${badgeClass}">${isRunning ? "Running" : "Stopped"}</span>
                        </div>
                    `;
                }).join("");
            } else {
                procContainer.innerHTML = "";
            }
        }

        // HTTP/TCP Health Checks
        const hcContainer = document.getElementById("healthChecksList");
        const checks = (data.services && data.services.health_checks) || [];
        if (hcContainer) {
            if (checks.length > 0) {
                hcContainer.innerHTML = checks.map(c => {
                    const isUp = c.status === "up";
                    const badgeClass = isUp ? "online" : (c.status === "warn" ? "warning" : "offline");
                    const latency = c.latency_ms !== undefined ? `(${c.latency_ms} ms)` : "";
                    return `
                        <div class="service-item">
                            <span class="service-name">${escapeHtml(c.name)} <small style="color:var(--muted);">${latency}</small></span>
                            <span class="service-badge ${badgeClass}">${escapeHtml(c.status.toUpperCase())}</span>
                        </div>
                    `;
                }).join("");
            } else {
                hcContainer.innerHTML = "";
            }
        }

        // Docker Containers
        const dkrContainer = document.getElementById("dockerContainersList");
        const dkr = (data.services && data.services.docker) || {};
        if (dkrContainer && dkr.available && Array.isArray(dkr.containers)) {
            if (dkr.containers.length > 0) {
                dkrContainer.innerHTML = `
                    <div style="font-size:11px;font-weight:600;color:var(--muted);margin-top:4px;">
                        ${escapeHtml(dkr.runtime || "Container")} (${dkr.running}/${dkr.total} running)
                    </div>
                ` + dkr.containers.map(c => {
                    const isRunning = c.state === "running";
                    const badgeClass = isRunning ? "online" : "offline";
                    return `
                        <div class="service-item">
                            <span class="service-name">
                                <span>${escapeHtml(c.name)}</span>
                                <span class="service-meta">(${escapeHtml(c.image)})</span>
                            </span>
                            <span class="service-badge ${badgeClass}">${escapeHtml(c.state)}</span>
                        </div>
                    `;
                }).join("");
            } else {
                dkrContainer.innerHTML = "";
            }
        }

        // 9. Sparkline Charts with standard hex colors
        if (data.history) {
            renderSparkline("cpuSparkline", data.history.cpu, "#58a6ff");
            renderSparkline("memorySparkline", data.history.memory, "#3fb950");
            renderSparkline("networkSparkline", data.history.rx_rate, "#bc8cff");
        }

    } catch (err) {
        const pill = document.getElementById("connectionPill");
        pill.className = "status-pill offline";
        document.getElementById("connectionText").textContent = "Reconnecting";
    } finally {
        isUpdating = false;
    }
}

// ============================================================
// ON-DEMAND SPEED TEST
// ============================================================

async function runSpeedTest() {
    const btn = document.getElementById("speedtestBtn");
    const status = document.getElementById("speedtestStatus");
    btn.disabled = true;
    status.textContent = "Testing network latency and throughput...";

    try {
        const token = getStoredToken();
        const headers = {};
        if (token) headers["Authorization"] = "Bearer " + token;

        const res = await fetch("/api/speedtest", { headers });
        const data = await res.json();

        if (data.success) {
            document.getElementById("stLatency").textContent = (data.latency || "--") + " ms";
            document.getElementById("stDownload").textContent = (data.download || "--") + " Mbps";
            document.getElementById("stUpload").textContent = (data.upload || "--") + " Mbps";
            status.textContent = "Speed test completed successfully.";
        } else {
            status.textContent = "Test failed: " + (data.error || "Unknown error");
        }
    } catch (e) {
        status.textContent = "Speed test request error: " + e.message;
    } finally {
        btn.disabled = false;
    }
}

// ============================================================
// ON-DEMAND LLM QUOTA (PR #1 COMPATIBLE)
// ============================================================

async function runLlmUsage() {
    const button = document.getElementById("llmusage-button");
    const resultsContainer = document.getElementById("llm-results");
    const note = document.getElementById("llmNotice");

    button.disabled = true;
    button.textContent = "Checking...";
    if (note) note.textContent = "Querying Antigravity CLI quota...";

    try {
        const token = getStoredToken();
        const headers = {};
        if (token) headers["Authorization"] = "Bearer " + token;

        const response = await fetch("/api/llmusage", { headers });
        const data = await response.json();

        if (!data.success) {
            resultsContainer.innerHTML = `
                <div style="font-size:12px;color:var(--red);padding:6px 0;">Error: ${escapeHtml(data.error || "Unknown error")}</div>
            `;
            if (note) note.textContent = "Could not fetch LLM usage quota.";
            return;
        }

        if (!data.groups || data.groups.length === 0) {
            resultsContainer.innerHTML = `<div style="font-size:12px;color:var(--muted);padding:6px 0;">No quota information reported.</div>`;
        } else {
            resultsContainer.innerHTML = data.groups.map(group => {
                const bucketsHtml = group.buckets.map(b => {
                    const colorClass = b.percentage >= 60 ? "high" : (b.percentage >= 20 ? "medium" : "low");
                    const resetTimeStr = b.reset_time ? escapeHtml(b.reset_time) : "";
                    return `
                        <div class="llm-bucket">
                            <div class="llm-bucket-header">
                                <span class="llm-bucket-name">${escapeHtml(b.name)}</span>
                                <span class="llm-bucket-val ${colorClass}">${b.percentage}%</span>
                            </div>
                            ${resetTimeStr ? `<span class="llm-bucket-reset">Resets: ${resetTimeStr}</span>` : ""}
                        </div>
                    `;
                }).join("");

                return `
                    <div class="llm-group">
                        <div class="llm-group-title">${escapeHtml(group.name)}</div>
                        ${bucketsHtml}
                    </div>
                `;
            }).join("");
        }
        if (note) note.textContent = "Quota refreshed successfully.";
    } catch (error) {
        resultsContainer.innerHTML = `
            <div style="font-size:12px;color:var(--red);padding:6px 0;">Error: ${escapeHtml(error.message)}</div>
        `;
        if (note) note.textContent = "Could not fetch LLM usage quota.";
    } finally {
        button.disabled = false;
        button.textContent = "Check LLM Quota";
    }
}

// ============================================================
// LIFECYCLE & POLLING (Interval: 2000ms as required by tests)
// ============================================================

update();

setInterval(
    update,
    2000
);

document.addEventListener(
    "visibilitychange",
    () => {
        if (!document.hidden) {
            update();
        }
    }
);
</script>
</body>
</html>
"""


# ============================================================
# HTTP REQUEST HANDLER
# ============================================================

class Handler(BaseHTTPRequestHandler):
    timeout = 10.0

    def handle(self):
        try:
            super().handle()
        except (socket.timeout, TimeoutError):
            self.close_connection = True

    def log_message(self, format, *args):
        # Disable verbose access logging
        pass

    def send_security_headers(self):
        for header, value in get_security_headers().items():
            self.send_header(header, value)

    def send_data(self, content, content_type, status_code=200):
        body = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def is_authenticated(self):
        global _global_config
        expected_token = _global_config.auth_token if _global_config else ""
        if not expected_token:
            return True
        provided_token = extract_token(self.headers, self.path)
        return verify_token(expected_token, provided_token)

    def is_allowed_host(self):
        global _global_config, HOST
        host_header = self.headers.get("Host")
        if not host_header:
            return False

        # Extract hostname/IP without port
        host_val = host_header.strip()
        if host_val.startswith("["):
            # IPv6 literal [::1]:8080 or [::1]
            end_bracket = host_val.find("]")
            if end_bracket != -1:
                hostname = host_val[1:end_bracket]
            else:
                hostname = host_val
        else:
            hostname = host_val.split(":")[0]

        if not hostname:
            return False

        allowed = {"127.0.0.1", "localhost", "::1"}
        cfg_host = _global_config.host if _global_config else HOST
        if cfg_host and cfg_host not in ("0.0.0.0", "::"):
            allowed.add(cfg_host)

        if _global_config and hasattr(_global_config, "data"):
            proxies = _global_config.data.get("trusted_proxies", [])
            for p in proxies:
                allowed.add(p)
            custom_allowed = _global_config.data.get("allowed_hosts", [])
            for h in custom_allowed:
                allowed.add(h)

        local_ip = get_local_ip()
        if local_ip:
            allowed.add(local_ip)

        hostname_lower = hostname.lower()

        # Wildcard allow-all check
        if "*" in custom_allowed or "*" in allowed:
            return True

        # Exact match
        allowed_set = {h.lower() for h in allowed}
        if hostname_lower in allowed_set:
            return True

        # Tailscale domain (*.ts.net) or CGNAT IP (100.64.0.0/10)
        if hostname_lower.endswith(".ts.net"):
            return True
        if hostname.startswith("100."):
            parts = hostname.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                b1 = int(parts[0])
                b2 = int(parts[1])
                if b1 == 100 and (64 <= b2 <= 127):
                    return True

        # Subdomain / wildcard matching (e.g. *.example.com or .example.com)
        for pattern in custom_allowed:
            pat = pattern.lower()
            if pat.startswith("*.") and hostname_lower.endswith(pat[1:]):
                return True
            if pat.startswith(".") and hostname_lower.endswith(pat):
                return True

        return False

    def get_client_ip(self):
        global _global_config
        peer_ip = self.client_address[0] if self.client_address else "127.0.0.1"
        trusted = set(_global_config.trusted_proxies) if _global_config and hasattr(_global_config, "trusted_proxies") else {"127.0.0.1", "::1"}

        # Immediate peer must be in trusted_proxies to inspect any forwarding headers.
        # This completely prevents header spoofing by direct untrusted clients.
        if peer_ip not in trusted:
            return peer_ip

        # Check X-Forwarded-For: "client, proxy1, proxy2"
        xff = self.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            # Walk right-to-left to locate the first untrusted client IP
            for candidate in reversed(parts):
                if candidate not in trusted:
                    return candidate
            if parts:
                return parts[0]

        # Check X-Real-IP
        x_real = self.headers.get("x-real-ip")
        if x_real and x_real.strip():
            candidate = x_real.strip()
            if candidate not in trusted:
                return candidate

        return peer_ip

    def check_rate_limit(self, limiter):
        client_ip = self.get_client_ip()
        return limiter.allow_request(client_ip)

    def do_GET(self):
        global _global_config

        # DNS Rebinding Defense: Host Header Verification
        if not self.is_allowed_host():
            self.send_data(json.dumps({"error": "Invalid or missing Host header"}), "application/json", 400)
            return

        # Rate Limiting
        if not self.check_rate_limit(_global_rate_limiter):
            self.send_data(json.dumps({"error": "Rate limit exceeded"}), "application/json", 429)
            return

        # Dashboard Route
        if self.path == "/" or self.path.startswith("/?"):
            self.send_data(HTML, "text/html; charset=utf-8")
            return

        # Authentication Check for API Routes
        if self.path.startswith("/api/"):
            if not self.is_authenticated():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer realm="ServerMonitor"')
                self.send_security_headers()
                self.send_header("Content-Type", "application/json")
                body = json.dumps({"error": "Unauthorized: valid access token required"}).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        # Status API Route
        if self.path == "/api/status" or self.path.startswith("/api/status?"):
            session_id = "default"
            if "?" in self.path:
                for param in self.path.split("?", 1)[1].split("&"):
                    if param.startswith("session="):
                        session_id = param.split("=", 1)[1].strip()
                        break
            status_data = get_status(client_id=session_id)
            self.send_data(json.dumps(status_data), "application/json")
            return

        # Speedtest Route
        if self.path == "/api/speedtest":
            cfg = _global_config or Config()
            if not cfg.speedtest_enabled:
                self.send_data(json.dumps({"success": False, "error": "Speed test is disabled in server configuration"}), "application/json", 403)
                return
            if not self.check_rate_limit(_speedtest_rate_limiter):
                self.send_data(json.dumps({"success": False, "error": "Speedtest rate limit exceeded"}), "application/json", 429)
                return
            result = run_speed_test()
            self.send_data(json.dumps(result), "application/json")
            return

        # Antigravity LLM Quota Route
        if self.path == "/api/llmusage":
            result = run_llm_usage()
            self.send_data(json.dumps(result), "application/json")
            return

        # Configuration Info Route
        if self.path == "/api/config":
            cfg = _global_config or Config()
            safe_info = {
                "auth_required": bool(cfg.auth_token),
                "sample_interval": cfg.sample_interval,
                "inactivity_timeout": cfg.inactivity_timeout,
                "speedtest_enabled": cfg.speedtest_enabled,
                "version": VERSION
            }
            self.send_data(json.dumps(safe_info), "application/json")
            return

        # 404 Fallback
        self.send_response(404)
        self.send_security_headers()
        self.end_headers()

    def do_POST(self):
        # 1. DNS Rebinding Defense: Host Header Verification
        if not self.is_allowed_host():
            self.send_data(json.dumps({"error": "Invalid or missing Host header"}), "application/json", 400)
            return

        # 2. Rate Limiting
        if not self.check_rate_limit(_global_rate_limiter):
            self.send_data(json.dumps({"error": "Rate limit exceeded"}), "application/json", 429)
            return

        # 3. Route Validation
        is_heartbeat = (self.path == "/api/heartbeat" or self.path.startswith("/api/heartbeat?"))
        if not is_heartbeat:
            self.send_response(404)
            self.send_security_headers()
            self.end_headers()
            return

        # 4. Authentication Check BEFORE Reading Request Body
        if not self.is_authenticated():
            self.send_data(json.dumps({"error": "Unauthorized"}), "application/json", 401)
            return

        # 5. Content-Length Validation & Body Size Limiting (DoS Defense)
        cl_header = self.headers.get("Content-Length")
        content_length = 0
        if cl_header is not None:
            try:
                content_length = int(cl_header.strip())
                if content_length < 0:
                    self.send_data(json.dumps({"error": "Invalid Content-Length"}), "application/json", 400)
                    return
            except (ValueError, TypeError):
                self.send_data(json.dumps({"error": "Invalid Content-Length"}), "application/json", 400)
                return

        MAX_CONTENT_LENGTH = 65536  # 64 KB limit
        if content_length > MAX_CONTENT_LENGTH:
            self.send_data(json.dumps({"error": "Payload Too Large: Content-Length exceeds limit"}), "application/json", 413)
            return

        # 6. Read Body Safely
        if content_length > 0:
            self.rfile.read(content_length)

        # 7. Process Heartbeat Endpoint
        session_id = "default"
        if "?" in self.path:
            for param in self.path.split("?", 1)[1].split("&"):
                if param.startswith("session="):
                    session_id = param.split("=", 1)[1].strip()
                    break
        if _global_collector:
            _global_collector.touch_session(session_id)
        self.send_data(json.dumps({"status": "ok", "state": _global_collector.get_state() if _global_collector else "standalone"}), "application/json")


# ============================================================
# SYSTEMD & PM2 TEMPLATE GENERATORS
# ============================================================

def generate_ssh_tunnel_command(port=8080, host=None, user=None):
    """Generates the copy-paste SSH tunnel port forwarding command."""
    if not host:
        host = get_local_ip()
    if not user:
        user = os.environ.get("USER", "root")
    return f"ssh -N -L {port}:127.0.0.1:{port} {user}@{host}"


def generate_pm2_config(app_name="servermonitor", script_path=None, port=8080, host="127.0.0.1"):
    """Generates a standalone PM2 ecosystem configuration."""
    if not script_path:
        script_path = os.path.abspath(__file__)
    return f"""module.exports = {{
  apps: [{{
    name: '{app_name}',
    script: '{script_path}',
    interpreter: '{sys.executable}',
    args: '--host {host} --port {port}',
    env: {{
      NODE_ENV: 'production',
      PORT: '{port}',
      HOST: '{host}'
    }},
    max_memory_restart: '80M',
    autorestart: true
  }}]
}};
"""


def generate_systemd_service(user=None, exec_start=None, working_dir=None):
    """Generates a hardened, read-only systemd service definition."""
    if not user:
        sudo_user = os.environ.get("SUDO_USER")
        current_user = os.environ.get("USER")
        is_root = False
        if hasattr(os, "geteuid"):
            is_root = (os.geteuid() == 0)
        elif current_user == "root":
            is_root = True

        if is_root:
            user = sudo_user or "nobody"
        else:
            user = current_user or "nobody"
    if not exec_start:
        exec_start = f"{sys.executable} {os.path.abspath(__file__)}"
    if not working_dir:
        working_dir = os.path.dirname(os.path.abspath(__file__))

    return f"""[Unit]
Description=ServerMonitor Universal Linux Monitoring Service
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={working_dir}
ExecStart={exec_start}
Restart=always
RestartSec=5s

# Security Hardening Directives
ProtectSystem=strict
ProtectHome=read-only
NoNewPrivileges=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
ProtectKernelModules=true
RestrictRealtime=true
RestrictSUIDSGID=true

# Sandboxed Writable State & Configuration Isolation
StateDirectory=servermonitor
ConfigurationDirectory=servermonitor
ReadOnlyPaths={working_dir}

[Install]
WantedBy=multi-user.target
"""


def get_local_ip():
    """Detects primary local LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ============================================================
# MODERN INTERACTIVE CLI SETUP WIZARD
# ============================================================

def run_interactive_setup(config_file=None):
    """Interactive CLI setup wizard with validation and deployment configuration."""
    print("\n" + "=" * 64)
    print("  ServerMonitor v2.0 - Universal Production Setup Wizard")
    print("=" * 64 + "\n")

    config = Config(config_file=config_file)

    # 1. Host and Port
    print("[1/5] Network Binding & Port")
    host_input = input(f"  Bind address [{config.host}]: ").strip()
    if host_input:
        config.host = host_input

    port_input = input(f"  Port [{config.port}]: ").strip()
    if port_input and port_input.isdigit():
        config.port = int(port_input)

    # 2. Authentication
    print("\n[2/5] Access Security & Authentication")
    is_loopback = config.host in ("127.0.0.1", "localhost", "::1")
    gen_token = secrets.token_hex(24)

    # Reverse proxy / Domain check
    rev_choice = input(f"  Will ServerMonitor be accessed via reverse proxy (Caddy/Nginx) or domain? [y/N]: ").strip().lower()
    is_proxied = rev_choice in ("y", "yes")
    if is_proxied:
        dom_input = input(f"  Enter allowed domain or host pattern (e.g. monitor.example.com or *): ").strip()
        if dom_input and dom_input not in config.data["allowed_hosts"]:
            config.data["allowed_hosts"].append(dom_input)

    if not is_loopback or is_proxied:
        print("  [!] Notice: Non-loopback or reverse-proxied deployment detected.")
        print(f"  [!] An access token (min {MIN_TOKEN_LENGTH} chars) is MANDATORY to prevent unauthorized access.")
        while True:
            try:
                import getpass
                token_input = getpass.getpass(f"  Enter access token (min {MIN_TOKEN_LENGTH} chars, or press Enter to generate): ").strip()
            except Exception:
                token_input = input(f"  Enter access token (min {MIN_TOKEN_LENGTH} chars, or press Enter to generate): ").strip()

            if not token_input:
                config.auth_token = gen_token
                print(f"  -> Generated secure access token: {config.auth_token}")
                break
            elif len(token_input) < MIN_TOKEN_LENGTH:
                print(f"  [!] Token too weak ({len(token_input)} chars). Minimum length is {MIN_TOKEN_LENGTH} characters.")
            else:
                config.auth_token = token_input
                print("  -> Custom access token accepted.")
                break
    else:
        print("  (Tip: A secret token protects your dashboard when accessible over LAN/WAN)")
        auth_choice = input(f"  Set access token? [y/N]: ").strip().lower()
        if auth_choice in ("y", "yes"):
            while True:
                try:
                    import getpass
                    token_input = getpass.getpass(f"  Enter token (min {MIN_TOKEN_LENGTH} chars, or press Enter to generate): ").strip()
                except Exception:
                    token_input = input(f"  Enter token (min {MIN_TOKEN_LENGTH} chars, or press Enter to generate): ").strip()

                if not token_input:
                    config.auth_token = gen_token
                    print(f"  -> Generated secure access token: {config.auth_token}")
                    break
                elif len(token_input) < MIN_TOKEN_LENGTH:
                    print(f"  [!] Token too weak ({len(token_input)} chars). Minimum length is {MIN_TOKEN_LENGTH} characters.")
                else:
                    config.auth_token = token_input
                    print("  -> Custom access token accepted.")
                    break
        else:
            config.auth_token = ""
            print("  -> Running in tokenless mode (Recommended for localhost / SSH tunnel only).")

    # 3. Intervals
    print("\n[3/5] Telemetry Cadence & Resource Conservation")
    interval_input = input(f"  Sampling interval in seconds [{config.sample_interval}]: ").strip()
    if interval_input:
        try:
            config.data["sample_interval"] = float(interval_input)
        except ValueError:
            pass

    timeout_input = input(f"  Inactivity pause timeout in seconds [{config.inactivity_timeout}]: ").strip()
    if timeout_input:
        try:
            config.data["inactivity_timeout"] = float(timeout_input)
        except ValueError:
            pass

    # 4. Save Config
    print("\n[4/5] Saving Configuration")
    config.save()
    print(f"  [✓] Configuration securely saved to: {config.config_file} (mode 0600)")

    # 5. Remote Access Guidance
    print("\n[5/5] Remote Access & Deployment Guidance")
    lan_ip = get_local_ip()
    print(f"  Local Access:      http://127.0.0.1:{config.port}")
    if config.host != "127.0.0.1":
        print(f"  LAN Access:        http://{lan_ip}:{config.port}")
    print(f"  SSH Tunnel:        {generate_ssh_tunnel_command(port=config.port, host=lan_ip)}")

    # Tailscale Check
    tailscale_bin = shutil.which("tailscale")
    if tailscale_bin:
        print("  Tailscale Status:  Detected installed. You can bind to your Tailscale 100.x.y.z IP.")
    else:
        print("  Tailscale Status:  Not installed. (Tip: Install Tailscale for zero-config encrypted mesh access)")

    # Deployment choice
    print("\n" + "-" * 64)
    print("Deployment Options:")
    print("  1. Run Standalone:    python3 server.py")
    print(f"  2. PM2 Ecosystem:     pm2 start server.py --name servermonitor-prod -- --port {config.port}")
    print("  3. Systemd Unit:      Generate unit with: python3 server.py --generate-systemd")
    print("=" * 64 + "\n")


# ============================================================
# BENCHMARK COMMAND
# ============================================================

def run_benchmark():
    """Benchmarks idle vs active memory and CPU footprint using os.times()."""
    print("\n" + "=" * 60)
    print("  ServerMonitor Performance & Resource Footprint Benchmark")
    print("=" * 60)

    pid = os.getpid()
    print(f"  Testing PID: {pid}")
    print("  Measuring baseline idle footprint (2s sleep)...")

    # Read memory RSS from /proc/<pid>/status
    def get_rss_kb():
        try:
            for line in read_file(f"/proc/{pid}/status").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
        except Exception:
            return 0
        return 0

    t0_wall = time.monotonic()
    t0_times = os.times()
    time.sleep(2.0)
    t1_wall = time.monotonic()
    t1_times = os.times()

    idle_wall = t1_wall - t0_wall
    idle_cpu_time = (t1_times.user - t0_times.user) + (t1_times.system - t0_times.system)
    idle_cpu_pct = (idle_cpu_time / idle_wall) * 100.0 if idle_wall > 0 else 0.0

    rss_idle = get_rss_kb() / 1024.0
    print(f"  Idle Memory RSS:    {rss_idle:.2f} MB")
    print(f"  Idle CPU Usage:     {idle_cpu_pct:.2f}% (Event-driven sleeping)")

    # Simulate 50 status queries
    print("  Simulating 50 rapid telemetry sampling requests...")
    t2_wall = time.monotonic()
    t2_times = os.times()
    for _ in range(50):
        get_status(client_id="benchmark")
    t3_wall = time.monotonic()
    t3_times = os.times()

    active_wall = t3_wall - t2_wall
    active_cpu_time = (t3_times.user - t2_times.user) + (t3_times.system - t2_times.system)
    active_cpu_pct = (active_cpu_time / active_wall) * 100.0 if active_wall > 0 else 0.0

    rss_active = get_rss_kb() / 1024.0
    print(f"  Active Memory RSS:  {rss_active:.2f} MB (Delta: +{(rss_active - rss_idle):.2f} MB)")
    print(f"  Active CPU Usage:   {active_cpu_pct:.2f}%")
    print(f"  50 Samples Time:    {active_wall:.3f} s ({active_wall / 50.0 * 1000:.2f} ms/query)")
    print("  Zero-dependency verification: Pure Python standard library only.")
    print("=" * 60 + "\n")


# ============================================================
# MAIN ENTRYPOINT
# ============================================================

def main():
    global HOST, PORT, _global_collector, _global_config

    parser = argparse.ArgumentParser(description="ServerMonitor: Universal Linux Server Monitoring")
    parser.add_argument("--host", help=f"Host interface to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--config", help="Path to config.json")
    parser.add_argument("--token", help="Access authentication token")
    parser.add_argument("--setup", action="store_true", help="Launch interactive setup wizard")
    parser.add_argument("--benchmark", action="store_true", help="Run memory/CPU resource benchmark")
    parser.add_argument("--tunnel", action="store_true", help="Print SSH tunnel command")
    parser.add_argument("--generate-systemd", action="store_true", help="Output hardened systemd unit file")
    parser.add_argument("--user", help="User to run systemd service as (default: non-root user or nobody)")
    parser.add_argument("--generate-pm2", action="store_true", help="Output PM2 ecosystem configuration")
    parser.add_argument("--status", action="store_true", help="Check running status of ServerMonitor")

    args = parser.parse_args()

    # CLI subcommands
    if args.setup:
        run_interactive_setup(config_file=args.config)
        return

    if args.benchmark:
        run_benchmark()
        return

    if args.tunnel:
        port = args.port or DEFAULT_PORT
        tunnel_host = args.host or get_local_ip()
        print("\nReady-to-use SSH Port Forwarding Command:")
        print("  " + generate_ssh_tunnel_command(port=port, host=tunnel_host))
        print("\nTip: If accessing a remote cloud VM (GCP, AWS, OCI), provide your public IP/hostname:")
        print(f"  servermonitor tunnel --host <cloud-ip> --port {port}\n")
        print(f"Then open http://127.0.0.1:{port} in your local browser.\n")
        return

    if args.generate_systemd:
        print(generate_systemd_service(user=args.user))
        return

    if args.generate_pm2:
        print(generate_pm2_config(port=args.port or DEFAULT_PORT))
        return

    if args.status:
        # Load configuration to resolve host, port, and authentication token
        cfg = Config(config_file=args.config)
        test_port = args.port or cfg.port or DEFAULT_PORT
        test_host = args.host or cfg.host or "127.0.0.1"
        token = args.token or cfg.auth_token or ""

        # Properly format IPv6 addresses in URLs
        formatted_host = f"[{test_host}]" if ":" in test_host and not test_host.startswith("[") else test_host
        url = f"http://{formatted_host}:{test_port}/api/config"
        headers = {"User-Agent": f"ServerMonitor/{VERSION}-CLI"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Monitor-Token"] = token

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                auth_str = " (Authenticated)" if token else ""
                print(f"[✓] ServerMonitor is running on http://{formatted_host}:{test_port}{auth_str} (v{data.get('version', VERSION)})")
                return
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                if token:
                    print(f"[!] ServerMonitor is running on http://{formatted_host}:{test_port}, but provided token was rejected (HTTP {e.code}).")
                else:
                    print(f"[✓] ServerMonitor is running on http://{formatted_host}:{test_port} (Protected - HTTP {e.code} Unauthorized). Use --token <secret> to authenticate.")
                return
            else:
                print(f"[!] ServerMonitor returned HTTP {e.code} on http://{formatted_host}:{test_port}")
                sys.exit(1)
        except urllib.error.URLError as e:
            print(f"[✗] ServerMonitor is not running or unreachable on http://{formatted_host}:{test_port} ({e.reason})")
            sys.exit(1)
        except Exception as e:
            print(f"[✗] Error connecting to ServerMonitor on http://{formatted_host}:{test_port}: {e}")
            sys.exit(1)

    # Load configuration
    _global_config = Config(config_file=args.config)
    if args.host:
        _global_config.host = args.host
    if args.port:
        _global_config.port = args.port
    if args.token:
        _global_config.auth_token = args.token

    HOST = _global_config.host
    PORT = _global_config.port

    if args.token and len(args.token) < MIN_TOKEN_LENGTH:
        sys.stderr.write(
            f"Security Error: Provided token is too weak ({len(args.token)} characters). "
            f"Tokens must be at least {MIN_TOKEN_LENGTH} characters long.\n"
        )
        sys.exit(1)

    # Remote access security check: reject non-loopback binding without authentication
    is_loopback = HOST in ("127.0.0.1", "localhost", "::1")
    if not is_loopback:
        if not _global_config.auth_token:
            sys.stderr.write(
                f"Security Error: ServerMonitor is configured to bind to non-loopback interface '{HOST}' "
                f"without authentication enabled.\n"
                f"Binding to external or LAN interfaces without an access token is rejected for security.\n"
                f"Please specify an authentication token with --token <secret> or configure 'auth_token' in config.json.\n"
                f"Alternatively, bind to 127.0.0.1 (default) and access via SSH tunnel or reverse proxy.\n"
            )
            sys.exit(1)
        if len(_global_config.auth_token) < MIN_TOKEN_LENGTH:
            sys.stderr.write(
                f"Security Error: Configured authentication token is too weak ({len(_global_config.auth_token)} characters). "
                f"Tokens must be at least {MIN_TOKEN_LENGTH} characters for non-loopback bindings.\n"
            )
            sys.exit(1)

    # Start On-Demand Collector
    _global_collector = OnDemandCollector(
        sample_interval=_global_config.sample_interval,
        inactivity_timeout=_global_config.inactivity_timeout,
        history_maxlen=_global_config.history_points
    )
    _global_collector.start()

    # Graceful shutdown handlers
    def handle_exit(sig, frame):
        if _global_collector:
            _global_collector.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # Initialize Threading HTTP Server
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    print("=" * 64)
    print(f"  ServerMonitor v{VERSION} - Universal Production Linux Monitor")
    print(f"  Listening on: http://{HOST}:{PORT}")
    if _global_config.auth_token:
        print(f"  Authentication: Enabled (Token Protected)")
    else:
        print(f"  Authentication: Disabled (Localhost / Trusted Network Mode)")
    print(f"  On-Demand Sampling: {int(_global_config.sample_interval)}s live, idle after {int(_global_config.inactivity_timeout)}s")
    print("=" * 64)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if _global_collector:
            _global_collector.stop()
        server.server_close()


if __name__ == "__main__":
    main()
