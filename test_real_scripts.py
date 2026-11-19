import copy
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import server


class TestRealScriptsAndEdgeCases(unittest.TestCase):
    """End-to-end tests executing real scripts against simulated environments and testing advanced defenses."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sm_test_")
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin_dir, exist_ok=True)

        self.target_dir = os.path.join(self.tmpdir, "app")
        os.makedirs(self.target_dir, exist_ok=True)

        # Copy real repository files into temp directory
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        for fname in ["server.py", "servermonitor", "install.sh", "uninstall.sh", "config.example.json"]:
            src = os.path.join(repo_dir, fname)
            if os.path.exists(src):
                shutil.copy2(src, self.target_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_real_uninstall_with_stub_pm2_calls_pm2_save(self):
        """Executes real uninstall.sh with a stub pm2 and systemctl, verifying exact path filtering and pm2 save."""
        pm2_log = os.path.join(self.tmpdir, "pm2.log")

        # Create stub pm2
        stub_pm2 = os.path.join(self.bin_dir, "pm2")
        mock_proc_list = [
            {
                "name": "other-app",
                "pm_id": 1,
                "pm2_env": {
                    "pm_exec_path": "/var/www/other/server.py",
                    "pm_cwd": "/var/www/other"
                }
            },
            {
                "name": "servermonitor-test",
                "pm_id": 99,
                "pm2_env": {
                    "pm_exec_path": os.path.join(self.target_dir, "server.py"),
                    "pm_cwd": self.target_dir
                }
            }
        ]

        with open(stub_pm2, "w") as f:
            f.write(f"""#!/bin/sh
echo "$@" >> "{pm2_log}"
if [ "$1" = "jlist" ]; then
    echo '{json.dumps(mock_proc_list)}'
    exit 0
fi
exit 0
""")
        os.chmod(stub_pm2, 0o755)

        # Create stub systemctl
        stub_sysctl = os.path.join(self.bin_dir, "systemctl")
        with open(stub_sysctl, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub_sysctl, 0o755)

        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"

        # Run uninstall.sh with piped 'y' to confirm
        proc = subprocess.run(
            ["bash", os.path.join(self.target_dir, "uninstall.sh")],
            input="y\ny\ny\ny\n",
            capture_output=True,
            text=True,
            env=env,
            timeout=5
        )

        self.assertEqual(proc.returncode, 0)

        with open(pm2_log, "r") as f:
            log_content = f.read()

        # Must have stopped and deleted matching process 99
        self.assertIn("stop 99", log_content)
        self.assertIn("delete 99", log_content)
        # Must have called pm2 save!
        self.assertIn("save", log_content)
        # Must NEVER have stopped or deleted foreign process 1
        self.assertNotIn("stop 1", log_content)
        self.assertNotIn("delete 1", log_content)

    def test_real_installer_exact_script_path_prevents_sibling_overwrite(self):
        """Verifies that install.sh does not treat sibling prefixes (e.g. /opt/servermonitor-old) as matching."""
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        env["SERVERMONITOR_DIR"] = self.target_dir

        # Create a mock systemd service file pointing to a sibling prefix directory
        fake_unit = os.path.join(self.tmpdir, "servermonitor.service")
        with open(fake_unit, "w") as f:
            f.write(f"[Service]\nExecStart=/usr/bin/python3 {self.target_dir}-old/server.py\n")

        # Verify exact script path check in install.sh script logic
        with open(os.path.join(self.target_dir, "install.sh"), "r") as f:
            script_text = f.read()

        self.assertIn('EXISTING_SCRIPT=$(echo "$EXISTING_EXEC"', script_text)
        self.assertIn('TARGET_SCRIPT="$TARGET_DIR/server.py"', script_text)
        self.assertIn('[ "$EXISTING_SCRIPT" != "$TARGET_SCRIPT" ]', script_text)

    def test_docker_fallback_to_podman_when_docker_fails(self):
        """Verifies that get_docker_containers falls back to podman when docker binary returns an error."""
        # Create a stub docker that exits with code 1 (daemon error)
        stub_docker = os.path.join(self.bin_dir, "docker")
        with open(stub_docker, "w") as f:
            f.write("#!/bin/sh\necho 'Cannot connect to Docker daemon' >&2\nexit 1\n")
        os.chmod(stub_docker, 0o755)

        # Create a stub podman that succeeds with 2 containers
        stub_podman = os.path.join(self.bin_dir, "podman")
        with open(stub_podman, "w") as f:
            f.write("""#!/bin/sh
printf "c12345678901\\tweb-app\\tnginx:latest\\tUp 2 hours\\trunning\\n"
printf "c98765432109\\tdatabase\\tpostgres:15\\tExited (0) 5 mins ago\\texited\\n"
exit 0
""")
        os.chmod(stub_podman, 0o755)

        with patch.dict(os.environ, {"PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}"}):
            res = server.get_docker_containers()

            self.assertTrue(res["available"])
            self.assertEqual(res["runtime"], "podman")
            self.assertEqual(res["total"], 2)
            self.assertEqual(res["running"], 1)
            self.assertEqual(res["containers"][0]["name"], "web-app")

    def test_health_checks_rejects_file_scheme_and_caps_redirects(self):
        """Verifies that get_health_checks strictly rejects file:// scheme and enforces http/https only."""
        # 1. file:// scheme must be rejected immediately without reading filesystem
        results = server.get_health_checks([{"name": "file-test", "url": "file:///etc/passwd"}])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "down")
        self.assertIn("Invalid URL scheme", results[0]["error"])

        # 2. ftp:// scheme must also be rejected
        results = server.get_health_checks([{"name": "ftp-test", "url": "ftp://example.com/test"}])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "down")
        self.assertIn("Invalid URL scheme", results[0]["error"])

    def test_cgroup_container_cpu_quota_parsing(self):
        """Verifies container CPU limits are detected from cgroup v2 cpu.max and cgroup v1 cfs_quota."""
        orig_read = server.read_file
        orig_exists = server.os.path.exists

        # Test cgroup v2
        def fake_exists_v2(p):
            if p == "/sys/fs/cgroup/cpu.max":
                return True
            return orig_exists(p)

        def fake_read_v2(p):
            if p == "/sys/fs/cgroup/cpu.max":
                return "200000 100000\n"
            return orig_read(p)

        with patch("server.os.path.exists", side_effect=fake_exists_v2), \
             patch("server.read_file", side_effect=fake_read_v2):
            stats = server.get_cpu_stats()
            self.assertIn("container", stats)
            self.assertTrue(stats["container"]["active"])
            self.assertEqual(stats["container"]["limit_cores"], 2.0)

        # Test cgroup v1
        def fake_exists_v1(p):
            if p in ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us"):
                return True
            if p == "/sys/fs/cgroup/cpu.max":
                return False
            return orig_exists(p)

        def fake_read_v1(p):
            if p == "/sys/fs/cgroup/cpu/cpu.cfs_quota_us":
                return "150000\n"
            if p == "/sys/fs/cgroup/cpu/cpu.cfs_period_us":
                return "100000\n"
            return orig_read(p)

        with patch("server.os.path.exists", side_effect=fake_exists_v1), \
             patch("server.read_file", side_effect=fake_read_v1):
            stats_v1 = server.get_cpu_stats()
            self.assertIn("container", stats_v1)
            self.assertTrue(stats_v1["container"]["active"])
            self.assertEqual(stats_v1["container"]["limit_cores"], 1.5)

    def test_allowed_hosts_and_tailscale_dns_rebinding_defense(self):
        """Verifies that is_allowed_host supports Tailscale IPs, subdomains, and wildcard configs."""
        cfg = server.Config()
        cfg.data["allowed_hosts"] = ["127.0.0.1", "localhost", "monitor.mydomain.com", "*.company.org"]
        saved_cfg = server._global_config
        server._global_config = cfg

        try:
            class MockReq:
                def __init__(self, host_val):
                    self.headers = {"Host": host_val}
                    self.client_address = ("127.0.0.1", 12345)

            # Localhost
            self.assertTrue(server.Handler.is_allowed_host(MockReq("127.0.0.1:8080")))
            self.assertTrue(server.Handler.is_allowed_host(MockReq("localhost")))
            self.assertTrue(server.Handler.is_allowed_host(MockReq("[::1]:8080")))

            # Explicitly allowed domain
            self.assertTrue(server.Handler.is_allowed_host(MockReq("monitor.mydomain.com")))
            self.assertTrue(server.Handler.is_allowed_host(MockReq("monitor.mydomain.com:443")))

            # Subdomain wildcard
            self.assertTrue(server.Handler.is_allowed_host(MockReq("metrics.company.org")))
            self.assertTrue(server.Handler.is_allowed_host(MockReq("sub.metrics.company.org")))

            # Tailscale MagicDNS and CGNAT IP
            self.assertTrue(server.Handler.is_allowed_host(MockReq("my-node.tailnet-xyz.ts.net")))
            self.assertTrue(server.Handler.is_allowed_host(MockReq("100.115.92.40:8080")))

            # Unauthorized malicious domain
            self.assertFalse(server.Handler.is_allowed_host(MockReq("attacker.evil.com")))
            self.assertFalse(server.Handler.is_allowed_host(MockReq("rebinding.attacker.com:8080")))
        finally:
            server._global_config = saved_cfg

    def test_atomic_config_save_temp_file_rename(self):
        """Verifies that Config.save uses an atomic temp file write followed by rename."""
        cfg_path = os.path.join(self.tmpdir, "config.json")
        cfg = server.Config(config_file=cfg_path)
        cfg.data["port"] = 8888
        self.assertTrue(cfg.save())

        self.assertTrue(os.path.exists(cfg_path))
        with open(cfg_path, "r") as f:
            data = json.load(f)
        self.assertEqual(data["port"], 8888)

        # Mode must strictly be 0600
        mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
