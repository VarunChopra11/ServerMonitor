import json
import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

import server


class TestServicesMonitoring(unittest.TestCase):

    @patch("server.subprocess.run")
    @patch("server.shutil.which")
    @patch("server.os.path.exists")
    def test_configurable_systemd_monitoring(self, mock_exists, mock_which, mock_run):
        mock_exists.return_value = True
        mock_which.return_value = "/usr/bin/systemctl"

        def fake_run(cmd, capture_output=True, text=True, timeout=2, shell=False):
            unit = cmd[2]
            res = MagicMock()
            if unit in ("ssh", "nginx"):
                res.returncode = 0
                res.stdout = "active\n"
            else:
                res.returncode = 3
                res.stdout = "inactive\n"
            return res

        mock_run.side_effect = fake_run

        res = server.get_systemd_services(units=["ssh", "nginx", "docker"])
        self.assertTrue(res["available"])
        self.assertEqual(res["units"]["ssh"], "active")
        self.assertEqual(res["units"]["nginx"], "active")
        self.assertEqual(res["units"]["docker"], "inactive")

    @patch("server.os.path.exists")
    def test_systemd_monitoring_unavailable(self, mock_exists):
        # When /run/systemd/system does not exist (e.g. inside Docker or container)
        mock_exists.return_value = False
        res = server.get_systemd_services(units=["ssh", "nginx"])
        self.assertFalse(res["available"])
        self.assertEqual(res["units"], {})

    @patch("server.read_file")
    @patch("server.os.listdir")
    @patch("server.os.path.exists")
    def test_monitored_processes_proc_inspection(self, mock_exists, mock_listdir, mock_read):
        mock_exists.return_value = True
        mock_listdir.return_value = ["1", "105", "200", "not_a_pid"]

        def fake_read(p):
            if p == "/proc/1/comm":
                return "systemd\n"
            if p == "/proc/105/comm":
                return "sshd\n"
            if p == "/proc/200/comm":
                return "sshd\n"
            return ""

        mock_read.side_effect = fake_read

        procs = server.get_monitored_processes(["sshd", "postgres"])
        self.assertIn("sshd", procs)
        self.assertTrue(procs["sshd"]["running"])
        self.assertEqual(procs["sshd"]["count"], 2)
        self.assertEqual(procs["sshd"]["pids"], [105, 200])

        self.assertIn("postgres", procs)
        self.assertFalse(procs["postgres"]["running"])
        self.assertEqual(procs["postgres"]["count"], 0)

    @patch("server.socket.create_connection")
    @patch("server.urllib.request.urlopen")
    def test_http_and_tcp_health_checks(self, mock_urlopen, mock_socket):
        # Mock HTTP response
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        # Mock TCP connection
        mock_socket.return_value.__enter__.return_value = MagicMock()

        endpoints = [
            {"name": "Local Web", "url": "http://127.0.0.1:8080/health", "type": "http"},
            {"name": "Database", "host": "127.0.0.1", "port": 5432, "type": "tcp"}
        ]

        results = server.get_health_checks(endpoints)
        self.assertEqual(len(results), 2)

        http_check = [r for r in results if r["type"] == "http"][0]
        self.assertEqual(http_check["name"], "Local Web")
        self.assertEqual(http_check["status"], "up")
        self.assertEqual(http_check["code"], 200)
        self.assertIn("latency_ms", http_check)

        tcp_check = [r for r in results if r["type"] == "tcp"][0]
        self.assertEqual(tcp_check["name"], "Database")
        self.assertEqual(tcp_check["status"], "up")
        self.assertIn("latency_ms", tcp_check)

    @patch("server.subprocess.run")
    @patch("server.shutil.which")
    def test_docker_podman_container_monitoring_success(self, mock_which, mock_run):
        mock_which.side_effect = lambda b: "/usr/bin/docker" if b == "docker" else None

        ps_output = """c102a9b3d4e5\tweb-app\tnginx:alpine\tUp 3 hours\trunning
d987e6f5c4b3\tdb\tpostgres:15\tExited (0) 10 minutes ago\texited
"""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ps_output
        mock_run.return_value = mock_proc

        dkr = server.get_docker_containers()
        self.assertTrue(dkr["available"])
        self.assertEqual(dkr["runtime"], "docker")
        self.assertEqual(dkr["total"], 2)
        self.assertEqual(dkr["running"], 1)

        containers = dkr["containers"]
        self.assertEqual(len(containers), 2)
        self.assertEqual(containers[0]["name"], "web-app")
        self.assertEqual(containers[0]["state"], "running")
        self.assertEqual(containers[1]["name"], "db")
        self.assertEqual(containers[1]["state"], "exited")

    @patch("server.shutil.which")
    def test_docker_podman_not_installed(self, mock_which):
        mock_which.return_value = None
        dkr = server.get_docker_containers()
        self.assertFalse(dkr["available"])
        self.assertEqual(dkr["containers"], [])
        self.assertIn("not found", dkr["error"])

    @patch("server.get_docker_containers")
    @patch("server.get_health_checks")
    @patch("server.get_monitored_processes")
    @patch("server.get_systemd_services")
    @patch("server.get_pm2_services")
    @patch("server.get_process_status")
    def test_complete_status_contains_all_services(self, mock_proc_status, mock_pm2, mock_sysd, mock_procs, mock_hc, mock_dkr):
        mock_proc_status.return_value = True
        mock_pm2.return_value = {"status": "ok", "services": []}
        mock_sysd.return_value = {"available": True, "units": {"ssh": "active"}}
        mock_procs.return_value = {"sshd": {"running": True, "count": 1, "pids": [123]}}
        mock_hc.return_value = []
        mock_dkr.return_value = {"available": True, "containers": []}

        status = server.get_status()
        self.assertIn("services", status)
        svcs = status["services"]

        self.assertIn("sshd", svcs)
        self.assertIn("pm2", svcs)
        self.assertIn("systemd", svcs)
        self.assertIn("processes", svcs)
        self.assertIn("health_checks", svcs)
        self.assertIn("docker", svcs)


if __name__ == "__main__":
    unittest.main()
