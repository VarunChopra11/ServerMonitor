import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server


class TestIntegrationLiveServer(unittest.TestCase):
    """End-to-end integration tests using an isolated live loopback HTTP server."""

    @classmethod
    def setUpClass(cls):
        # Pick an unused high test port
        cls.test_port = 8999
        cls.test_host = "127.0.0.1"
        cls.test_token = "integration-test-token-42"

        # Initialize config
        cls.config = server.Config()
        cls.config.host = cls.test_host
        cls.config.port = cls.test_port
        cls.config.auth_token = cls.test_token
        cls.config.data["sample_interval"] = 0.5
        cls.config.data["inactivity_timeout"] = 1.0

        server._global_config = cls.config

        # Initialize collector
        cls.collector = server.OnDemandCollector(sample_interval=0.5, inactivity_timeout=1.0)
        cls.collector.start()
        server._global_collector = cls.collector

        # Start HTTP server in background thread
        cls.http_server = server.ThreadingHTTPServer((cls.test_host, cls.test_port), server.Handler)
        cls.server_thread = threading.Thread(target=cls.http_server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.collector.stop()
        cls.http_server.shutdown()
        cls.http_server.server_close()
        server._global_collector = None
        server._global_config = None

    def test_dashboard_html_served_with_security_headers(self):
        url = f"http://{self.test_host}:{self.test_port}/"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            headers = dict(resp.headers)
            self.assertIn("Content-Security-Policy", headers)
            self.assertEqual(headers.get("X-Frame-Options"), "DENY")
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            content = resp.read().decode("utf-8")
            self.assertIn("ServerMonitor", content)
            self.assertIn("id=\"pm2Services\"", content)
            self.assertIn("id=\"sshd\"", content)

    def test_auth_rejection_without_token(self):
        url = f"http://{self.test_host}:{self.test_port}/api/status"
        req = urllib.request.Request(url)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn("WWW-Authenticate", ctx.exception.headers)

    def test_api_status_success_with_bearer_token(self):
        url = f"http://{self.test_host}:{self.test_port}/api/status?session=integration-client-1"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.test_token}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("hostname", data)
            self.assertIn("cpu", data)
            self.assertIn("memory", data)
            self.assertIn("storage", data)
            self.assertIn("disks", data)
            self.assertIn("services", data)
            self.assertIn("collector_state", data)
            self.assertEqual(data["collector_state"], "active")

    def test_api_config_endpoint(self):
        url = f"http://{self.test_host}:{self.test_port}/api/config"
        req = urllib.request.Request(url, headers={"X-Monitor-Token": self.test_token})
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["auth_required"])
            self.assertIn("version", data)

    def test_heartbeat_post_endpoint(self):
        url = f"http://{self.test_host}:{self.test_port}/api/heartbeat?session=test-heartbeat"
        req = urllib.request.Request(url, data=b"{}", headers={"Authorization": f"Bearer {self.test_token}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")


if __name__ == "__main__":
    unittest.main()
