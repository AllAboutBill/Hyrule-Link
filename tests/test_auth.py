import importlib
import os
import time
import unittest
from unittest.mock import patch

from server import auth


class AuthTests(unittest.TestCase):
    def test_sessions_fail_closed_without_secret(self):
        with patch.object(auth, "SESSION_SECRET", ""), patch.object(auth, "TOKENS_ENABLED", False):
            self.assertIsNone(auth.unsign("anything.at-all"))
            with self.assertRaises(RuntimeError):
                auth.sign({"admin": True, "exp": time.time() + 60})

    def test_signed_session_round_trip(self):
        with patch.object(auth, "SESSION_SECRET", "test-secret"), patch.object(auth, "TOKENS_ENABLED", True):
            token = auth.sign({"admin": True, "exp": time.time() + 60})
            self.assertTrue(auth.unsign(token)["admin"])

    def test_discord_login_is_disabled_with_a_weak_secret(self):
        values = {
            "DISCORD_CLIENT_ID": "client", "DISCORD_CLIENT_SECRET": "discord-secret",
            "DISCORD_REDIRECT_URI": "https://example.test/auth", "SESSION_SECRET": "short",
        }
        with patch.dict(os.environ, values):
            importlib.reload(auth)
            self.assertFalse(auth.LOGIN_ENABLED)
        importlib.reload(auth)


if __name__ == "__main__":
    unittest.main()
