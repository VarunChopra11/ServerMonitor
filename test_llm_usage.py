import json
import unittest
from unittest.mock import patch, MagicMock
import subprocess

import server

SAMPLE_AGY_OUTPUT = {
    "conversation_id": "",
    "status": "SUCCESS",
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.941534,
                            "reset_time": "2026-10-24T15:45:48Z"
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.94585,
                            "reset_time": "2026-10-19T16:26:17Z"
                        }
                    ]
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.72711,
                            "reset_time": "2026-10-24T17:40:47Z"
                        },
                        {
                            "id": "3p-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.63668,
                            "reset_time": "2026-10-19T16:48:47Z"
                        }
                    ]
                }
            ]
        }
    }
}

class TestLlmUsageBackend(unittest.TestCase):
    def setUp(self):
        self.old_config = server._global_config
        cfg = server.Config()
        cfg.data["modules"]["antigravity"] = True
        server._global_config = cfg

    def tearDown(self):
        server._global_config = self.old_config

    @patch("server.subprocess.run")
    def test_run_llm_usage_success(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps(SAMPLE_AGY_OUTPUT)
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        result = server.run_llm_usage()
        self.assertTrue(result["success"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["groups"]), 2)

        # Check Gemini group
        gemini = result["groups"][0]
        self.assertEqual(gemini["name"], "Gemini Models")
        self.assertEqual(len(gemini["buckets"]), 2)
        self.assertEqual(gemini["buckets"][0]["percentage"], 94.2)
        self.assertEqual(gemini["buckets"][1]["percentage"], 94.6)
        self.assertEqual(gemini["buckets"][0]["reset_time"], "2026-10-24T15:45:48Z")

        # Check Claude group
        claude = result["groups"][1]
        self.assertEqual(claude["name"], "Claude and GPT models")
        self.assertEqual(claude["buckets"][0]["percentage"], 72.7)
        self.assertEqual(claude["buckets"][1]["percentage"], 63.7)

    @patch("server.subprocess.run")
    def test_run_llm_usage_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["agy"], timeout=30)
        result = server.run_llm_usage()
        self.assertFalse(result["success"])
        self.assertIn("timed out", result["error"])

    @patch("server.subprocess.run")
    def test_run_llm_usage_non_zero_exit(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Permission denied"
        mock_run.return_value = mock_proc

        result = server.run_llm_usage()
        self.assertFalse(result["success"])
        self.assertIn("Permission denied", result["error"])

    @patch("server.subprocess.run")
    def test_run_llm_usage_plain_text_fallback(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Gemini Models\tWeekly Limit Remaining\t67.2%\t2026-11-25T13:46:29Z\nGemini Models\tFive Hour Limit Remaining\t99.0%\t2026-11-19T16:28:21Z\n"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        result = server.run_llm_usage()
        self.assertTrue(result["success"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["name"], "Gemini Models")
        self.assertEqual(len(result["groups"][0]["buckets"]), 2)
        self.assertEqual(result["groups"][0]["buckets"][0]["percentage"], 67.2)

    @patch("server.subprocess.run")
    def test_run_llm_usage_malformed_json(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Not valid json!"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        result = server.run_llm_usage()
        self.assertFalse(result["success"])
        self.assertIn("Failed to parse", result["error"])

    def test_html_contains_llm_usage_card(self):
        self.assertIn('LLM Quota', server.HTML)
        self.assertIn('id="llmusage-button"', server.HTML)
        self.assertIn('id="llm-results"', server.HTML)
        self.assertIn('runLlmUsage()', server.HTML)

    def test_get_status_does_not_contain_llm(self):
        status = server.get_status()
        self.assertNotIn("llm", status)
        self.assertNotIn("groups", status)

if __name__ == '__main__':
    unittest.main()
