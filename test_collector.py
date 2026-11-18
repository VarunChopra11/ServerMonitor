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

    def test_multiple_tabs_and_disconnected_sessions(self):
        self.collector.start()

        # Two tabs active simultaneously
        self.collector.touch_session("tab-1")
        self.collector.touch_session("tab-2")
        self.assertTrue(self.collector.is_active())

        # Tab 1 closes or becomes hidden
        self.collector.unregister_session("tab-1")
        # Collector must still be active because tab-2 is connected
        self.assertTrue(self.collector.is_active())

        # Tab 2 unregisters / closes
        self.collector.unregister_session("tab-2")
        # Now no sessions active -> collector becomes idle
        self.assertFalse(self.collector.is_active())
        self.assertEqual(self.collector.get_state(), "idle")

        # Tab returns -> immediate resumption
        self.collector.touch_session("tab-1")
        self.assertTrue(self.collector.is_active())
        self.assertEqual(self.collector.get_state(), "active")

    def test_frontend_html_inactivity_and_sparkline_fixes(self):
        html = server.HTML
        # Must check document.hidden inside update() to stop background tab polling
        self.assertIn("if (document.hidden) return;", html)
        self.assertIn("visibilitychange", html)
        # Sparkline area must not have invalid CSS ${color}15 concatenation
        self.assertNotIn("${color}15", html)
        self.assertIn('fill-opacity="0.15"', html)


if __name__ == "__main__":
    unittest.main()
