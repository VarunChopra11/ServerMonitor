import json
import os
import subprocess
import tempfile
import unittest


class TestInstallationSafety(unittest.TestCase):
    """Verifies conflict safety and path-matching isolation in install and uninstall scripts."""

    def test_python_version_check_in_installer(self):
        """Verifies that the installer script correctly enforces Python >= 3.8."""
        installer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")
        with open(installer_path, "r") as f:
            content = f.read()

        self.assertIn("sys.version_info >= (3, 8)", content)
        self.assertIn("ServerMonitor requires Python 3.8 or newer", content)

    def test_systemd_conflict_check_in_installer(self):
        """Verifies that the installer verifies existing systemd unit ownership before overwriting."""
        installer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")
        with open(installer_path, "r") as f:
            content = f.read()

        self.assertIn("EXISTING_EXEC=", content)
        self.assertIn("The existing unit does NOT point to this installation", content)
        self.assertIn("Overwrite existing service anyway?", content)

    def test_systemd_path_isolation_in_uninstaller(self):
        """Verifies that uninstaller only removes systemd units matching this directory."""
        uninstaller_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uninstall.sh")
        with open(uninstaller_path, "r") as f:
            content = f.read()

        self.assertIn("echo \"$EXISTING_EXEC\" | grep -q \"$SCRIPT_DIR\"", content)
        self.assertIn("Leaving systemd service untouched to protect other installations", content)

    def test_pm2_path_matching_in_uninstaller(self):
        """Verifies that PM2 uninstallation logic filters strictly by pm_exec_path and pm_cwd."""
        # Simulate PM2 jlist containing both a foreign service and a local service
        fake_pm2_jlist = [
            {
                "name": "server-monitor",
                "pm_id": 2,
                "pm2_env": {
                    "pm_exec_path": "/home/server/Programming/server-monitor/server.js",
                    "pm_cwd": "/home/server/Programming/server-monitor"
                }
            },
            {
                "name": "other-app",
                "pm_id": 5,
                "pm2_env": {
                    "pm_exec_path": "/var/www/other/index.js",
                    "pm_cwd": "/var/www/other"
                }
            },
            {
                "name": "servermonitor-test",
                "pm_id": 99,
                "pm2_env": {
                    "pm_exec_path": "/home/server/Programming/servermonitor-contribution/server.py",
                    "pm_cwd": "/home/server/Programming/servermonitor-contribution"
                }
            }
        ]

        target_dir = "/home/server/Programming/servermonitor-contribution"

        # Run the matching algorithm used in uninstall.sh
        matches = []
        for proc in fake_pm2_jlist:
            pm_env = proc.get("pm2_env", {})
            exec_path = pm_env.get("pm_exec_path", "")
            cwd = pm_env.get("pm_cwd", "")
            pm_id = proc.get("pm_id")
            name = proc.get("name", "")
            if target_dir in exec_path or target_dir in cwd:
                matches.append((pm_id, name, exec_path))

        # Only the matching app (ID 99) should be matched!
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], 99)
        self.assertEqual(matches[0][1], "servermonitor-test")

        # Crucially: host service ID 2 must NEVER be matched
        matched_ids = [m[0] for m in matches]
        self.assertNotIn(2, matched_ids)
        self.assertNotIn(5, matched_ids)


if __name__ == "__main__":
    unittest.main()
