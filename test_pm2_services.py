import json
import unittest
from unittest.mock import patch, MagicMock
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import server


class TestPm2ServicesBackend(unittest.TestCase):

    def setUp(self):
        if hasattr(server, "_pm2_cache_data"):
            server._pm2_cache_data = None
        if hasattr(server, "_pm2_cache_time"):
            server._pm2_cache_time = 0.0

    def test_pm2_cache_lock_instance(self):
        self.assertTrue(hasattr(server, "_pm2_cache_lock"))
        self.assertIsInstance(server._pm2_cache_lock, type(threading.Lock()))

    def test_pm2_cache_ttl_constant(self):
        self.assertTrue(hasattr(server, "PM2_CACHE_TTL"))
        self.assertEqual(server.PM2_CACHE_TTL, 5.0)

    @patch("server.subprocess.run")
    def test_pm2_services_success(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps([
            {
                "name": "web-app",
                "pm_id": 1,
                "pm2_env": {
                    "status": "online",
                    "restart_time": 3,
                    "pm_uptime": 1700000000000,
                    "env": {"SECRET_TOKEN": "shhh", "PATH": "/usr/bin"}
                },
                "monit": {
                    "memory": 52428800,
                    "cpu": 2.5
                }
            }
        ])
        mock_run.return_value = mock_proc

        result = server.get_pm2_services()
        mock_run.assert_called_once_with(
            ["pm2", "jlist"],
            capture_output=True,
            text=True,
            timeout=5
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["services"]), 1)
        svc = result["services"][0]
        self.assertEqual(svc["name"], "web-app")
        self.assertEqual(svc["status"], "online")
        self.assertEqual(svc["pm_id"], 1)
        self.assertEqual(svc["cpu"], 2.5)
        self.assertEqual(svc["memory"], 52428800)
        # restarts and uptime stripped for minimal payload
        self.assertNotIn("restarts", svc)
        self.assertNotIn("uptime", svc)
        # Ensure sensitive env vars are not exposed
        self.assertNotIn("env", svc)
        self.assertNotIn("SECRET_TOKEN", json.dumps(result))

    @patch("server.subprocess.run")
    def test_pm2_services_empty(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "[]"
        mock_run.return_value = mock_proc

        result = server.get_pm2_services()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["services"], [])

    @patch("server.subprocess.run")
    def test_pm2_services_command_error(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "PM2 Daemon Error"
        mock_run.return_value = mock_proc

        result = server.get_pm2_services()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["services"], [])
        self.assertIn("error", result)

    @patch("server.subprocess.run")
    def test_pm2_services_unavailable(self, mock_run):
        mock_run.side_effect = FileNotFoundError()

        result = server.get_pm2_services()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["services"], [])
        self.assertIn("error", result)

    @patch("server.subprocess.run")
    def test_pm2_services_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["pm2", "jlist"], timeout=5)

        result = server.get_pm2_services()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["services"], [])
        self.assertIn("error", result)

    @patch("server.subprocess.run")
    def test_pm2_services_invalid_json(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "malformed json {"
        mock_run.return_value = mock_proc

        result = server.get_pm2_services()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["services"], [])
        self.assertIn("error", result)

    @patch("server.subprocess.run")
    def test_pm2_services_non_list_json(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"status": "running"}'
        mock_run.return_value = mock_proc

        result = server.get_pm2_services()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["services"], [])
        self.assertIn("error", result)

    @patch("server.get_process_status")
    @patch("server.get_pm2_services")
    def test_get_status_services_structure(self, mock_pm2, mock_process):
        mock_process.return_value = True
        mock_pm2.return_value = {"status": "ok", "services": []}

        status = server.get_status()
        self.assertIn("services", status)
        services = status["services"]

        # SSH must remain unchanged
        self.assertIn("sshd", services)
        self.assertTrue(services["sshd"])
        mock_process.assert_called_with("sshd")

        # PM2 must be present
        self.assertIn("pm2", services)
        self.assertEqual(services["pm2"], {"status": "ok", "services": []})

        # Hardcoded PM2 services must be replaced/removed
        self.assertNotIn("n8n", services)
        self.assertNotIn("cloudflared", services)

    @patch("server.subprocess.run")
    def test_pm2_cache_reuse_within_ttl(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "[]"
        mock_run.return_value = mock_proc

        clock = [100.0]
        with patch("server.time.monotonic", side_effect=lambda: clock[0]):
            res1 = server.get_pm2_services()
            clock[0] = 102.0
            res2 = server.get_pm2_services()
            clock[0] = 104.5
            res3 = server.get_pm2_services()

        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(res1, res2)
        self.assertEqual(res2, res3)

    @patch("server.subprocess.run")
    def test_pm2_cache_ttl_expiry(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "[]"
        mock_run.return_value = mock_proc

        clock = [100.0]
        with patch("server.time.monotonic", side_effect=lambda: clock[0]):
            res1 = server.get_pm2_services()
            clock[0] = 115.0  # past 10s TTL
            res2 = server.get_pm2_services()

        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(res1["status"], "ok")
        self.assertEqual(res2["status"], "ok")

    @patch("server.subprocess.run")
    def test_pm2_cache_ttl_expiry_refreshes_data(self, mock_run):
        mock_proc1 = MagicMock()
        mock_proc1.returncode = 0
        mock_proc1.stdout = json.dumps([{"name": "svc-1", "pm2_env": {"status": "online"}}])

        mock_proc2 = MagicMock()
        mock_proc2.returncode = 0
        mock_proc2.stdout = json.dumps([
            {"name": "svc-1", "pm2_env": {"status": "online"}},
            {"name": "svc-2", "pm2_env": {"status": "online"}}
        ])

        mock_run.side_effect = [mock_proc1, mock_proc2]

        clock = [100.0]
        with patch("server.time.monotonic", side_effect=lambda: clock[0]):
            res1 = server.get_pm2_services()
            self.assertEqual(len(res1["services"]), 1)
            self.assertEqual(res1["services"][0]["name"], "svc-1")

            # Advance clock past TTL
            clock[0] = 115.0
            res2 = server.get_pm2_services()
            self.assertEqual(len(res2["services"]), 2)
            self.assertEqual(res2["services"][1]["name"], "svc-2")

        self.assertEqual(mock_run.call_count, 2)

    @patch("server.subprocess.run")
    def test_pm2_cache_negative_error_result(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "daemon dead"
        mock_run.return_value = mock_proc

        clock = [100.0]
        with patch("server.time.monotonic", side_effect=lambda: clock[0]):
            res1 = server.get_pm2_services()
            clock[0] = 104.0
            res2 = server.get_pm2_services()

        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(res1["status"], "error")
        self.assertEqual(res2["status"], "error")

    @patch("server.subprocess.run")
    def test_pm2_cache_concurrent_stampede_prevention(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "[]"

        def slow_run(*args, **kwargs):
            time.sleep(0.05)
            return mock_proc

        mock_run.side_effect = slow_run

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(server.get_pm2_services) for _ in range(5)]
            results = [f.result() for f in futures]

        self.assertEqual(mock_run.call_count, 1)
        for r in results:
            self.assertEqual(r["status"], "ok")

    @patch("server.subprocess.run")
    def test_pm2_cache_concurrent_requests_during_expiry(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "[]"

        def slow_run(*args, **kwargs):
            time.sleep(0.05)
            return mock_proc

        mock_run.side_effect = slow_run

        # Prime the cache
        server.get_pm2_services()
        self.assertEqual(mock_run.call_count, 1)

        # Force expiry of cache
        server._pm2_cache_time = time.monotonic() - 20.0

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(server.get_pm2_services) for _ in range(5)]
            results = [f.result() for f in futures]

        self.assertEqual(mock_run.call_count, 2)
        for r in results:
            self.assertEqual(r["status"], "ok")


class TestServicesFrontendHtmlAndCss(unittest.TestCase):

    def test_services_list_scrollable(self):
        # Service list must be vertically scrollable while card dimensions are preserved
        self.assertIn("overflow-y: auto", server.HTML)
        self.assertIn("max-height:", server.HTML)

    def test_sshd_retained_in_html(self):
        self.assertIn('id="sshd"', server.HTML)
        self.assertIn("SSH", server.HTML)

    def test_hardcoded_pm2_rows_removed_from_html(self):
        self.assertNotIn('id="n8n"', server.HTML)
        self.assertNotIn('id="cloudflared"', server.HTML)

    def test_pm2_container_present_in_html(self):
        self.assertIn('id="pm2Services"', server.HTML)

    def test_frontend_js_handles_states_and_details(self):
        # JS must render safe details (cpu/memory or restarts) and handle empty, error, unavailable
        html = server.HTML
        self.assertIn("pm2Services", html)
        self.assertIn("unavailable", html)
        self.assertIn("error", html)

    def test_frontend_polling_interval_is_2_seconds(self):
        self.assertIn("setInterval(\n    update,\n    2000\n);", server.HTML)

    def test_services_scrollbar_invisible_cross_browser(self):
        html = server.HTML
        self.assertIn("scrollbar-width: none", html)
        self.assertIn("-ms-overflow-style: none", html)
        self.assertIn(".services::-webkit-scrollbar", html)
        self.assertIn("display: none", html)

    def test_frontend_visibility_and_overlap_guards(self):
        html = server.HTML
        self.assertIn("document.hidden", html)
        self.assertIn("visibilitychange", html)
        self.assertIn("isUpdating", html)

    def test_frontend_escapes_service_name_and_errors(self):
        html = server.HTML
        self.assertIn("escapeHtml(svc.name)", html)
        self.assertIn("escapeHtml(pm2.error", html)


if __name__ == "__main__":
    unittest.main()
