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


if __name__ == "__main__":
    unittest.main()
