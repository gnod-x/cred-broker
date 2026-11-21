"""Bounded, process-local browser sessions and login attempt limits."""
from collections import deque
import hashlib
import secrets
import threading
import time

SESSION_TTL = 8 * 3600
LOGIN_WINDOW = 60
PEER_ATTEMPTS = 8
TOTAL_ATTEMPTS = 64
MAX_SESSIONS = 128


class AdminAuth:
    def __init__(self):
        self._guard = threading.Lock()
        self._sessions = {}
        self._attempts = deque(maxlen=TOTAL_ATTEMPTS)

    @staticmethod
    def _hash(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    def admit_login(self, peer):
        now = time.monotonic()
        with self._guard:
            while self._attempts and self._attempts[0][0] <= now - LOGIN_WINDOW:
                self._attempts.popleft()
            if (len(self._attempts) >= TOTAL_ATTEMPTS
                    or sum(source == peer for _, source in self._attempts) >= PEER_ATTEMPTS):
                return False
            self._attempts.append((now, peer))
            return True

    def issue(self, previous=""):
        raw = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._guard:
            self._sessions = {key: expires for key, expires in self._sessions.items() if expires > now}
            if previous:
                self._sessions.pop(self._hash(previous), None)
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.get)
                del self._sessions[oldest]
            self._sessions[self._hash(raw)] = now + SESSION_TTL
        return raw

    def valid(self, raw):
        if not raw or len(raw) > 256:
            return False
        with self._guard:
            digest = self._hash(raw)
            expires = self._sessions.get(digest, 0)
            if expires <= time.monotonic():
                self._sessions.pop(digest, None)
                return False
            return True

    def revoke(self, raw):
        if raw and len(raw) <= 256:
            with self._guard:
                self._sessions.pop(self._hash(raw), None)
