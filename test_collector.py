import time
import unittest
from unittest.mock import patch, MagicMock

import server


class TestOnDemandCollector(unittest.TestCase):

    def setUp(self):
        # Create a fresh collector instance with short intervals for testing
        self.collector = server.OnDemandCollector(sample_interval=0.05, inactivity_timeout=0.2)

    def tearDown(self):
        self.collector.stop()

    def test_collector_initially_idle(self):
        self.assertFalse(self.collector.is_active())
        self.assertEqual(self.collector.get_state(), "idle")

    def test_collector_wakes_on_client_touch(self):
        self.collector.start()
        # Touch session
        self.collector.touch_session("session-abc")
        self.assertTrue(self.collector.is_active())
        self.assertEqual(self.collector.get_state(), "active")

    def test_collector_pauses_after_inactivity_timeout(self):
        self.collector.start()
        self.collector.touch_session("session-abc")
        self.assertTrue(self.collector.is_active())

        # Wait past inactivity timeout (0.2s)
        time.sleep(0.35)
        self.assertFalse(self.collector.is_active())
        self.assertEqual(self.collector.get_state(), "idle")

    def test_collector_resumes_immediately_on_new_touch(self):
        self.collector.start()
        self.collector.touch_session("session-1")
        time.sleep(0.35)
        self.assertFalse(self.collector.is_active())

        # Resume
        self.collector.touch_session("session-2")
        self.assertTrue(self.collector.is_active())
        self.assertEqual(self.collector.get_state(), "active")

    def test_bounded_history_ring_buffer(self):
        # Verify history is bounded
        self.collector.start()
        self.collector.touch_session("test")
        time.sleep(0.2)
        history = self.collector.get_history()
        self.assertIn("cpu", history)
        self.assertIn("memory", history)
        self.assertLessEqual(len(history["cpu"]), self.collector.history_maxlen)


if __name__ == "__main__":
    unittest.main()
