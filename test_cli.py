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
        self.assertIn("NoNewPrivileges=true", unit)
        # Must isolate writable state and paths
        self.assertIn("StateDirectory=servermonitor", unit)
        self.assertIn("ReadWritePaths=", unit)

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
