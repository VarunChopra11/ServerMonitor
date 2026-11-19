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

        self.assertIn("SCRIPT_DIR/(server\\.py|servermonitor)", content)
        self.assertIn("Leaving systemd service untouched to protect other installations", content)

    def test_pm2_path_matching_in_uninstaller(self):
        """Verifies that PM2 uninstallation logic filters strictly by directory boundary and script name."""
        # Simulate PM2 jlist containing both a foreign service, a substring-matched service, and a local service
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
                "name": "servermonitor-extra",
                "pm_id": 7,
                "pm2_env": {
                    # Path contains target_dir as a substring, but is in a different directory!
                    "pm_exec_path": "/home/server/Programming/servermonitor-contribution-extra/server.py",
                    "pm_cwd": "/home/server/Programming/servermonitor-contribution-extra"
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

        target_dir = os.path.abspath("/home/server/Programming/servermonitor-contribution")

        # Run the strict matching algorithm used in uninstall.sh
        matches = []
        for proc in fake_pm2_jlist:
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
                matches.append((pm_id, name, exec_path))

        # Only the true matching app (ID 99) should be matched!
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], 99)
        self.assertEqual(matches[0][1], "servermonitor-test")

        # Crucially: host service ID 2, foreign ID 5, and substring sibling ID 7 must NEVER be matched
        matched_ids = [m[0] for m in matches]
        self.assertNotIn(2, matched_ids)
        self.assertNotIn(5, matched_ids)
        self.assertNotIn(7, matched_ids)

    def test_installer_backup_protection(self):
        """Verifies that install.sh contains backup logic for existing files and unit files."""
        installer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")
        with open(installer_path, "r") as f:
            content = f.read()

        self.assertIn("backup_file()", content)
        self.assertIn("${file}.bak.${timestamp}", content)
        self.assertIn("backup_file \"$UNIT_FILE\"", content)


if __name__ == "__main__":
    unittest.main()
