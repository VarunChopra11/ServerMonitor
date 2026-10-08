# Lightweight Linux Server Monitor

A lightweight, dependency-free web dashboard for monitoring a Linux server.

The monitor runs as a single Python application and exposes a small local web interface showing real-time system information such as CPU, memory, storage, load average, uptime, network activity, temperature, battery status, and selected service states.

It also includes an optional on-demand Internet Speed Test. The speed test is NOT performed automatically. It only runs when the user clicks the "Run Speed Test" button.

## Features

### System Monitoring
- CPU usage
- CPU core count
- Load average (1, 5, and 15 minutes)
- Memory usage
- Swap / ZRAM usage
- Storage usage and free storage
- System uptime
- CPU/system temperature
- System architecture

### Network Monitoring
- Current download traffic
- Current upload traffic
- Traffic is calculated from Linux `/proc/net/dev`
- No external network test is performed during normal monitoring

### Battery Monitoring
When supported by the system:
- Battery percentage
- Battery status
- Battery health
- Battery temperature
- Battery voltage
- Battery technology

### Service Monitoring
The example implementation checks:
- n8n
- Cloudflare Tunnel (`cloudflared`)
- SSH (`sshd`)

n8n is checked through its local HTTP port rather than repeatedly invoking PM2, keeping the monitor lightweight.

### On-Demand Internet Speed Test
The dashboard has a separate Internet Speed Test section measuring:
- Download speed
- Upload speed
- Latency

The test is completely manual and does not run during normal dashboard updates.

## Requirements

- Linux
- Python 3
- Network access for the web dashboard

The application uses only the Python standard library. No third-party Python packages are required.

There is no requirement for Flask, FastAPI, Django, Node.js, npm, a database, Redis, Prometheus, or Grafana.

## Installation

Clone the repository:

```bash
git clone https://github.com/YOUR_USERNAME/server-monitor.git
cd server-monitor
```

Make the script executable:

```bash
chmod +x server.py
```

Run it:

```bash
python3 server.py
```

You should see:

```text
Server monitor listening on http://0.0.0.0:8080
```

## Open the Dashboard

Find the server IP:

```bash
ip -4 addr
```

Then open:

```text
http://SERVER_IP:8080
```

from another device that can reach the server.

## Configuration

The default configuration is:

```python
HOST = "0.0.0.0"
PORT = 8080
```

To change the port, edit `PORT` in `server.py`.

## How It Works

```text
Browser
   |
   | HTTP
   v
server.py
   |
   +-- /proc/stat             CPU
   +-- /proc/meminfo          Memory / Swap
   +-- /proc/net/dev          Network traffic
   +-- /proc/uptime           Uptime
   +-- statvfs("/")           Storage
   +-- /sys/class/thermal     Temperature
   +-- /sys/class/power_supply Battery
   +-- /proc/<pid>/comm       Process checks
   +-- localhost service ports Service availability
   +-- Speed-test endpoint     On-demand only
```

While the dashboard is open, the browser requests:

```text
GET /api/status
```

once per second. The server returns the current status as JSON.

When nobody has the dashboard open, there are no browser status requests.

## Resource Usage

The monitor is designed for low-power and resource-constrained systems.

Normal dashboard operation:
- does not launch subprocesses every second
- does not run shell commands every second
- does not run external speed tests
- does not maintain a database
- does not store monitoring history
- does not run a continuous background monitoring loop

Most metrics are read directly from Linux `/proc` and `/sys`.

This makes it suitable for:
- old laptops
- Raspberry Pi
- ARM boards
- Android/Linux servers
- VPS instances
- home servers
- low-power machines

## n8n Monitoring

The example configuration checks n8n at:

```text
127.0.0.1:5678
```

It does not execute `pm2 jlist` on every dashboard refresh.

If n8n uses another port, change the address and port in `server.py`.

## Cloudflare Monitoring

The example checks for the `cloudflared` process. If Cloudflare Tunnel is not used, remove or adapt that service check.

## SSH Monitoring

The example checks for an `sshd` process. Adapt it if the target server uses another SSH implementation or service layout.

## Internet Speed Test

Click:

```text
Run Speed Test
```

to start the test.

It reports:

```text
Download
Upload
Latency
```

The test is not automatic, so normal dashboard operation does not continuously consume Internet bandwidth.

## Running in the Background

The monitor can be managed by a process supervisor such as:
- systemd
- PM2
- supervisord
- OpenRC
- another process supervisor

The repository does not force a specific process manager.

### systemd example

Create:

```text
/etc/systemd/system/server-monitor.service
```

with:

```ini
[Unit]
Description=Lightweight Linux Server Monitor
After=network.target

[Service]
Type=simple
User=server
WorkingDirectory=/opt/server-monitor
ExecStart=/usr/bin/python3 /opt/server-monitor/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor
sudo systemctl status server-monitor
```

### PM2 example

If PM2 is already installed:

```bash
pm2 start server.py --name server-monitor --interpreter python3
pm2 save
```

## Security

By default, `0.0.0.0:8080` makes the dashboard reachable from networks that can reach the server on port 8080.

For a home server, this is normally appropriate when port 8080 is restricted to a trusted local network.

Do NOT expose this port directly to the public Internet without appropriate authentication and network controls.

Avoid:
- router port forwarding for port 8080
- public firewall exposure
- public reverse-proxy exposure without authentication

The dashboard should preferably remain LAN-only.

## Important Security Considerations

Never commit:
- SSH private keys
- `authorized_keys`
- Cloudflare certificates
- Cloudflare tunnel credentials
- API keys
- API tokens
- passwords
- `.env` files
- browser cookies
- session tokens
- PM2 dumps
- private server logs
- private IP documentation
- private deployment configuration

The repository should contain the reusable application, not the credentials or configuration of a specific server.

## Adapting to Another Server

On a new Linux server:

```bash
git clone https://github.com/YOUR_USERNAME/server-monitor.git
cd server-monitor
chmod +x server.py
python3 server.py
```

Then open:

```text
http://SERVER_IP:8080
```

Some metrics may differ by hardware or kernel. For example:
- battery information may not exist
- temperature interfaces vary
- service process names vary
- n8n may use another port
- network interface names vary

## Android / Linux Chroot Use

The monitor can also run inside a Linux userspace running on Android through a chroot or similar environment.

In such environments, `/proc` and `/sys` may expose information from the Android host kernel. CPU and memory values should therefore be interpreted as system-level values rather than necessarily being limited to the Linux userspace.

```text
Android + Linux userspace
        |
        +-- shared kernel
        +-- shared memory
        +-- shared CPU
        +-- shared network
```

## API

```text
GET /
```

Returns the dashboard.

```text
GET /api/status
```

Returns current server status as JSON.

```text
GET /api/speedtest
```

Runs the on-demand Internet Speed Test.

The speed-test endpoint should only be triggered manually because it consumes network bandwidth and system resources.

## Example API Response

A simplified response:

```json
{
  "hostname": "server",
  "architecture": "aarch64",
  "cpu": 12.4,
  "cpu_count": 8,
  "load": [0.42, 0.38, 0.31],
  "memory": {
    "total": 6000000000,
    "used": 2800000000,
    "available": 3200000000,
    "swap_total": 2000000000,
    "swap_used": 24000000
  },
  "storage": {
    "total": 80000000000,
    "used": 3500000000,
    "free": 76500000000
  }
}
```

Values depend on the actual server.

## Project Philosophy

The goal is not to replace full monitoring platforms.

It is intentionally:
- small
- simple
- dependency-free
- easy to understand
- easy to modify
- lightweight
- suitable for low-power hardware
- suitable for ARM64
- suitable for local networks

For large production environments, use a dedicated observability stack such as Prometheus/Grafana or another appropriate solution.

## Current Version

Initial project developed for a lightweight ARM64 Linux server running on resource-constrained hardware. The application is designed to remain portable to other Linux systems.

## License

MIT License.

See [LICENSE](LICENSE).
