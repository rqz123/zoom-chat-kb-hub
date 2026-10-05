import base64
import hashlib
import io
import json
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from zoom_kb.oauth import ZoomOAuth, oauth_router


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.exists.return_value = False
        self.flow = ZoomOAuth(self.store, "test-client", "http://localhost:8765/oauth/zoom/callback")

    def test_pkce_browser_binding_and_replay(self):
        url, browser = self.flow.begin()
        params = parse_qs(urlsplit(url).query)
        state = params["state"][0]
        with self.assertRaises(HTTPException):
            self.flow.consume(state, "different-browser")
        verifier = self.flow.consume(state, browser)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(params["code_challenge"], [challenge])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        with self.assertRaises(HTTPException):
            self.flow.consume(state, browser)

    def test_expired_state(self):
        url, browser = self.flow.begin()
        state = parse_qs(urlsplit(url).query)["state"][0]
        with patch("zoom_kb.oauth.time.monotonic", return_value=time.monotonic() + 601):
            with self.assertRaises(HTTPException):
                self.flow.consume(state, browser)

    def test_exchange_saves_tokens_and_sends_verifier(self):
        payload = {"access_token": "test-access", "refresh_token": "test-refresh"}
        with patch("zoom_kb.oauth.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as send:
            self.flow.exchange("test-code", "test-verifier")
        form = parse_qs(send.call_args.args[0].data.decode())
        self.assertEqual(form["code_verifier"], ["test-verifier"])
        self.assertEqual(form["client_id"], ["test-client"])
        self.store.save.assert_called_once_with(payload)

    def test_failed_exchange_preserves_existing_tokens(self):
        with patch("zoom_kb.oauth.urllib.request.urlopen", side_effect=urllib.error.URLError("private detail")):
            with self.assertRaises(HTTPException) as result:
                self.flow.exchange("test-code", "test-verifier")
        self.assertNotIn("private detail", result.exception.detail)
        self.store.save.assert_not_called()

    def test_exchange_reports_safe_zoom_error_and_guidance(self):
        response = io.BytesIO(json.dumps({
            "code": 4709,
            "reason": "Invalid redirect: secret-code-must-not-be-repeated",
        }).encode())
        failure = urllib.error.HTTPError(
            "https://zoom.us/oauth/token", 400, "Bad Request", {}, response
        )
        with patch("zoom_kb.oauth.urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(HTTPException) as result:
                self.flow.exchange("secret-code-must-not-be-repeated", "test-verifier")
        self.assertIn("code 4709", result.exception.detail)
        self.assertIn("Register the exact redirect URL", result.exception.detail)
        self.assertNotIn("secret-code-must-not-be-repeated", result.exception.detail)
        self.store.save.assert_not_called()

    def test_callback_cookie_and_single_use(self):
        app = FastAPI()
        with patch.dict("os.environ", {"ZOOM_REDIRECT_URI": "http://localhost:8765/oauth/zoom/callback"}):
            app.include_router(oauth_router(self.store, "test-client"))
        client = TestClient(app, base_url="http://localhost:8765", follow_redirects=False)
        start = client.get("/oauth/zoom/start")
        self.assertIn("HttpOnly", start.headers["set-cookie"])
        self.assertIn("SameSite=lax", start.headers["set-cookie"])
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        with patch.object(ZoomOAuth, "exchange") as exchange:
            callback = client.get("/oauth/zoom/callback", params={"code": "test-code", "state": state})
            self.assertEqual(callback.status_code, 200)
            self.assertEqual(callback.headers["referrer-policy"], "no-referrer")
            exchange.assert_called_once()
            replay = client.get("/oauth/zoom/callback", params={"code": "test-code", "state": state})
            self.assertEqual(replay.status_code, 400)
            self.assertEqual(exchange.call_count, 1)

    def test_non_loopback_redirect_rejected(self):
        with patch.dict("os.environ", {"ZOOM_REDIRECT_URI": "https://example.com/callback"}):
            with self.assertRaises(ValueError):
                oauth_router(self.store, "test-client")

    def test_default_redirect_uses_numeric_loopback(self):
        with patch.dict("os.environ", {}, clear=True):
            app = FastAPI()
            app.include_router(oauth_router(self.store, "test-client"))
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        status = client.get("/api/auth/zoom")
        self.assertEqual(status.json()["redirect_uri"], "http://127.0.0.1:8765/oauth/zoom/callback")


if __name__ == "__main__":
    unittest.main()
