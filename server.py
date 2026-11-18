#!/usr/bin/env python3

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOST = "0.0.0.0"
PORT = 8080

# Sampling state
_previous_cpu = None
_previous_network = None
_previous_sample_time = None

# PM2 Cache state
_pm2_cache_data = None
_pm2_cache_time = 0.0
_pm2_cache_lock = threading.Lock()
PM2_CACHE_TTL = 5.0



# ============================================================
# BASIC FILE READER
# ============================================================

def read_file(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except (OSError, IOError):
        return ""


# ============================================================
# CPU
# ============================================================

def get_cpu_usage():
    global _previous_cpu

    try:
        values = None

        for line in read_file("/proc/stat").splitlines():
            if line.startswith("cpu "):
                values = list(map(int, line.split()[1:]))
                break

        if not values:
            return 0.0

        idle = values[3] + (
            values[4] if len(values) > 4 else 0
        )

        total = sum(values)

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

        return round(
            (1 - idle_delta / total_delta) * 100,
            1
        )

    except (OSError, ValueError, IndexError):
        return 0.0


# ============================================================
# MEMORY
# ============================================================

def get_memory():
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

    available = values.get(
        "MemAvailable",
        values.get("MemFree", 0)
    )

    used = max(0, total - available)

    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)

    swap_used = max(
        0,
        swap_total - swap_free
    )

    return {
        "total": total,
        "used": used,
        "available": available,
        "swap_total": swap_total,
        "swap_used": swap_used,
    }


# ============================================================
# LOAD / UPTIME / STORAGE
# ============================================================

def get_load():
    try:
        return [
            round(x, 2)
            for x in os.getloadavg()
        ]
    except OSError:
        return [0, 0, 0]


def get_uptime():
    try:
        return float(
            read_file("/proc/uptime")
            .split()[0]
        )
    except (ValueError, IndexError):
        return 0


def get_storage():
    try:
        stat = os.statvfs("/")

        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used = total - free

        return {
            "total": total,
            "used": used,
            "free": free,
        }

    except OSError:
        return {
            "total": 0,
            "used": 0,
            "free": 0,
        }


# ============================================================
# SYSTEM
# ============================================================

def get_hostname():
    return (
        read_file("/etc/hostname").strip()
        or os.uname().nodename
    )


def get_cpu_count():
    return os.cpu_count() or 1


# ============================================================
# NETWORK TRAFFIC
# ============================================================

def get_network():
    rx = 0
    tx = 0

    try:
        with open("/proc/net/dev") as f:

            for line in f:

                if ":" not in line:
                    continue

                interface, data = line.split(":", 1)

                if interface.strip() == "lo":
                    continue

                values = data.split()

                if len(values) >= 9:
                    rx += int(values[0])
                    tx += int(values[8])

    except (OSError, ValueError):
        pass

    global _previous_network
    global _previous_sample_time

    now = time.monotonic()

    rx_rate = 0.0
    tx_rate = 0.0

    if (
        _previous_network is not None
        and _previous_sample_time is not None
    ):

        elapsed = (
            now - _previous_sample_time
        )

        if elapsed > 0:

            rx_rate = max(
                0,
                (
                    rx -
                    _previous_network["rx"]
                ) / elapsed
            )

            tx_rate = max(
                0,
                (
                    tx -
                    _previous_network["tx"]
                ) / elapsed
            )

    _previous_network = {
        "rx": rx,
        "tx": tx,
    }

    _previous_sample_time = now

    return {
        "rx": rx,
        "tx": tx,
        "rx_rate": round(rx_rate, 1),
        "tx_rate": round(tx_rate, 1),
    }


# ============================================================
# TEMPERATURE
# ============================================================

def get_temperature():
    temperatures = []

    try:

        for zone in os.listdir(
            "/sys/class/thermal"
        ):

            if not zone.startswith(
                "thermal_zone"
            ):
                continue

            temp = read_file(
                f"/sys/class/thermal/{zone}/temp"
            ).strip()

            if not temp:
                continue

            try:
                value = int(temp)
            except ValueError:
                continue

            if value > 1000:
                value /= 1000

            if -20 <= value <= 120:
                temperatures.append(value)

    except OSError:
        pass

    if not temperatures:
        return None

    return round(
        max(temperatures),
        1
    )


# ============================================================
# PROCESS CHECK
# ============================================================

def get_process_status(process_name):

    try:

        for pid in os.listdir("/proc"):

            if not pid.isdigit():
                continue

            name = read_file(
                f"/proc/{pid}/comm"
            ).strip()

            if name == process_name:
                return True

    except OSError:
        pass

    return False


# ============================================================
# N8N STATUS
# ============================================================

def get_n8n_status():

    try:

        with socket.create_connection(
            ("127.0.0.1", 5678),
            timeout=0.15
        ):
            return True

    except (OSError, socket.timeout):
        return False


# ============================================================
# PM2 SERVICES
# ============================================================

def get_pm2_services():
    global _pm2_cache_data, _pm2_cache_time

    now = time.monotonic()

    if (
        _pm2_cache_data is not None
        and (now - _pm2_cache_time) < PM2_CACHE_TTL
    ):
        return _pm2_cache_data

    with _pm2_cache_lock:

        now = time.monotonic()

        if (
            _pm2_cache_data is not None
            and (now - _pm2_cache_time) < PM2_CACHE_TTL
        ):
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
# BATTERY
# ============================================================

def get_battery():

    base = (
        "/sys/class/power_supply/battery"
    )

    def read(name):
        return read_file(
            f"{base}/{name}"
        ).strip()

    capacity = read("capacity")
    status = read("status")
    health = read("health")
    voltage = read("voltage_now")
    current = read("current_now")
    temperature = read("temp")
    technology = read("technology")

    try:
        capacity_value = (
            int(capacity)
            if capacity
            else None
        )
    except ValueError:
        capacity_value = None

    try:
        voltage_value = (
            round(
                int(voltage) / 1000000,
                3
            )
            if voltage
            else None
        )
    except ValueError:
        voltage_value = None

    try:
        current_value = (
            int(current)
            if current
            else None
        )
    except ValueError:
        current_value = None

    try:
        temperature_value = (
            round(
                int(temperature) / 10,
                1
            )
            if temperature
            else None
        )
    except ValueError:
        temperature_value = None

    return {
        "capacity": capacity_value,
        "status": status or "Unknown",
        "health": health or "Unknown",
        "voltage": voltage_value,
        "current": current_value,
        "temperature": temperature_value,
        "technology": technology or "Unknown",
    }


# ============================================================
# ON-DEMAND INTERNET SPEED TEST
# ============================================================

def run_speed_test():

    result = {
        "success": False,
        "download": None,
        "upload": None,
        "latency": None,
        "error": None,
    }

    try:

        # ----------------------------------------------------
        # Latency
        # ----------------------------------------------------

        latency_start = time.monotonic()

        with socket.create_connection(
            ("speed.cloudflare.com", 443),
            timeout=5
        ):
            pass

        latency = (
            time.monotonic() -
            latency_start
        ) * 1000

        result["latency"] = round(
            latency,
            1
        )

        # ----------------------------------------------------
        # Download
        #
        # 10 MB test.
        # Only runs when user presses the button.
        # ----------------------------------------------------

        download_url = (
            "https://speed.cloudflare.com/"
            "__down?bytes=10000000"
        )

        request = urllib.request.Request(
            download_url,
            headers={
                "User-Agent":
                    "A7-Server-Monitor/1.0"
            }
        )

        download_start = time.monotonic()

        total_bytes = 0

        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            while True:

                chunk = response.read(
                    256 * 1024
                )

                if not chunk:
                    break

                total_bytes += len(chunk)

        download_elapsed = (
            time.monotonic() -
            download_start
        )

        if download_elapsed <= 0:
            raise RuntimeError(
                "Invalid download timing"
            )

        download_mbps = (
            total_bytes * 8 /
            download_elapsed /
            1_000_000
        )

        result["download"] = round(
            download_mbps,
            2
        )

        # ----------------------------------------------------
        # Upload
        #
        # 5 MB test.
        # Only runs when user presses the button.
        # ----------------------------------------------------

        upload_size = 5 * 1024 * 1024

        upload_data = b"0" * upload_size

        upload_request = urllib.request.Request(
            "https://speed.cloudflare.com/__up",
            data=upload_data,
            method="POST",
            headers={
                "User-Agent":
                    "A7-Server-Monitor/1.0",
                "Content-Type":
                    "application/octet-stream",
                "Content-Length":
                    str(upload_size),
            }
        )

        upload_start = time.monotonic()

        with urllib.request.urlopen(
            upload_request,
            timeout=15
        ) as response:

            # Consume response.
            response.read()

        upload_elapsed = (
            time.monotonic() -
            upload_start
        )

        if upload_elapsed <= 0:
            raise RuntimeError(
                "Invalid upload timing"
            )

        upload_mbps = (
            upload_size * 8 /
            upload_elapsed /
            1_000_000
        )

        result["upload"] = round(
            upload_mbps,
            2
        )

        result["success"] = True

    except Exception as e:

        result["error"] = str(e)

    return result


# ============================================================
# ON-DEMAND LLM USAGE (ANTIGRAVITY CLI)
# ============================================================

_llm_lock = threading.Lock()

def get_agy_executable():
    path = shutil.which("agy")
    if path:
        return path
    for candidate in [
        "/home/server/.local/bin/agy",
        os.path.expanduser("~/.local/bin/agy"),
        "/usr/local/bin/agy",
    ]:
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "agy"

def run_llm_usage():
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
        user_home = os.path.expanduser("~server") if os.path.exists("/home/server") else os.path.expanduser("~")
        local_bin = os.path.join(user_home, ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        if "HOME" not in env:
            env["HOME"] = user_home

        proc = subprocess.run(
            [agy_bin, "-p", "/usage", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env
        )

        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or f"AGY process exited with code {proc.returncode}"
            result["error"] = err_msg
            return result

        raw_data = json.loads(proc.stdout)
        groups_data = raw_data.get("command", {}).get("data", {}).get("groups", [])
        
        parsed_groups = []
        for g in groups_data:
            group_name = g.get("name", "Unknown")
            buckets = []
            for b in g.get("buckets", []):
                rem_frac = b.get("remaining_fraction", 0.0)
                try:
                    pct = round(float(rem_frac) * 100, 1)
                except (ValueError, TypeError):
                    pct = 0.0
                buckets.append({
                    "id": b.get("id", ""),
                    "name": b.get("name", "Limit"),
                    "window": b.get("window", ""),
                    "percentage": pct,
                    "reset_time": b.get("reset_time", ""),
                })
            parsed_groups.append({
                "name": group_name,
                "buckets": buckets
            })

        result["groups"] = parsed_groups
        result["success"] = True

    except subprocess.TimeoutExpired:
        result["error"] = "AGY usage command timed out (30s limit)"
    except json.JSONDecodeError:
        result["error"] = "Failed to parse AGY usage output as JSON"
    except Exception as e:
        result["error"] = str(e)
    finally:
        _llm_lock.release()

    return result


# ============================================================
# COMPLETE SERVER STATUS
# ============================================================

def get_status():

    memory = get_memory()
    storage = get_storage()

    return {

        "hostname":
            get_hostname(),

        "architecture":
            os.uname().machine,

        "cpu":
            get_cpu_usage(),

        "cpu_count":
            get_cpu_count(),

        "load":
            get_load(),

        "memory":
            memory,

        "storage":
            storage,

        "uptime":
            get_uptime(),

        "network":
            get_network(),

        "temperature":
            get_temperature(),

        "battery":
            get_battery(),

        "services": {

            "sshd":
                get_process_status(
                    "sshd"
                ),

            "pm2":
                get_pm2_services(),
        },

        "timestamp":
            time.time(),
    }


# ============================================================
# HTML
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>A7 Server Monitor</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    background: #0b0f14;

    color: #e6edf3;

    font-family:
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

.container {

    width: min(1100px, 94%);

    margin: 30px auto;
}

.header {

    display: flex;

    justify-content: space-between;

    align-items: center;

    margin-bottom: 24px;
}

.title {

    font-size: 26px;

    font-weight: 700;
}

.subtitle {

    color: #8b949e;

    font-size: 14px;

    margin-top: 4px;
}

.status {

    display: flex;

    align-items: center;

    gap: 8px;

    color: #3fb950;

    font-weight: 600;
}

.dot {

    width: 10px;

    height: 10px;

    background: #3fb950;

    border-radius: 50%;

    box-shadow:
        0 0 10px #3fb950;
}

.grid {

    display: grid;

    grid-template-columns:
        repeat(3, 1fr);

    gap: 14px;
}

.card {

    background: #111820;

    border:
        1px solid #202938;

    border-radius: 12px;

    padding: 18px;
}

.card-title {

    color: #8b949e;

    font-size: 13px;

    margin-bottom: 12px;
}

.big {

    font-size: 30px;

    font-weight: 700;
}

.unit {

    font-size: 14px;

    color: #8b949e;
}

.progress {

    height: 6px;

    background: #202938;

    border-radius: 10px;

    overflow: hidden;

    margin-top: 14px;
}

.progress-bar {

    height: 100%;

    width: 0%;

    background: #58a6ff;

    transition:
        width .2s ease;
}

.rows {

    display: grid;

    gap: 10px;
}

.row {

    display: flex;

    justify-content:
        space-between;

    gap: 20px;
}

.label {

    color: #8b949e;
}

.value {

    font-weight: 600;

    text-align: right;
}

.services {

    display: grid;

    gap: 10px;

    max-height: 140px;

    overflow-y: auto;

    scrollbar-width: none;

    -ms-overflow-style: none;
}

.services::-webkit-scrollbar {
    display: none;
}

.pm2-services {

    display: grid;

    gap: 10px;
}

.service {

    display: flex;

    justify-content:
        space-between;

    align-items: center;
}

.service-info {

    display: flex;

    flex-direction: column;

    gap: 2px;
}

.service-sub {

    font-size: 11px;

    color: #8b949e;
}

.service-empty {

    font-size: 13px;

    color: #8b949e;

    font-style: italic;
}

.online {

    color: #3fb950;
}

.offline {

    color: #f85149;
}

.speed-test {

    display: flex;

    flex-direction: column;

    gap: 12px;
}

.speed-button {

    border: none;

    border-radius: 8px;

    padding: 10px 14px;

    background: #238636;

    color: white;

    font-size: 14px;

    font-weight: 600;

    cursor: pointer;
}

.speed-button:hover {

    background: #2ea043;
}

.speed-button:disabled {

    background: #30363d;

    color: #8b949e;

    cursor: wait;
}

.speed-results {

    display: grid;

    gap: 8px;
}

.speed-result {

    display: flex;

    justify-content:
        space-between;

    gap: 10px;
}

.speed-label {

    color: #8b949e;
}

.speed-value {

    font-weight: 600;
}

.speed-note {

    color: #6e7681;

    font-size: 11px;

    line-height: 1.4;
}

.llm-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-bottom: 8px;
    border-bottom: 1px solid #21262d;
}

.llm-group:last-child {
    border-bottom: none;
    padding-bottom: 0;
}

.llm-group-title {
    font-size: 12px;
    font-weight: 600;
    color: #58a6ff;
    display: flex;
    justify-content: space-between;
}

.llm-bucket {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.llm-bucket-header {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
}

.llm-bucket-name {
    color: #8b949e;
}

.llm-bucket-val {
    font-weight: 600;
}

.llm-bucket-reset {
    font-size: 10px;
    color: #6e7681;
}

.footer {

    margin-top: 18px;

    color: #6e7681;

    font-size: 12px;

    text-align: center;
}

@media (max-width: 750px) {

    .grid {

        grid-template-columns:
            1fr;
    }

    .header {

        align-items:
            flex-start;
    }

    .big {

        font-size: 26px;
    }
}

</style>

</head>


<body>

<div class="container">


    <!-- HEADER -->

    <div class="header">

        <div>

            <div
                class="title"
                id="hostname"
            >
                A7 Server
            </div>

            <div class="subtitle">
                ARM64 Linux Server
            </div>

        </div>


        <div class="status">

            <span class="dot"></span>

            ONLINE

        </div>

    </div>


    <div class="grid">


        <!-- CPU -->

        <div class="card">

            <div class="card-title">
                CPU USAGE
            </div>

            <div>

                <span
                    class="big"
                    id="cpu"
                >
                    --
                </span>

                <span class="unit">
                    %
                </span>

            </div>

            <div class="progress">

                <div
                    class="progress-bar"
                    id="cpuBar"
                ></div>

            </div>

        </div>


        <!-- MEMORY -->

        <div class="card">

            <div class="card-title">
                MEMORY
            </div>

            <div>

                <span
                    class="big"
                    id="memory"
                >
                    --
                </span>

            </div>

            <div class="progress">

                <div
                    class="progress-bar"
                    id="memoryBar"
                ></div>

            </div>

            <div
                class="subtitle"
                id="memoryDetails"
            >
                --
            </div>

        </div>


        <!-- STORAGE -->

        <div class="card">

            <div class="card-title">
                STORAGE
            </div>

            <div>

                <span
                    class="big"
                    id="storage"
                >
                    --
                </span>

            </div>

            <div class="progress">

                <div
                    class="progress-bar"
                    id="storageBar"
                ></div>

            </div>

            <div
                class="subtitle"
                id="storageDetails"
            >
                --
            </div>

        </div>


        <!-- LOAD -->

        <div class="card">

            <div class="card-title">
                LOAD AVERAGE
            </div>

            <div
                class="big"
                id="load"
            >
                --
            </div>

            <div class="subtitle">
                1 min &nbsp; / &nbsp;
                5 min &nbsp; / &nbsp;
                15 min
            </div>

        </div>


        <!-- UPTIME -->

        <div class="card">

            <div class="card-title">
                UPTIME
            </div>

            <div
                class="big"
                id="uptime"
            >
                --
            </div>

        </div>


        <!-- SYSTEM -->

        <div class="card">

            <div class="card-title">
                SYSTEM
            </div>

            <div class="rows">

                <div class="row">

                    <span class="label">
                        Architecture
                    </span>

                    <span
                        class="value"
                        id="architecture"
                    >
                        --
                    </span>

                </div>


                <div class="row">

                    <span class="label">
                        CPU Cores
                    </span>

                    <span
                        class="value"
                        id="cores"
                    >
                        --
                    </span>

                </div>


                <div class="row">

                    <span class="label">
                        Temperature
                    </span>

                    <span
                        class="value"
                        id="temperature"
                    >
                        --
                    </span>

                </div>

            </div>

        </div>


        <!-- NETWORK -->

        <div class="card">

            <div class="card-title">
                NETWORK
            </div>

            <div class="rows">

                <div class="row">

                    <span class="label">
                        Download
                    </span>

                    <span
                        class="value"
                        id="rx"
                    >
                        --
                    </span>

                </div>


                <div class="row">

                    <span class="label">
                        Upload
                    </span>

                    <span
                        class="value"
                        id="tx"
                    >
                        --
                    </span>

                </div>

            </div>

        </div>


        <!-- SWAP -->

        <div class="card">

            <div class="card-title">
                SWAP / ZRAM
            </div>

            <div>

                <span
                    class="big"
                    id="swap"
                >
                    --
                </span>

            </div>

            <div
                class="subtitle"
                id="swapDetails"
            >
                --
            </div>

        </div>


        <!-- BATTERY -->

        <div class="card">

            <div class="card-title">
                BATTERY
            </div>

            <div>

                <span
                    class="big"
                    id="battery"
                >
                    --
                </span>

            </div>

            <div class="progress">

                <div
                    class="progress-bar"
                    id="batteryBar"
                ></div>

            </div>

            <div
                class="subtitle"
                id="batteryDetails"
            >
                --
            </div>

        </div>


        <!-- SERVICES -->

        <div class="card">

            <div class="card-title">
                SERVICES
            </div>

            <div class="services">

                <div class="service">

                    <span>
                        SSH
                    </span>

                    <span id="sshd">
                        --
                    </span>

                </div>

                <div id="pm2Services" class="pm2-services"></div>

            </div>

        </div>


        <!-- INTERNET SPEED TEST -->

        <div class="card">

            <div class="card-title">
                INTERNET SPEED TEST
            </div>

            <div class="speed-test">

                <button
                    class="speed-button"
                    id="speedButton"
                    onclick="runSpeedTest()"
                >
                    Run Speed Test
                </button>


                <div
                    class="speed-results"
                    id="speedResults"
                >

                    <div class="speed-result">

                        <span class="speed-label">
                            Download
                        </span>

                        <span
                            class="speed-value"
                            id="speedDownload"
                        >
                            --
                        </span>

                    </div>


                    <div class="speed-result">

                        <span class="speed-label">
                            Upload
                        </span>

                        <span
                            class="speed-value"
                            id="speedUpload"
                        >
                            --
                        </span>

                    </div>


                    <div class="speed-result">

                        <span class="speed-label">
                            Latency
                        </span>

                        <span
                            class="speed-value"
                            id="speedLatency"
                        >
                            --
                        </span>

                    </div>

                </div>


                <div
                    class="speed-note"
                    id="speedNote"
                >
                    On-demand test. Uses temporary
                    internet bandwidth only when run.
                </div>

            </div>

        </div>


        <!-- LLM USAGE -->

        <div class="card">

            <div class="card-title">
                LLM Quota &amp; Usage
            </div>

            <div class="speed-test">

                <button
                    class="speed-button"
                    id="llmusage-button"
                    type="button"
                    onclick="runLlmUsage()"
                >
                    Check LLM Quota
                </button>

                <div
                    class="speed-results"
                    id="llm-results"
                >
                    <div class="speed-result">
                        <span class="speed-label">Gemini &amp; 3P Models</span>
                        <span class="speed-value">--</span>
                    </div>
                </div>

                <div
                    class="speed-note"
                    id="llmusage-note"
                >
                    On-demand quota check via AGY
                </div>

            </div>

        </div>


    </div>


    <div class="footer">

        Updates every second · Local network only

    </div>


</div>


<script>


// ============================================================
// FORMATTING
// ============================================================

function formatBytes(bytes) {

    if (bytes < 1024)
        return bytes.toFixed(0) + " B";

    if (bytes < 1024 ** 2)
        return (
            bytes / 1024
        ).toFixed(1) + " KB";

    if (bytes < 1024 ** 3)
        return (
            bytes / 1024 ** 2
        ).toFixed(1) + " MB";

    return (
        bytes / 1024 ** 3
    ).toFixed(2) + " GB";
}


function formatRate(bytes) {

    if (bytes < 1024)
        return bytes.toFixed(0) + " B/s";

    if (bytes < 1024 ** 2)
        return (
            bytes / 1024
        ).toFixed(1) + " KB/s";

    return (
        bytes / 1024 ** 2
    ).toFixed(2) + " MB/s";
}


function formatUptime(seconds) {

    seconds = Math.floor(seconds);

    const days =
        Math.floor(seconds / 86400);

    seconds %= 86400;

    const hours =
        Math.floor(seconds / 3600);

    seconds %= 3600;

    const minutes =
        Math.floor(seconds / 60);

    if (days > 0)
        return (
            `${days}d ${hours}h ${minutes}m`
        );

    if (hours > 0)
        return (
            `${hours}h ${minutes}m`
        );

    return `${minutes}m`;
}


function escapeHtml(text) {
    if (text === null || text === undefined) return "";
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}


function serviceStatus(
    element,
    online
) {

    element.textContent =
        online
            ? "ONLINE"
            : "OFFLINE";

    element.className =
        online
            ? "online"
            : "offline";
}


// ============================================================
// NORMAL DASHBOARD UPDATE
// ============================================================

let isUpdating = false;

async function update() {

    if (isUpdating || document.hidden) {
        return;
    }

    isUpdating = true;

    try {

        const response =
            await fetch(
                "/api/status",
                {
                    cache: "no-store"
                }
            );

        if (!response.ok)
            return;

        const data =
            await response.json();


        // Header

        document.getElementById(
            "hostname"
        ).textContent =
            data.hostname;


        // CPU

        document.getElementById(
            "cpu"
        ).textContent =
            data.cpu.toFixed(1);

        document.getElementById(
            "cpuBar"
        ).style.width =
            Math.min(
                data.cpu,
                100
            ) + "%";


        // Memory

        const memoryPercent =
            data.memory.total > 0
                ? (
                    data.memory.used /
                    data.memory.total
                ) * 100
                : 0;

        document.getElementById(
            "memory"
        ).textContent =
            memoryPercent.toFixed(1)
            + "%";

        document.getElementById(
            "memoryBar"
        ).style.width =
            Math.min(
                memoryPercent,
                100
            ) + "%";

        document.getElementById(
            "memoryDetails"
        ).textContent =
            `${formatBytes(data.memory.used)} used / ` +
            `${formatBytes(data.memory.total)} total`;


        // Storage

        const storagePercent =
            data.storage.total > 0
                ? (
                    data.storage.used /
                    data.storage.total
                ) * 100
                : 0;

        document.getElementById(
            "storage"
        ).textContent =
            storagePercent.toFixed(1)
            + "%";

        document.getElementById(
            "storageBar"
        ).style.width =
            Math.min(
                storagePercent,
                100
            ) + "%";

        document.getElementById(
            "storageDetails"
        ).textContent =
            `${formatBytes(data.storage.free)} free`;


        // Load

        document.getElementById(
            "load"
        ).textContent =
            data.load
                .map(
                    x => x.toFixed(2)
                )
                .join(" / ");


        // Uptime

        document.getElementById(
            "uptime"
        ).textContent =
            formatUptime(
                data.uptime
            );


        // System

        document.getElementById(
            "architecture"
        ).textContent =
            data.architecture;

        document.getElementById(
            "cores"
        ).textContent =
            data.cpu_count;

        document.getElementById(
            "temperature"
        ).textContent =
            data.temperature === null
                ? "N/A"
                : data.temperature + " °C";


        // Network

        document.getElementById(
            "rx"
        ).textContent =
            formatRate(
                data.network.rx_rate
            );

        document.getElementById(
            "tx"
        ).textContent =
            formatRate(
                data.network.tx_rate
            );


        // Swap

        const swapTotal =
            data.memory.swap_total;

        const swapUsed =
            data.memory.swap_used;

        const swapPercent =
            swapTotal > 0
                ? (
                    swapUsed /
                    swapTotal
                ) * 100
                : 0;

        document.getElementById(
            "swap"
        ).textContent =
            swapPercent.toFixed(1)
            + "%";

        document.getElementById(
            "swapDetails"
        ).textContent =
            swapTotal > 0
                ? `${formatBytes(swapUsed)} used / ` +
                  `${formatBytes(swapTotal)} total`
                : "No swap";


        // Battery

        const battery =
            data.battery;

        document.getElementById(
            "battery"
        ).textContent =
            battery.capacity === null
                ? "N/A"
                : battery.capacity + "%";

        document.getElementById(
            "batteryBar"
        ).style.width =
            battery.capacity === null
                ? "0%"
                : battery.capacity + "%";

        document.getElementById(
            "batteryDetails"
        ).textContent =
            `${battery.status} · ` +
            `${battery.temperature ?? "N/A"} °C · ` +
            `${battery.voltage ?? "N/A"} V · ` +
            `${battery.health}`;


        // Services

        serviceStatus(
            document.getElementById(
                "sshd"
            ),
            data.services.sshd
        );

        const pm2Container = document.getElementById("pm2Services");

        if (pm2Container && data.services && data.services.pm2) {
            const pm2 = data.services.pm2;

            if (pm2.status === "unavailable") {
                pm2Container.innerHTML = '<div class="service-empty">PM2 unavailable</div>';
            } else if (pm2.status === "error") {
                pm2Container.innerHTML = `<div class="service-empty">PM2 error: ${escapeHtml(pm2.error || "Command error")}</div>`;
            } else if (!pm2.services || pm2.services.length === 0) {
                pm2Container.innerHTML = '<div class="service-empty">No PM2 services</div>';
            } else {
                pm2Container.innerHTML = pm2.services.map(svc => {
                    const isOnline = svc.status === "online";
                    const statusClass = isOnline ? "online" : "offline";
                    const statusText = (svc.status || "UNKNOWN").toUpperCase();

                    const memText = svc.memory ? (svc.memory / (1024 * 1024)).toFixed(0) + " MB" : "";
                    const cpuText = (svc.cpu !== undefined && svc.cpu !== null) ? svc.cpu + "%" : "";
                    const details = [memText, cpuText].filter(Boolean).join(" · ");
                    const subText = details ? `<span class="service-sub">${escapeHtml(details)}</span>` : "";

                    return `
                        <div class="service">
                            <div class="service-info">
                                <span>${escapeHtml(svc.name)}</span>
                                ${subText}
                            </div>
                            <span class="${statusClass}">${statusText}</span>
                        </div>
                    `;
                }).join("");
            }
        }

    }

    catch (error) {

        console.error(error);

    }

    finally {

        isUpdating = false;

    }

}


// ============================================================
// ON-DEMAND SPEED TEST
// ============================================================

async function runSpeedTest() {

    const button =
        document.getElementById(
            "speedButton"
        );

    const note =
        document.getElementById(
            "speedNote"
        );

    const download =
        document.getElementById(
            "speedDownload"
        );

    const upload =
        document.getElementById(
            "speedUpload"
        );

    const latency =
        document.getElementById(
            "speedLatency"
        );


    button.disabled = true;

    button.textContent =
        "Testing...";


    note.textContent =
        "Running internet speed test. " +
        "This temporarily uses bandwidth.";


    download.textContent =
        "Testing...";

    upload.textContent =
        "Waiting...";

    latency.textContent =
        "Testing...";


    try {

        const response =
            await fetch(
                "/api/speedtest",
                {
                    cache: "no-store"
                }
            );

        const data =
            await response.json();


        if (!data.success) {

            download.textContent =
                "Failed";

            upload.textContent =
                "Failed";

            latency.textContent =
                "Failed";

            note.textContent =
                data.error ||
                "Speed test failed.";

            return;
        }


        download.textContent =
            data.download +
            " Mbps";

        upload.textContent =
            data.upload +
            " Mbps";

        latency.textContent =
            data.latency +
            " ms";

        note.textContent =
            "Test completed.";

    }

    catch (error) {

        download.textContent =
            "Failed";

        upload.textContent =
            "Failed";

        latency.textContent =
            "Failed";

        note.textContent =
            "Could not run speed test.";

    }

    finally {

        button.disabled = false;

        button.textContent =
            "Run Speed Test";

    }

}


async function runLlmUsage() {

    const button =
        document.getElementById("llmusage-button");
    const resultsContainer =
        document.getElementById("llm-results");
    const note =
        document.getElementById("llmusage-note");

    button.disabled = true;
    button.textContent = "Checking Quotas...";
    note.textContent = "Querying Antigravity CLI usage quotas (takes ~15-20s)...";

    try {
        const response = await fetch("/api/llmusage");
        const data = await response.json();

        if (!data.success) {
            throw new Error(data.error || "Failed to fetch LLM usage");
        }

        if (!data.groups || data.groups.length === 0) {
            resultsContainer.innerHTML = `
                <div class="speed-result">
                    <span class="speed-label">No active quota groups found</span>
                </div>
            `;
        } else {
            resultsContainer.innerHTML = data.groups.map(group => {
                const bucketsHtml = (group.buckets || []).map(b => {
                    const resetTimeStr = b.reset_time ? new Date(b.reset_time).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', month: 'short', day: 'numeric'}) : '';
                    const colorClass = b.percentage < 20 ? 'offline' : (b.percentage < 50 ? 'warning' : 'online');
                    return `
                        <div class="llm-bucket">
                            <div class="llm-bucket-header">
                                <span class="llm-bucket-name">${b.name}</span>
                                <span class="llm-bucket-val ${colorClass}">${b.percentage}%</span>
                            </div>
                            ${resetTimeStr ? `<span class="llm-bucket-reset">Resets: ${resetTimeStr}</span>` : ''}
                        </div>
                    `;
                }).join('');

                return `
                    <div class="llm-group">
                        <div class="llm-group-title">${group.name}</div>
                        ${bucketsHtml}
                    </div>
                `;
            }).join('');
        }

        note.textContent = "Quota refreshed successfully.";
    }
    catch (error) {
        resultsContainer.innerHTML = `
            <div class="speed-result">
                <span class="speed-label offline">Error: ${error.message}</span>
            </div>
        `;
        note.textContent = "Could not fetch LLM usage quota.";
    }
    finally {
        button.disabled = false;
        button.textContent = "Check LLM Quota";
    }
}


// ============================================================
// START DASHBOARD
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
# HTTP HANDLER
# ============================================================

class Handler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):

        # Disable access logging.
        pass


    def send_data(
        self,
        content,
        content_type
    ):

        body = content.encode()

        self.send_response(200)

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.end_headers()

        self.wfile.write(body)


    def do_GET(self):

        # Dashboard

        if self.path == "/":

            self.send_data(
                HTML,
                "text/html; charset=utf-8"
            )

            return


        # Normal lightweight status API

        if self.path == "/api/status":

            self.send_data(
                json.dumps(
                    get_status()
                ),
                "application/json"
            )

            return


        # On-demand speed test

        if self.path == "/api/speedtest":

            result = run_speed_test()

            self.send_data(
                json.dumps(result),
                "application/json"
            )

            return


        # On-demand LLM usage

        if self.path == "/api/llmusage":

            result = run_llm_usage()

            self.send_data(
                json.dumps(result),
                "application/json"
            )

            return


        self.send_response(404)

        self.end_headers()


# ============================================================
# MAIN
# ============================================================

def main():

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler
    )

    print(
        "Server monitor listening on "
        f"http://0.0.0.0:{PORT}"
    )

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        pass

    finally:

        server.server_close()


if __name__ == "__main__":
    main()
