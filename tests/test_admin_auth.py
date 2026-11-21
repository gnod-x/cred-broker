"""Session revocation, bounds and rate windows without network or real secrets."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cred_broker.admin_auth import AdminAuth, LOGIN_WINDOW, MAX_SESSIONS, PEER_ATTEMPTS, SESSION_TTL, TOTAL_ATTEMPTS


class AdminAuthTest(unittest.TestCase):
    def test_sessions_expire_rotate_revoke_and_store_hashes(self):
        auth = AdminAuth()
        with patch("cred_broker.admin_auth.time.monotonic", return_value=100):
            first = auth.issue()
            self.assertTrue(auth.valid(first))
            self.assertNotIn(first, str(auth._sessions))
            second = auth.issue(previous=first)
            self.assertFalse(auth.valid(first))
            self.assertTrue(auth.valid(second))
            auth.revoke(second)
            self.assertFalse(auth.valid(second))
            expiring = auth.issue()
        with patch("cred_broker.admin_auth.time.monotonic", return_value=100 + SESSION_TTL):
            self.assertFalse(auth.valid(expiring))
        self.assertFalse(AdminAuth().valid(expiring))

    def test_session_capacity_and_attempt_windows_are_bounded(self):
        auth = AdminAuth()
        first = auth.issue()
        for _ in range(MAX_SESSIONS):
            auth.issue()
        self.assertEqual(len(auth._sessions), MAX_SESSIONS)
        self.assertFalse(auth.valid(first))
        with patch("cred_broker.admin_auth.time.monotonic", return_value=100):
            for _ in range(PEER_ATTEMPTS):
                self.assertTrue(auth.admit_login("fixture-peer"))
            self.assertFalse(auth.admit_login("fixture-peer"))
            for index in range(TOTAL_ATTEMPTS - PEER_ATTEMPTS):
                self.assertTrue(auth.admit_login(str(index)))
            self.assertFalse(auth.admit_login("fresh-peer"))
            self.assertEqual(len(auth._attempts), TOTAL_ATTEMPTS)
        with patch("cred_broker.admin_auth.time.monotonic", return_value=100 + LOGIN_WINDOW):
            self.assertTrue(auth.admit_login("fixture-peer"))


if __name__ == "__main__":
    unittest.main()
