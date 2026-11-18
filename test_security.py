import json
import time
import unittest
from unittest.mock import patch, MagicMock

import server


class TestSecurityAndAuth(unittest.TestCase):

    def test_verify_token_constant_time(self):
        # Empty token means auth not required
        self.assertTrue(server.verify_token("", ""))
        self.assertTrue(server.verify_token("", "any-client-token"))

        # Valid token comparison
        secret = "super-secret-production-token-12345"
        self.assertTrue(server.verify_token(secret, "super-secret-production-token-12345"))
        self.assertFalse(server.verify_token(secret, "wrong-token"))
        self.assertFalse(server.verify_token(secret, ""))
        self.assertFalse(server.verify_token(secret, None))

    def test_extract_token_from_headers(self):
        # Bearer token
        headers_bearer = {"authorization": "Bearer secret-xyz-987"}
        self.assertEqual(server.extract_token(headers_bearer, ""), "secret-xyz-987")

        # X-Monitor-Token header
        headers_custom = {"x-monitor-token": "custom-token-555"}
        self.assertEqual(server.extract_token(headers_custom, ""), "custom-token-555")

        # Query param
        self.assertEqual(server.extract_token({}, "/api/status?token=query-token-111"), "query-token-111")

        # Missing token
        self.assertEqual(server.extract_token({}, "/api/status"), "")

    def test_speedtest_lock_and_cooldown(self):
        server._speedtest_lock = server.threading.Lock()
        server._speedtest_last_run = 0.0
        server._speedtest_cooldown = 60.0

        # Run test with mock
        with patch("server.socket.create_connection"), \
             patch("server.urllib.request.urlopen") as mock_urlopen:
            mock_dl_resp = MagicMock()
            mock_dl_resp.read.side_effect = [b"0" * 1024, b""]
            mock_ul_resp = MagicMock()
            mock_ul_resp.read.return_value = b"OK"
            mock_urlopen.side_effect = [
                MagicMock(__enter__=MagicMock(return_value=mock_dl_resp)),
                MagicMock(__enter__=MagicMock(return_value=mock_ul_resp))
            ]

            # First run should succeed
            res1 = server.run_speed_test()
            self.assertTrue(res1["success"])

            # Immediate second run must trigger cooldown
            res2 = server.run_speed_test()
            self.assertFalse(res2["success"])
            self.assertIn("Cooldown active", res2["error"])

    def test_security_headers_present(self):
        headers = server.get_security_headers()
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("X-Content-Type-Options", headers)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("X-Frame-Options", headers)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("Referrer-Policy", headers)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_rate_limiter(self):
        limiter = server.RateLimiter(max_requests=5, window_seconds=1.0)
        client_ip = "192.168.1.100"

        for _ in range(5):
            self.assertTrue(limiter.allow_request(client_ip))

        # 6th request within window must be rejected
        self.assertFalse(limiter.allow_request(client_ip))

        # Other IP should still be allowed
        self.assertTrue(limiter.allow_request("10.0.0.1"))


if __name__ == "__main__":
    unittest.main()
