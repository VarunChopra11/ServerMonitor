# ServerMonitor &bull; Universal Linux Server Monitoring

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Architecture: ARM64 | x86_64 | Bare--Metal | VM | Container](https://img.shields.io/badge/arch-ARM64%20%7C%20x86__64%20%7C%20cgroups-success.svg)](https://github.com/VarunChopra11/ServerMonitor)
[![Dependencies: Zero](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](https://github.com/VarunChopra11/ServerMonitor)

A production-ready, ultra-lightweight, universal Linux server monitoring application with an on-demand live web dashboard. Zero external dependencies, pure Python standard library, dynamic hardware detection, and container-aware telemetry.

---

## Key Highlights

- **On-Demand Live Monitoring**: Sampling begins **only when an authenticated dashboard session is actively connected**. When all sessions close or become inactive, the background collector pauses automatically—dropping daemon CPU usage to **0.0%**.
- **Universal Hardware & Cloud Support**: Dynamically detects CPU architecture (`x86_64`, `aarch64`, `riscv64`), CPU model, cores, per-core utilization, and **CPU steal time** (essential for cloud VMs).
- **Container & Cgroup Aware**: Detects cgroup v1 and cgroup v2 memory limits (Docker, Podman, LXC, Kubernetes) and visualizes container resource constraints alongside host RAM.
- **Dynamic Mounts & Storage**: Discovers all mounted physical block devices and filesystems (`ext4`, `xfs`, `btrfs`, `zfs`, `f2fs`, `nfs`, `cifs`) instead of hardcoding root.
- **Adaptive Sensor UI**: Automatically displays hardware thermals (`/sys/class/thermal`, `/sys/class/hwmon`) and battery status (`/sys/class/power_supply`) when present; **gracefully collapses** those widgets on cloud VMs and hypervisors without clutter.
- **Multi-Service & Container Health**: Configurable monitoring for systemd units, background processes, HTTP/TCP endpoints, and Docker/Podman containers.
- **Security by Default**: Binds to `127.0.0.1` by default, supports constant-time token authentication (`hmac.compare_digest`), injects strict CSP/security headers, enforces rate limits, validates reverse-proxy client IPs, and applies speed test mutex locks with mandatory cooldowns.
- **Microscopic Resource Footprint**: Zero third-party dependencies (`pip install` not required). Consumes only **~28 MB RAM** and negligible CPU overhead.
- **Interactive CLI Setup Wizard**: Includes `setup`, `run`, `status`, `tunnel`, `benchmark`, `systemd`, `pm2`, and `uninstall` commands.

---

## Architecture Overview

```
+-------------------------------------------------------------------------+
|                              ServerMonitor                              |
|                                                                         |
|  +-------------------------------------------------------------------+  |
|  |                 On-Demand Live Metrics Collector                  |  |
|  |   [Active Sessions > 0]  ---> Sample at 2s interval               |  |
|  |   [Active Sessions == 0] ---> Pause collection thread (0.0% CPU)  |  |
|  +-------------------------------------------------------------------+  |
|                                  |                                      |
|    +--------------------+--------+--------+--------------------+        |
|    |                    |                 |                    |        |
|    v                    v                 v                    v        |
| Hardware Engine     Memory / Cgroup   Storage Mounts     Services / Docker  |
|  - CPU Model/Arch    - Host RAM        - Real Block Fs    - SSH status       |
|  - Per-Core Usage    - cgroup v1/v2    - Ext4/XFS/Btrfs   - PM2 jlist        |
|  - CPU Steal %       - Swap usage      - ZFS / NFS / CIFS - Systemd units    |
|  - Thermals / SoC                      - Dynamic statvfs  - Docker / Podman  |
|                                                                         |
|  +-------------------------------------------------------------------+  |
|  |           Thread-Safe HTTP Server & Security Middleware           |  |
|  |   - Constant-Time Token Auth (hmac.compare_digest)                |  |
|  |   - Strict Security Headers (CSP, nosniff, DENY, no-referrer)    |  |
|  |   - Sliding-Window IP Rate Limiter & Trusted Reverse Proxy Checks  |  |
|  |   - Speedtest Mutex Cooldown & On-Demand Lifecycle Manager        |  |
|  +-------------------------------------------------------------------+  |
+-------------------------------------------------------------------------+
                                   ^
                                   | (On-Demand HTTP Polling / REST JSON API)
                                   v
             [ Browser Dashboard / SSH Tunnel / Tailscale ]
```

---

## Requirements

- **Operating System**: Linux (Kernel 3.10+ / 4.x / 5.x / 6.x)
- **Architecture**: ARM64, x86_64, ARMv7, RISC-V, or MIPS
- **Python**: Python 3.8 or newer (`/usr/bin/python3`)
- **Dependencies**: **Zero** (Standard library only; no Flask, Node.js, Redis, or Docker daemon required)

---

## Quick Installation & Interactive Setup

### 1. Clone & Setup

```bash
git clone https://github.com/VarunChopra11/ServerMonitor.git
cd ServerMonitor
./install.sh
```

Or run the interactive setup wizard directly:

```bash
chmod +x servermonitor server.py
./servermonitor setup
```

The interactive wizard guides you through:
1. **Network Binding**: Localhost `127.0.0.1` (recommended) or private LAN IP.
2. **Port**: Default `8080` (customizable).
3. **Security Token**: Generate a secure 32-character random access token or set custom credentials.
4. **Sampling & Conservation**: Configure the 2-second live sampling cadence and inactivity pause timeout.
5. **Deployment**: Standalone CLI, PM2 process, or systemd daemon.
6. **Remote Access**: Generates tailored SSH port forwarding commands and guides Tailscale, Caddy, and Nginx configurations.

---

## CLI Reference

`servermonitor` provides an ergonomic management interface:

```bash
# Launch interactive configuration wizard
./servermonitor setup

# Start the monitoring service in foreground
./servermonitor start

# Inspect running process and port status
./servermonitor status

# Generate ready-to-copy SSH port forwarding command
./servermonitor tunnel

# Run memory RSS and sampling performance benchmark
./servermonitor benchmark

# Generate hardened systemd service file
./servermonitor systemd

# Generate PM2 ecosystem configuration
./servermonitor pm2

# Safely uninstall ServerMonitor
./servermonitor uninstall
```

You can also pass arguments directly to `server.py`:

```bash
python3 server.py --host 127.0.0.1 --port 8085 --token "my-secret-token"
```

---

## Configuration Reference (`config.json`)

Configuration is saved with strict `0600` permissions:

```json
{
  "host": "127.0.0.1",
  "port": 8080,
  "auth_token": "your-secret-token-here",
  "sample_interval": 2.0,
  "inactivity_timeout": 15.0,
  "history_points": 60,
  "speedtest_enabled": true,
  "speedtest_cooldown": 60.0,
  "modules": {
    "cpu": true,
    "memory": true,
    "storage": true,
    "network": true,
    "thermals": true,
    "battery": true,
    "services": true,
    "pm2": true,
    "systemd": true,
    "docker": false,
    "antigravity": true
  },
  "monitored_systemd_units": ["ssh", "sshd", "nginx", "caddy", "docker"],
  "monitored_processes": ["sshd"],
  "trusted_proxies": ["127.0.0.1", "::1"]
}
```

---

## Remote Access & Production Deployment

Never expose raw monitoring ports directly to the public Internet without an encrypted tunnel or reverse proxy.

### 1. Secure SSH Port Forwarding (Recommended)

Keep ServerMonitor bound to `127.0.0.1:8080` on the server and forward the port over SSH:

```bash
ssh -N -L 8080:127.0.0.1:8080 user@your-server-ip
```

Open `http://127.0.0.1:8080` in your local computer's browser. Traffic is end-to-end encrypted with zero exposed server ports.

### 2. Tailscale Private Mesh Network

If using [Tailscale](https://tailscale.com):
1. Install Tailscale: `curl -fsSL https://tailscale.com/install.sh | sh`
2. Connect the node: `tailscale up`
3. In `config.json`, set your authentication token and add your Tailscale IP/domain to `allowed_hosts`:
   ```json
   {
     "auth_token": "your-secret-token-min-24-chars",
     "allowed_hosts": ["127.0.0.1", "localhost", "100.*", "*.ts.net"]
   }
   ```
4. Access via your private Tailnet URL: `http://100.x.y.z:8080`

### 3. HTTPS via Caddy (Automatic SSL)

> [!NOTE]
> When accessing ServerMonitor behind a reverse proxy, an `auth_token` is **strictly required**. Add your domain name to `allowed_hosts` in `config.json` so that the Host-header validation accepts proxied traffic.

In `config.json`:
```json
{
  "auth_token": "your-secret-token-min-24-chars",
  "allowed_hosts": ["127.0.0.1", "localhost", "monitor.yourdomain.com"]
}
```

Add to `/etc/caddy/Caddyfile`:

```caddy
monitor.yourdomain.com {
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Forwarded-Proto {scheme}
    }
}
```

Reload Caddy: `sudo systemctl reload caddy`

### 4. HTTPS via Nginx

Add to `/etc/nginx/sites-available/servermonitor`:

```nginx
server {
    server_name monitor.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # HTTP connection keepalive & buffer options
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 60;
    }

    listen 443 ssl;
    # SSL certificate configuration...
}
```

---

## Cloud VM Deployment (GCP, AWS, Oracle Cloud)

### Google Cloud Platform (GCP Compute Engine)
1. Keep `host` bound to `127.0.0.1`.
2. Access securely using Google Cloud Identity-Aware Proxy (IAP) or SSH forwarding:
   ```bash
   gcloud compute ssh instance-name --zone=us-central1-a -- -L 8080:127.0.0.1:8080
   ```
3. CPU steal time monitoring automatically reflects noisy-neighbor hypervisor throttling.

### Amazon Web Services (AWS EC2)
1. Deploy on Amazon Linux 2023, Ubuntu, or Debian.
2. Use AWS Systems Manager (SSM) Session Manager for port forwarding without open Security Group ports:
   ```bash
   aws ssm start-session --target i-0123456789abcdef0 \
     --document-name AWS-StartPortForwardingSession \
     --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'
   ```

### Oracle Cloud Infrastructure (OCI Compute)
1. Supported on both x86_64 and Ampere Altra (ARM64) Always-Free instances.
2. Notice: OCI enforces host iptables rules by default. Maintain `127.0.0.1` binding and use SSH tunneling to connect without opening public ingress ports.

---

## Service Management (PM2 & Systemd)

### PM2 Deployment
PM2 is supported as a first-class process manager:

```bash
# Generate ecosystem file
./servermonitor pm2 > ecosystem.config.js

# Start service with PM2
pm2 start ecosystem.config.js

# Save process list
pm2 save
```

### Systemd Deployment
To install as a persistent, sandboxed systemd service:

```bash
# Generate and install service unit
sudo ./servermonitor systemd > /etc/systemd/system/servermonitor.service

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable --now servermonitor.service

# Check status
sudo systemctl status servermonitor.service
```

---

## Security Architecture & Deployment Guarantees

> [!IMPORTANT]
> **Production Remote Access Requirement**: ServerMonitor must bind to loopback `127.0.0.1` (the default) and be accessed via SSH port forwarding, an encrypted mesh VPN (such as Tailscale or WireGuard), or behind a hardened TLS reverse proxy (Caddy or Nginx with HTTPS).
> Binding to non-loopback addresses (`0.0.0.0` or public IPs) without an authentication token is **strictly rejected by default**. Direct public exposure over unencrypted HTTP without TLS is strongly discouraged.

- **Strict Read-Only Monitoring**: The application performs **zero write operations** to monitored system files, services, firewall rules, or kernel knobs.
- **Loopback Enforcement & Token Guard**: Rejects binding to external network interfaces without a cryptographically secure token.
- **Constant-Time Verification**: Auth tokens are verified using `hmac.compare_digest` to prevent timing attacks.
- **Strict Header Authentication**: Rejects token exposure in query parameters; requires `Authorization: Bearer <token>` or `X-Monitor-Token`.
- **Systemd Process Isolation**: Generated systemd units enforce `ProtectSystem=strict`, `ProtectHome=read-only`, and `ReadOnlyPaths={working_dir}` with mutable state isolated to `StateDirectory=servermonitor` and configuration in `ConfigurationDirectory=servermonitor`.
- **Installation & Uninstallation Safety**: Installer verifies systemd unit ownership before modifying service files; uninstaller verifies `pm_exec_path` and `ExecStart` matching this exact directory, preventing accidental deletion of foreign processes.
- **Trusted Reverse-Proxy Validation**: Only trusts forwarded client IP headers (`X-Forwarded-For`, `X-Real-IP`) when the immediate peer belongs to explicitly configured `trusted_proxies`.
- **Security Headers**: Injects `Content-Security-Policy: default-src 'self' 'unsafe-inline' data:`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`.
- **DoS & Resource Exhaustion Defense**:
  - Speed test is protected by a mutual exclusion lock (`_speedtest_lock`) and mandatory 60-second cooldown timer. It is fully disabled at the endpoint when `speedtest_enabled: false`.
  - Sliding-window in-memory IP rate limiter throttles rapid API abuse and periodically purges expired records to prevent unbounded memory growth.
- **Data Sanitization**: Sensitive environment variables and command line credentials are excluded from API payloads, and frontend outputs are sanitized via `escapeHtml()`.

---

## Resource Footprint & Benchmarking

Run the built-in benchmark on your server:

```bash
./servermonitor benchmark
```

Typical benchmark results on ARM64 and x86_64 Linux:

| Metric | Measured Value | Notes |
|:---|:---|:---|
| **Memory (RSS) - Idle** | **~28.9 MB** | Collector paused, HTTP socket listening |
| **Memory (RSS) - Active** | **~29.2 MB** | 2-second background telemetry sampling |
| **CPU Usage - Idle** | **0.0%** | Event-driven sleep when no browser tab open |
| **CPU Usage - Active** | **< 0.2%** | Delta calculations in pure Python standard library |
| **Average Query Latency (Cached)** | **~0.6 ms** | Served from atomic in-memory snapshot (~60 ms for full cold hardware sweep) |
| **External Dependencies** | **0** | No pip, npm, wheels, or compiled binary dependencies |

---

## Running the Unit & Integration Test Suite

A comprehensive test suite of 105+ tests verifies hardware discovery, cgroups, collector lifecycle, security, CLI, real script execution, and live HTTP operations:

```bash
python3 -m unittest discover -v
```

All tests run in under 4 seconds with zero external dependencies.

---

## Safe Uninstallation

To safely stop ServerMonitor and clean up its files without touching other projects:

```bash
./servermonitor uninstall
```

The uninstaller:
1. Stops and removes the systemd service (if registered).
2. Stops and removes the PM2 process (if registered) and saves process list via `pm2 save`.
3. Safely prompts before removing system state/config directories (`/var/lib/servermonitor`, `/etc/servermonitor`) and local configuration files.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
