import os
import unittest
from unittest.mock import patch, mock_open, MagicMock

import server


class TestHardwareDetection(unittest.TestCase):

    def test_cpu_architecture(self):
        arch = server.get_cpu_architecture()
        self.assertIsInstance(arch, str)
        self.assertGreater(len(arch), 0)

    @patch("server.read_file")
    def test_cpu_model_x86(self, mock_read):
        sample_cpuinfo = """processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 85
model name\t: Intel(R) Xeon(R) Platinum 8175M CPU @ 2.50GHz
stepping\t: 4
microcode\t: 0x2000065
cpu MHz\t\t: 2500.000
cache size\t: 33792 KB
"""
        mock_read.side_effect = lambda p: sample_cpuinfo if p == "/proc/cpuinfo" else ""
        model = server.get_cpu_model()
        self.assertIn("Intel(R) Xeon(R)", model)

    @patch("server.read_file")
    @patch("server.os.path.exists")
    def test_cpu_model_arm_soc(self, mock_exists, mock_read):
        mock_exists.side_effect = lambda p: p == "/sys/devices/soc0/machine"
        mock_read.side_effect = lambda p: "Samsung A7Y18LTE EUR OPEN rev02 board based on EXYNOS7885\n" if p == "/sys/devices/soc0/machine" else ""
        model = server.get_cpu_model()
        self.assertIn("EXYNOS7885", model)

    @patch("server.read_file")
    def test_cpu_steal_and_per_core(self, mock_read):
        stat_sample_1 = """cpu  1000 0 500 8000 100 0 50 200 0 0
cpu0 500 0 250 4000 50 0 25 100 0 0
cpu1 500 0 250 4000 50 0 25 100 0 0
"""
        stat_sample_2 = """cpu  1100 0 550 8100 110 0 55 250 0 0
cpu0 550 0 275 4050 55 0 27 125 0 0
cpu1 550 0 275 4050 55 0 28 125 0 0
"""
        # Reset collector state
        server._previous_cpu_detailed = None

        mock_read.return_value = stat_sample_1
        stats1 = server.get_cpu_stats()
        self.assertIsInstance(stats1, dict)
        self.assertIn("usage", stats1)
        self.assertIn("steal", stats1)
        self.assertIn("per_core", stats1)

        mock_read.return_value = stat_sample_2
        stats2 = server.get_cpu_stats()
        # total delta: 100+0+50+100+10+0+5+50 = 315
        # idle delta: 100 + 10 = 110
        # usage = (1 - 110/315)*100 = 65.1%
        # steal delta: 50
        # steal % = (50 / 315) * 100 = 15.9%
        self.assertGreater(stats2["usage"], 50)
        self.assertGreater(stats2["steal"], 10)
        self.assertEqual(len(stats2["per_core"]), 2)

    @patch("server.read_file")
    @patch("server.os.path.exists")
    def test_memory_cgroup_v2(self, mock_exists, mock_read):
        meminfo = """MemTotal:       16384000 kB
MemFree:         4096000 kB
MemAvailable:    8192000 kB
SwapTotal:       2048000 kB
SwapFree:        1024000 kB
"""
        def fake_exists(p):
            return p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current")

        def fake_read(p):
            if p == "/proc/meminfo":
                return meminfo
            if p == "/sys/fs/cgroup/memory.max":
                return "4294967296\n"  # 4 GB container limit
            if p == "/sys/fs/cgroup/memory.current":
                return "1073741824\n"  # 1 GB used
            return ""

        mock_exists.side_effect = fake_exists
        mock_read.side_effect = fake_read

        mem = server.get_memory()
        self.assertEqual(mem["total"], 16384000 * 1024)
        self.assertIn("container", mem)
        self.assertTrue(mem["container"]["active"])
        self.assertEqual(mem["container"]["limit"], 4294967296)
        self.assertEqual(mem["container"]["used"], 1073741824)
        self.assertEqual(mem["container"]["percent"], 25.0)

    @patch("server.read_file")
    @patch("server.os.statvfs")
    def test_storage_mounts_discovery(self, mock_statvfs, mock_read):
        mounts_data = """rootfs / rootfs rw 0 0
sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0
/dev/sdb1 /mnt/data xfs rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev,noexec,relatime,size=819200k,mode=755 0 0
"""
        mock_read.side_effect = lambda p: mounts_data if p == "/proc/mounts" else ""

        fake_stat_root = MagicMock(f_blocks=1000000, f_bavail=400000, f_frsize=4096)
        fake_stat_data = MagicMock(f_blocks=2000000, f_bavail=1500000, f_frsize=4096)

        def fake_stat(p):
            if p == "/":
                return fake_stat_root
            if p == "/mnt/data":
                return fake_stat_data
            raise OSError("not found")

        mock_statvfs.side_effect = fake_stat

        disks = server.get_storage_mounts()
        self.assertEqual(len(disks), 2)
        mount_points = [d["mount"] for d in disks]
        self.assertIn("/", mount_points)
        self.assertIn("/mnt/data", mount_points)

        root_disk = [d for d in disks if d["mount"] == "/"][0]
        self.assertEqual(root_disk["fstype"], "ext4")
        self.assertEqual(root_disk["percent"], 60.0)

    @patch("server.os.path.exists")
    @patch("server.os.listdir")
    @patch("server.read_file")
    def test_thermal_zones_fallback_when_missing(self, mock_read, mock_listdir, mock_exists):
        # Cloud VM with no thermal sensors
        mock_exists.return_value = False
        thermals = server.get_thermal_details()
        self.assertFalse(thermals["available"])
        self.assertIsNone(thermals["temperature"])
        self.assertEqual(thermals["sensors"], [])

    @patch("server.os.path.exists")
    @patch("server.os.listdir")
    @patch("server.read_file")
    def test_thermal_zones_parsing(self, mock_read, mock_listdir, mock_exists):
        mock_exists.side_effect = lambda p: p in ("/sys/class/thermal", "/sys/class/hwmon")
        mock_listdir.side_effect = lambda p: ["thermal_zone0", "thermal_zone1"] if p == "/sys/class/thermal" else []

        def fake_read(p):
            if p == "/sys/class/thermal/thermal_zone0/type":
                return "cpu-thermal\n"
            if p == "/sys/class/thermal/thermal_zone0/temp":
                return "45000\n"  # 45.0 C (millidegrees)
            if p == "/sys/class/thermal/thermal_zone1/type":
                return "gpu-thermal\n"
            if p == "/sys/class/thermal/thermal_zone1/temp":
                return "52000\n"  # 52.0 C
            return ""

        mock_read.side_effect = fake_read

        thermals = server.get_thermal_details()
        self.assertTrue(thermals["available"])
        self.assertEqual(thermals["temperature"], 52.0)
        self.assertEqual(len(thermals["sensors"]), 2)

    @patch("server.read_file")
    @patch("server.os.path.exists")
    @patch("server.os.listdir")
    def test_battery_fallback_when_no_battery(self, mock_listdir, mock_exists, mock_read):
        # Cloud VM / Server without battery
        mock_exists.return_value = True
        mock_listdir.return_value = ["ac", "usb"]
        mock_read.side_effect = lambda p: "Mains" if "ac/type" in p else ("USB" if "usb/type" in p else "")
        bat = server.get_battery()
        self.assertFalse(bat["available"])
        self.assertIsNone(bat["capacity"])

    @patch("server.read_file")
    @patch("server.os.statvfs")
    def test_zfs_nfs_cifs_mount_discovery(self, mock_statvfs, mock_read):
        mounts_data = """rootfs / rootfs rw 0 0
sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
/dev/sda1 / ext4 rw,relatime 0 0
tank/data /mnt/zfs zfs rw,xattr,noacl 0 0
192.168.1.10:/exports/media /mnt/nfs nfs4 rw,relatime 0 0
//nas.local/backups /mnt/smb cifs rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev,mode=755 0 0
cgroup2 /sys/fs/cgroup cgroup2 rw,nosuid,nodev 0 0
"""
        mock_read.side_effect = lambda p: mounts_data if p == "/proc/mounts" else ""

        def fake_stat(p):
            if p in ("/", "/mnt/zfs", "/mnt/nfs", "/mnt/smb"):
                return MagicMock(f_blocks=5000000, f_bavail=3000000, f_frsize=4096)
            raise OSError("no mount")

        mock_statvfs.side_effect = fake_stat

        disks = server.get_storage_mounts()
        mount_points = [d["mount"] for d in disks]
        self.assertIn("/", mount_points)
        self.assertIn("/mnt/zfs", mount_points)
        self.assertIn("/mnt/nfs", mount_points)
        self.assertIn("/mnt/smb", mount_points)

        # Pseudo filesystems must not be included
        self.assertNotIn("/sys", mount_points)
        self.assertNotIn("/proc", mount_points)
        self.assertNotIn("/run", mount_points)
        self.assertNotIn("/sys/fs/cgroup", mount_points)

        # Check types
        zfs_disk = [d for d in disks if d["mount"] == "/mnt/zfs"][0]
        self.assertEqual(zfs_disk["fstype"], "zfs")
        self.assertEqual(zfs_disk["device"], "tank/data")

        nfs_disk = [d for d in disks if d["mount"] == "/mnt/nfs"][0]
        self.assertEqual(nfs_disk["fstype"], "nfs4")

        smb_disk = [d for d in disks if d["mount"] == "/mnt/smb"][0]
        self.assertEqual(smb_disk["fstype"], "cifs")

    @patch("server.open")
    @patch("server.os.path.exists")
    def test_network_virtual_interface_filtering(self, mock_exists, mock_open_fn):
        dev_content = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 100000       10    0    0    0     0          0         0   100000       10    0    0    0     0       0          0
  eth0: 5000000      500    0    0    0     0          0         0  2000000      300    0    0    0     0       0          0
docker0: 5000000      500    0    0    0     0          0         0  2000000      300    0    0    0     0       0          0
veth12: 5000000      500    0    0    0     0          0         0  2000000      300    0    0    0     0       0          0
"""
        from io import StringIO
        mock_open_fn.return_value = StringIO(dev_content)

        # Only eth0 has /sys/class/net/eth0/device
        mock_exists.side_effect = lambda p: p == "/sys/class/net/eth0/device"

        # Reset previous network state
        server._previous_network = None
        server._previous_sample_time = None

        net = server.get_network()

        # The total rx and tx should ONLY count eth0 (5000000 and 2000000)
        # It must NOT sum docker0 and veth12, which would triple-count to 15000000!
        self.assertEqual(net["rx"], 5000000)
        self.assertEqual(net["tx"], 2000000)
        self.assertEqual(net["primary"], "eth0")

        # But per-interface details must still be available
        self.assertIn("docker0", net["interfaces"])
        self.assertIn("veth12", net["interfaces"])
        self.assertTrue(net["interfaces"]["docker0"]["is_virtual"])
        self.assertTrue(net["interfaces"]["veth12"]["is_virtual"])
        self.assertFalse(net["interfaces"]["eth0"]["is_virtual"])

    @patch("server.read_file")
    def test_cpu_guest_time_calculation(self, mock_read):
        # Linux /proc/stat: user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
        # Note: guest (100) is already in user (500), guest_nice (50) is already in nice (200).
        stat_sample_1 = "cpu  500 200 100 4000 50 10 20 50 100 50\n"
        stat_sample_2 = "cpu  600 250 150 4100 60 15 25 60 120 60\n"

        server._previous_cpu = None
        server._previous_cpu_detailed = None

        mock_read.return_value = stat_sample_1
        stats1 = server.get_cpu_stats()

        mock_read.return_value = stat_sample_2
        stats2 = server.get_cpu_stats()

        # Fields 0..7 delta: (600-500) + (250-200) + (150-100) + (4100-4000) + (60-50) + (15-10) + (25-20) + (60-50)
        # = 100 + 50 + 50 + 100 + 10 + 5 + 5 + 10 = 330 ticks.
        # Idle delta: (4100-4000) + (60-50) = 110 ticks.
        # If guest (120-100 = 20) and guest_nice (60-50 = 10) were incorrectly added, total would be 360 ticks.
        # With correct total (330 ticks): usage = (1 - 110/330) * 100 = 66.7%
        self.assertAlmostEqual(stats2["usage"], 66.7, delta=0.5)


if __name__ == "__main__":
    unittest.main()
