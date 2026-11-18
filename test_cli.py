import json
import os
import tempfile
import unittest

import server


class TestCliAndConfig(unittest.TestCase):

    def test_default_config(self):
        cfg = server.Config()
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.auth_token, "")
        self.assertEqual(cfg.sample_interval, 2.0)
        self.assertEqual(cfg.inactivity_timeout, 15.0)

    def test_load_save_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            cfg = server.Config(config_file=cfg_path)
            cfg.port = 8888
            cfg.auth_token = "test-token-777"
            cfg.save()

            self.assertTrue(os.path.exists(cfg_path))

            # Load into new config
            cfg2 = server.Config(config_file=cfg_path)
            self.assertEqual(cfg2.port, 8888)
            self.assertEqual(cfg2.auth_token, "test-token-777")

    def test_ssh_tunnel_command_generation(self):
        cmd = server.generate_ssh_tunnel_command(port=8085, host="192.168.1.50", user="admin")
        self.assertIn("ssh -N -L 8085:127.0.0.1:8085", cmd)
        self.assertIn("admin@192.168.1.50", cmd)

    def test_pm2_ecosystem_generation(self):
        js = server.generate_pm2_config(app_name="servermonitor-prod", script_path="/opt/servermonitor/server.py", port=8085)
        self.assertIn("name: 'servermonitor-prod'", js)
        self.assertIn("PORT: '8085'", js)

    def test_systemd_service_generation(self):
        unit = server.generate_systemd_service(user="server", exec_start="/usr/bin/python3 /opt/servermonitor/server.py")
        self.assertIn("[Unit]", unit)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/servermonitor/server.py", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ProtectHome=read-only", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        # Must isolate writable state and configuration in designated systemd directories
        self.assertIn("StateDirectory=servermonitor", unit)
        self.assertIn("ConfigurationDirectory=servermonitor", unit)
        self.assertIn("ReadOnlyPaths=", unit)
        self.assertNotIn("ReadWritePaths=", unit)

    def test_config_deep_copy_isolation(self):
        """Verifies that mutating one Config instance does not contaminate another or DEFAULT_CONFIG."""
        cfg1 = server.Config()
        cfg2 = server.Config()

        # Mutate nested dictionary in cfg1
        cfg1.data["modules"]["cpu"] = False
        cfg1.data["monitored_systemd_units"].append("custom-service")

        # Verify cfg2 remains pristine
        self.assertTrue(cfg2.data["modules"]["cpu"])
        self.assertNotIn("custom-service", cfg2.data["monitored_systemd_units"])

        # Verify class DEFAULT_CONFIG remains untouched
        self.assertTrue(server.Config.DEFAULT_CONFIG["modules"]["cpu"])
        self.assertNotIn("custom-service", server.Config.DEFAULT_CONFIG["monitored_systemd_units"])

    def test_non_loopback_binding_security(self):
        """Verifies that binding to a non-loopback interface without an auth token is rejected."""
        from unittest.mock import patch
        import io

        # 1. Non-loopback 0.0.0.0 without token must exit with code 1
        with patch("sys.argv", ["server.py", "--host", "0.0.0.0", "--port", "9876"]), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                server.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Security Error", mock_err.getvalue())
            self.assertIn("non-loopback interface '0.0.0.0'", mock_err.getvalue())

        # 2. LAN IP without token must exit with code 1
        with patch("sys.argv", ["server.py", "--host", "192.168.1.50", "--port", "9876"]), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                server.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Security Error", mock_err.getvalue())

    def test_systemd_state_and_config_directory_resolution(self):
        """Verifies that Config resolves systemd STATE_DIRECTORY and CONFIGURATION_DIRECTORY."""
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            test_cfg = os.path.join(tmpdir, "config.json")
            with open(test_cfg, "w") as f:
                json.dump({"port": 8765}, f)

            with patch.dict(os.environ, {"STATE_DIRECTORY": tmpdir}):
                resolved = server.Config._find_config_file(None)
                self.assertEqual(resolved, test_cfg)

    def test_status_cli_handling(self):
        from unittest.mock import patch, MagicMock
        import io

        # 1. Unauthenticated server running (HTTP 200)
        with patch("sys.argv", ["server.py", "--status", "--port", "9999"]), \
             patch("urllib.request.urlopen") as mock_url, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"version": "2.0.0"}).encode()
            mock_url.return_value.__enter__.return_value = mock_resp
            server.main()
            self.assertIn("[✓] ServerMonitor is running", mock_out.getvalue())

        # 2. Protected server running with valid token (HTTP 200 with Bearer)
        with patch("sys.argv", ["server.py", "--status", "--port", "9999", "--token", "secret123"]), \
             patch("urllib.request.urlopen") as mock_url, \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"version": "2.0.0"}).encode()
            mock_url.return_value.__enter__.return_value = mock_resp
            server.main()
            self.assertIn("[✓] ServerMonitor is running", mock_out.getvalue())
            self.assertIn("Authenticated", mock_out.getvalue())

        # 3. Protected server running without token -> reports Protected, NOT that server is dead!
        from urllib.error import HTTPError
        with patch("sys.argv", ["server.py", "--status", "--port", "9999"]), \
             patch("urllib.request.urlopen", side_effect=HTTPError("http://127.0.0.1:9999/api/config", 401, "Unauthorized", {}, None)), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            server.main()
            self.assertIn("[✓] ServerMonitor is running", mock_out.getvalue())
            self.assertIn("Protected - HTTP 401 Unauthorized", mock_out.getvalue())

        # 4. Server completely unreachable -> exits with code 1
        from urllib.error import URLError
        with patch("sys.argv", ["server.py", "--status", "--port", "9999"]), \
             patch("urllib.request.urlopen", side_effect=URLError("Connection refused")), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                server.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("[✗] ServerMonitor is not running or unreachable", mock_out.getvalue())


if __name__ == "__main__":
    unittest.main()
