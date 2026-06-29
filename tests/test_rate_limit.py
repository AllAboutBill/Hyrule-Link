import unittest
from types import SimpleNamespace

from server.rate_limit import RateLimiter, client_key


class RateLimiterTests(unittest.TestCase):
    def test_enforces_and_expires_window(self):
        limiter = RateLimiter(2, 10)
        self.assertTrue(limiter.allow("client", now=0))
        self.assertTrue(limiter.allow("client", now=1))
        self.assertFalse(limiter.allow("client", now=2))
        self.assertTrue(limiter.allow("client", now=11))

    def test_clients_are_independent(self):
        limiter = RateLimiter(1, 10)
        self.assertTrue(limiter.allow("a", now=0))
        self.assertFalse(limiter.allow("a", now=1))
        self.assertTrue(limiter.allow("b", now=1))

    def test_proxy_uses_appended_address_not_spoofed_prefix(self):
        request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            headers={"x-forwarded-for": "spoofed, 203.0.113.7"},
        )
        self.assertEqual(client_key(request), "203.0.113.7")


if __name__ == "__main__":
    unittest.main()
