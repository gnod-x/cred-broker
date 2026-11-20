import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from cred_broker import google_oauth as oauth, provider_gmail as gmail


def encoded(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


class GmailTests(unittest.TestCase):
    settings = {"email": "reader@example.test"}
    secret = {"client_id": "fake-client", "client_secret": "fake-secret", "refresh_token": "fake-refresh"}

    def test_ephemeral_token_is_removed_from_all_returned_strings(self):
        token = "fake-ephemeral-access"
        with patch.object(gmail, "google_json", side_effect=[
            {"access_token": token}, {"emailAddress": self.settings["email"]},
            {"snippet": token, "payload": {"mimeType": "text/plain",
             "headers": [{"name": "subject", "value": token}],
             "body": {"data": encoded("ordinary token field " + token)}}},
        ]):
            result = gmail.call("read", self.settings["email"], {"message_id": "abc"}, self.settings, self.secret)
            self.assertNotIn(token, json.dumps(result))
            self.assertEqual(result["body"], "ordinary token field ***")
        with patch.object(gmail, "google_json", side_effect=[
            {"access_token": token}, {"emailAddress": self.settings["email"]},
            {"messages": [{"id": token, "threadId": token}]},
        ]):
            result = gmail.call("search", self.settings["email"], {"query": "x"}, self.settings, self.secret)
            self.assertEqual(result["messages"], [{"id": "***", "thread_id": "***"}])
        with patch.object(gmail, "google_json", side_effect=[
            {"access_token": "reader"}, {"emailAddress": self.settings["email"]},
        ]):
            self.assertEqual(gmail.test_connection(self.settings, self.secret), {"email": "***@example.test"})

    def test_scope(self):
        self.assertEqual(gmail.access_from({"access_token": "fake", "scope": gmail.SCOPE}), "fake")
        self.assertEqual(gmail.access_from({"access_token": "fake"}), "fake")
        for scope in ("https://mail.google.com/", gmail.SCOPE + " openid", ""):
            with self.assertRaises(RuntimeError):
                gmail.access_from({"access_token": "fake", "scope": scope})

    def test_search_checks_mailbox_and_encodes_query(self):
        with patch.object(gmail, "google_json", side_effect=[
            {"access_token": "fake-access", "scope": gmail.SCOPE},
            {"emailAddress": self.settings["email"]},
            {"messages": [{"id": "abc", "threadId": "t", "secret": "ignored"}]},
        ]) as request:
            result = gmail.call("search", self.settings["email"], {"query": "a&maxResults=500", "limit": 1}, self.settings, self.secret)
            self.assertEqual(result, {"messages": [{"id": "abc", "thread_id": "t"}], "count": 1})
            self.assertEqual(request.call_args_list[0].kwargs["forms"]["grant_type"], "refresh_token")
            self.assertNotIn("scope", request.call_args_list[0].kwargs["forms"])
            self.assertEqual(parse_qs(urlsplit(request.call_args.args[0]).query)["q"], ["a&maxResults=500"])

    def test_rejects_path_and_mailbox_escalation_before_reads(self):
        with patch.object(gmail, "google_json") as request:
            for mid in ("../profile", "a?format=raw", "https://attacker.test", "a/b"):
                with self.assertRaises(ValueError):
                    gmail.call("read", self.settings["email"], {"message_id": mid}, self.settings, self.secret)
            with self.assertRaises(ValueError):
                gmail.call("search", "other@example.test", {"query": "x"}, self.settings, self.secret)
            request.assert_not_called()
        with patch.object(gmail, "google_json", side_effect=[
            {"access_token": "fake-access"}, {"emailAddress": "other@example.test"},
        ]) as request:
            with self.assertRaises(RuntimeError):
                gmail.call("read", self.settings["email"], {"message_id": "abc"}, self.settings, self.secret)
            self.assertEqual(request.call_count, 2)

    def test_bodies_exclude_attachment_children_and_scripts(self):
        payload = {"mimeType": "multipart/mixed", "parts": [
            {"filename": "forward.eml", "parts": [{"mimeType": "text/plain", "body": {"data": encoded("attachment secret")}}]},
            {"mimeType": "text/html", "body": {"data": encoded("<head>hidden</head><p>Hello</p><script>evil()</script><style>bad</style>")}},
        ]}
        self.assertEqual(gmail._message_body(payload), "Hello")
        payload["parts"].append({"mimeType": "text/plain", "body": {"data": encoded("Plain" * 10000)}})
        self.assertEqual(gmail._message_body(payload), ("Plain" * 10000)[:gmail.MAX_BODY])

    def test_http_errors_are_safe_and_response_is_bounded(self):
        with patch.object(gmail, "build_opener") as build:
            build.return_value.open.side_effect = URLError("fake-secret")
            with self.assertRaises(RuntimeError) as caught:
                gmail.google_json(gmail.TOKEN_URL)
            self.assertNotIn("fake-secret", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertTrue(any(isinstance(handler, gmail.NoRedirect) for handler in build.call_args.args))
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"x" * (gmail.MAX_RESPONSE + 1)
        with patch.object(gmail, "build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaises(RuntimeError):
                gmail.google_json(gmail.API + "/profile")
            response.read.assert_called_once_with(gmail.MAX_RESPONSE + 1)


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.control = Mock()
        oauth.register_google_oauth(self.app, self.control, lambda: None,
            oauth.GoogleSettings("fake-client", "fake-secret", "https://broker.example.test" + oauth.CALLBACK))
        self.routes = {route.path: route.endpoint for route in self.app.routes}

    def request(self, cookie=""):
        return Request({"type": "http", "headers": [(b"cookie", cookie.encode())]})

    def start(self):
        response = self.routes["/admin/oauth/google/start"](self.request(), label="Test")
        query = parse_qs(urlsplit(response.headers["location"]).query)
        cookie = response.headers["set-cookie"].split(";")[0]
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        self.assertIn("Secure", response.headers["set-cookie"])
        return query, cookie

    def test_pkce_readonly_scope_success_and_replay(self):
        query, cookie = self.start()
        self.assertEqual(query["scope"], [gmail.SCOPE])
        with patch.object(gmail, "google_json", side_effect=[
            {"access_token": "fake-access", "refresh_token": "fake-refresh", "scope": gmail.SCOPE},
            {"emailAddress": "reader@example.test"},
        ]) as request:
            response = self.routes[oauth.CALLBACK](self.request(cookie), state=query["state"][0], code="fake-code", error="")
            verifier = request.call_args_list[0].kwargs["forms"]["code_verifier"]
            expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            self.assertEqual(query["code_challenge"], [expected])
            self.assertEqual(response.headers["location"], "/google/complete?google=connected")
            self.control.create_connection.assert_called_once()
            with self.assertRaises(HTTPException):
                self.routes[oauth.CALLBACK](self.request(cookie), state=query["state"][0], code="fake-code", error="")
        landing = self.routes["/google/complete"](google='"><script>')
        self.assertNotIn(b"<script>", landing.body)
        self.assertIn(b"/admin?google=failed", landing.body)

    def test_cookie_expiry_and_failure(self):
        query, cookie = self.start()
        with self.assertRaises(HTTPException):
            self.routes[oauth.CALLBACK](self.request("cb_google_flow=wrong"), state=query["state"][0], code="fake-code", error="")
        with patch.object(oauth.time, "monotonic", return_value=10**20):
            with self.assertRaises(HTTPException):
                self.routes[oauth.CALLBACK](self.request(cookie), state=query["state"][0], code="fake-code", error="")
        query, cookie = self.start()
        with patch.object(gmail, "google_json", side_effect=RuntimeError("fake-secret")):
            response = self.routes[oauth.CALLBACK](self.request(cookie), state=query["state"][0], code="fake-code", error="")
            self.assertEqual(response.headers["location"], "/google/complete?google=failed")
            self.assertNotIn("fake-secret", str(response.headers))
        self.control.create_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
