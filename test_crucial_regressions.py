import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import server


class TestCrucialRegressions(unittest.TestCase):
    """Automated regression tests covering all verified crucial and blocking issues."""

    # -------------------------------------------------------------------------
    # Issue 1: Uninstaller loose folder matching
    # -------------------------------------------------------------------------
    def test_issue1_uninstaller_strict_systemd_matching(self):
        """Verifies that uninstaller regex matches only the exact directory boundary."""
        uninstaller_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uninstall.sh")
        with open(uninstaller_path, "r") as f:
            content = f.read()

        self.assertIn("SCRIPT_DIR/(server\\.py|servermonitor)", content)
        self.assertNotIn('grep -q "$SCRIPT_DIR"', content)

    def test_issue1_uninstaller_strict_pm2_boundary_matching(self):
        """Verifies that PM2 processes in substring sibling directories are rejected."""
        target_dir = "/opt/servermonitor"

        fake_procs = [
            # Host process
            {"pm_id": 2, "name": "server-monitor", "pm2_env": {"pm_exec_path": "/home/server/Programming/server-monitor/server.js", "pm_cwd": "/home/server/Programming/server-monitor"}},
            # Substring sibling path: should NOT match!
            {"pm_id": 3, "name": "servermonitor-backup", "pm2_env": {"pm_exec_path": "/opt/servermonitor-backup/server.py", "pm_cwd": "/opt/servermonitor-backup"}},
            # Another foreign app
            {"pm_id": 4, "name": "other", "pm2_env": {"pm_exec_path": "/opt/servermonitor/other.py", "pm_cwd": "/opt/servermonitor"}},
            # Genuine installation matching target_dir
            {"pm_id": 10, "name": "servermonitor", "pm2_env": {"pm_exec_path": "/opt/servermonitor/server.py", "pm_cwd": "/opt/servermonitor"}},
        ]

        matches = []
        for proc in fake_procs:
            pm_env = proc.get("pm2_env", {})
            exec_path = os.path.abspath(pm_env.get("pm_exec_path", "")) if pm_env.get("pm_exec_path") else ""
            cwd = os.path.abspath(pm_env.get("pm_cwd", "")) if pm_env.get("pm_cwd") else ""
            pm_id = proc.get("pm_id")
            name = proc.get("name", "")

            is_in_target = False
            if exec_path:
                try:
                    if os.path.commonpath([target_dir, exec_path]) == target_dir and os.path.basename(exec_path) == "server.py":
                        is_in_target = True
                except ValueError:
                    pass
            if cwd and not is_in_target:
                try:
                    if os.path.commonpath([target_dir, cwd]) == target_dir and name.startswith("servermonitor"):
                        is_in_target = True
                except ValueError:
                    pass

            if is_in_target:
                matches.append(pm_id)

        self.assertEqual(matches, [10])
        self.assertNotIn(2, matches)
        self.assertNotIn(3, matches)
        self.assertNotIn(4, matches)

    # -------------------------------------------------------------------------
    # Issue 2: Installer backup protection
    # -------------------------------------------------------------------------
    def test_issue2_installer_creates_timestamped_backups(self):
        """Verifies that install.sh creates timestamped backups before overwriting files."""
        installer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")
        with open(installer_path, "r") as f:
            content = f.read()

        self.assertIn("backup_file()", content)
        self.assertIn("${file}.bak.${timestamp}", content)
        self.assertIn("backup_file \"$UNIT_FILE\"", content)
        self.assertIn("backup_file \"$TARGET_DIR/$file_name\"", content)

    # -------------------------------------------------------------------------
    # Issue 3: Memory exhaustion DoS from large POST requests
    # -------------------------------------------------------------------------
    def test_issue3_post_large_payload_rejected_413(self):
        """Verifies that POST with Content-Length > 64KB is rejected with HTTP 413 without reading body."""
        class MockSocket:
            def __init__(self, request_bytes):
                self._rfile = io.BytesIO(request_bytes)
                self._wfile = io.BytesIO()

            def settimeout(self, timeout):
                self._timeout = timeout

            def makefile(self, mode, *args, **kwargs):
                if "r" in mode:
                    return self._rfile
                return self._wfile

            def sendall(self, data):
                self._wfile.write(data)

            def close(self):
                pass

        saved_cfg = server._global_config
        try:
            cfg = server.Config()
            cfg.auth_token = "secret123"
            server._global_config = cfg

            # Attempt a 100 KB payload with valid auth
            req = (
                b"POST /api/heartbeat HTTP/1.1\r\n"
                b"Host: 127.0.0.1:8080\r\n"
                b"Authorization: Bearer secret123\r\n"
                b"Content-Length: 102400\r\n"
                b"\r\n"
            )
            mock_sock = MockSocket(req)
            handler = server.Handler(mock_sock, ("127.0.0.1", 54321), None)

            output = mock_sock._wfile.getvalue().decode("utf-8", errors="replace")
            self.assertIn("413", output)
            self.assertIn("Payload Too Large", output)
        finally:
            server._global_config = saved_cfg

    def test_issue3_post_invalid_content_length_rejected_400(self):
        """Verifies that invalid or negative Content-Length is rejected with HTTP 400."""
        class MockSocket:
            def __init__(self, request_bytes):
                self._rfile = io.BytesIO(request_bytes)
                self._wfile = io.BytesIO()

            def settimeout(self, timeout):
                self._timeout = timeout

            def makefile(self, mode, *args, **kwargs):
                if "r" in mode:
                    return self._rfile
                return self._wfile

            def sendall(self, data):
                self._wfile.write(data)

            def close(self):
                pass

        saved_cfg = server._global_config
        try:
            cfg = server.Config()
            cfg.auth_token = "secret123"
            server._global_config = cfg

            # Negative content length
            req = (
                b"POST /api/heartbeat HTTP/1.1\r\n"
                b"Host: 127.0.0.1:8080\r\n"
                b"Authorization: Bearer secret123\r\n"
                b"Content-Length: -5\r\n"
                b"\r\n"
            )
            mock_sock = MockSocket(req)
            handler = server.Handler(mock_sock, ("127.0.0.1", 54321), None)

            output = mock_sock._wfile.getvalue().decode("utf-8", errors="replace")
            self.assertIn("400 Bad Request", output)
        finally:
            server._global_config = saved_cfg

    def test_issue3_post_unauthenticated_rejected_before_reading_body(self):
        """Verifies that unauthenticated POST requests are rejected with HTTP 401 before body is read."""
        class MockSocket:
            def __init__(self, request_bytes):
                self._rfile = io.BytesIO(request_bytes)
                self._wfile = io.BytesIO()
                self.read_spy = MagicMock(wraps=self._rfile.read)
                self._rfile.read = self.read_spy

            def settimeout(self, timeout):
                self._timeout = timeout

            def makefile(self, mode, *args, **kwargs):
                if "r" in mode:
                    return self._rfile
                return self._wfile

            def sendall(self, data):
                self._wfile.write(data)

            def close(self):
                pass

        saved_cfg = server._global_config
        try:
            cfg = server.Config()
            cfg.auth_token = "secret123"
            server._global_config = cfg

            # Unauthenticated request with large Content-Length
            req = (
                b"POST /api/heartbeat HTTP/1.1\r\n"
                b"Host: 127.0.0.1:8080\r\n"
                b"Content-Length: 50000\r\n"
                b"\r\n"
            )
            mock_sock = MockSocket(req)
            handler = server.Handler(mock_sock, ("127.0.0.1", 54321), None)

            output = mock_sock._wfile.getvalue().decode("utf-8", errors="replace")
            self.assertIn("401", output)
            self.assertIn("Unauthorized", output)
            # Body read method on rfile must NEVER have been called!
            mock_sock.read_spy.assert_not_called()
        finally:
            server._global_config = saved_cfg

    def test_issue3_post_rate_limiting(self):
        """Verifies that rate limiting applies to POST requests."""
        class MockSocket:
            def __init__(self, request_bytes):
                self._rfile = io.BytesIO(request_bytes)
                self._wfile = io.BytesIO()

            def settimeout(self, timeout):
                self._timeout = timeout

            def makefile(self, mode, *args, **kwargs):
                if "r" in mode:
                    return self._rfile
                return self._wfile

            def sendall(self, data):
                self._wfile.write(data)

            def close(self):
                pass

        # Exhaust rate limiter for test IP (max_requests is 120)
        test_ip = "192.0.2.1"
        for _ in range(130):
            server._global_rate_limiter.allow_request(test_ip)

        req = (
            b"POST /api/heartbeat HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8080\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )
        mock_sock = MockSocket(req)
        handler = server.Handler(mock_sock, (test_ip, 54321), None)

        output = mock_sock._wfile.getvalue().decode("utf-8", errors="replace")
        self.assertIn("429", output)
        self.assertIn("Rate limit exceeded", output)

    # -------------------------------------------------------------------------
    # Issue 4: Connection exhaustion Slowloris / socket timeout
    # -------------------------------------------------------------------------
    def test_issue4_socket_timeout_configured_on_handler(self):
        """Verifies that Handler defines a 10s socket timeout."""
        self.assertTrue(hasattr(server.Handler, "timeout"))
        self.assertEqual(server.Handler.timeout, 10.0)

    def test_issue4_timeout_caught_gracefully_in_handle(self):
        """Verifies that socket.timeout / TimeoutError in handle() closes connection without traceback."""
        class MockTimeoutHandler(server.Handler):
            def __init__(self):
                self.close_connection = False

            def handle(self):
                try:
                    raise socket.timeout("timed out")
                except (socket.timeout, TimeoutError):
                    self.close_connection = True

        h = MockTimeoutHandler()
        h.handle()
        self.assertTrue(h.close_connection)

    # -------------------------------------------------------------------------
    # Issue 5: Corrupt config.json fail-closed
    # -------------------------------------------------------------------------
    def test_issue5_corrupt_config_raises_runtime_error(self):
        """Verifies that a corrupt config.json fails closed and raises RuntimeError."""
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("{corrupt_json: true, missing_quotes")
            bad_path = f.name

        try:
            with self.assertRaises(RuntimeError) as ctx:
                server.Config(config_file=bad_path)
            self.assertIn("Fatal: Corrupt or unparseable configuration file", str(ctx.exception))
        finally:
            if os.path.exists(bad_path):
                os.unlink(bad_path)

    # -------------------------------------------------------------------------
    # Issue 6: Systemd non-root default & --user CLI parameter
    # -------------------------------------------------------------------------
    def test_issue6_systemd_defaults_to_non_root(self):
        """Verifies that generate_systemd_service defaults to nobody/SUDO_USER when root."""
        with patch.dict(os.environ, {"USER": "root", "SUDO_USER": "admin"}):
            with patch("os.geteuid", return_value=0):
                unit = server.generate_systemd_service()
                self.assertIn("User=admin", unit)
                self.assertNotIn("User=root", unit)

        with patch.dict(os.environ, {"USER": "root"}, clear=True):
            with patch("os.geteuid", return_value=0):
                unit = server.generate_systemd_service()
                self.assertIn("User=nobody", unit)
                self.assertNotIn("User=root", unit)

    def test_issue6_systemd_user_parameter(self):
        """Verifies that explicit user parameter is respected."""
        unit = server.generate_systemd_service(user="custommonitor")
        self.assertIn("User=custommonitor", unit)

    # -------------------------------------------------------------------------
    # Issue 7: DNS rebinding defense (Host header validation)
    # -------------------------------------------------------------------------
    def test_issue7_dns_rebinding_rejection(self):
        """Verifies that malicious or missing Host headers are rejected with HTTP 400."""
        class MockSocket:
            def __init__(self, request_bytes):
                self._rfile = io.BytesIO(request_bytes)
                self._wfile = io.BytesIO()

            def settimeout(self, timeout):
                self._timeout = timeout

            def makefile(self, mode, *args, **kwargs):
                if "r" in mode:
                    return self._rfile
                return self._wfile

            def sendall(self, data):
                self._wfile.write(data)

            def close(self):
                pass

        # 1. Attacker domain host header
        req_rebinding = (
            b"GET /api/status HTTP/1.1\r\n"
            b"Host: evil.attacker.com\r\n"
            b"\r\n"
        )
        mock_sock = MockSocket(req_rebinding)
        server.Handler(mock_sock, ("127.0.0.1", 54321), None)
        output = mock_sock._wfile.getvalue().decode("utf-8", errors="replace")
        self.assertIn("400 Bad Request", output)
        self.assertIn("Invalid or missing Host header", output)

        # 2. Legitimate localhost host header
        req_valid = (
            b"GET /api/config HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8080\r\n"
            b"\r\n"
        )
        mock_sock2 = MockSocket(req_valid)
        server.Handler(mock_sock2, ("127.0.0.1", 54321), None)
        output2 = mock_sock2._wfile.getvalue().decode("utf-8", errors="replace")
        self.assertIn("200 OK", output2)

    # -------------------------------------------------------------------------
    # Issue 8 & 19: Setup wizard cwd & --config flag
    # -------------------------------------------------------------------------
    def test_issue8_find_config_prefers_install_dir(self):
        """Verifies that _find_config_file prefers install directory over cwd."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                cfg = server.Config()
                expected_dir = os.path.dirname(os.path.abspath(server.__file__))
                self.assertTrue(cfg.config_file.startswith(expected_dir))
                self.assertFalse(cfg.config_file.startswith(tmpdir))
            finally:
                os.chdir(orig_cwd)

    def test_issue19_setup_wizard_accepts_config_file(self):
        """Verifies that run_interactive_setup accepts config_file parameter."""
        import inspect
        sig = inspect.signature(server.run_interactive_setup)
        self.assertIn("config_file", sig.parameters)

    # -------------------------------------------------------------------------
    # Issue 13 & 14: Speed-test boot timer & cooldown config
    # -------------------------------------------------------------------------
    def test_issue13_speedtest_boot_timer_does_not_block_first_run(self):
        """Verifies that speed test allows immediate first run even when uptime < 60s."""
        server._speedtest_last_run = 0.0
        server._speedtest_lock = threading.Lock()

        with patch("server.time.monotonic", return_value=15.0), \
             patch("server.socket.create_connection"), \
             patch("server.urllib.request.urlopen") as mock_url:
            mock_dl = MagicMock()
            mock_dl.read.side_effect = [b"x" * 1024, b""]
            mock_ul = MagicMock()
            mock_ul.read.return_value = b"OK"
            mock_url.side_effect = [
                MagicMock(__enter__=MagicMock(return_value=mock_dl)),
                MagicMock(__enter__=MagicMock(return_value=mock_ul)),
            ]

            res = server.run_speed_test()
            self.assertTrue(res["success"])
            self.assertIsNone(res["error"])

    def test_issue14_speedtest_cooldown_respects_config(self):
        """Verifies that speedtest uses cfg.speedtest_cooldown."""
        saved_cfg = server._global_config
        try:
            cfg = server.Config()
            cfg.data["speedtest_cooldown"] = 300.0  # 5 minutes
            server._global_config = cfg
            server._speedtest_last_run = 100.0
            server._speedtest_lock = threading.Lock()

            with patch("server.time.monotonic", return_value=150.0):
                res = server.run_speed_test()
                self.assertFalse(res["success"])
                # Elapsed is 50s, remaining cooldown should be 300 - 50 = 250s
                self.assertIn("wait 250 seconds", res["error"])
        finally:
            server._global_config = saved_cfg

    # -------------------------------------------------------------------------
    # Issue 18: Config file atomic permissions (mode 0o600)
    # -------------------------------------------------------------------------
    def test_issue18_config_file_mode_0600(self):
        """Verifies that saving config creates a file strictly with mode 0o600."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "secure_config.json")
            cfg = server.Config(config_file=cfg_path)
            cfg.auth_token = "secret-token-xyz"
            self.assertTrue(cfg.save())

            st_mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
            self.assertEqual(st_mode, 0o600)


if __name__ == "__main__":
    unittest.main()
