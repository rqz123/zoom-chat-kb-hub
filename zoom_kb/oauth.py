"""Local public-client OAuth with PKCE and browser-bound, single-use state."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import ZOOM_TOKEN_URL
from .token_store import TokenStore


def _exchange_error(exc: urllib.error.HTTPError) -> str:
    """Return a useful error without exposing authorization codes or tokens."""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    code = str(payload.get("code", "")).strip()
    reason = next((str(payload.get(key, "")).strip() for key in
                   ("reason", "error_description", "error", "message")
                   if payload.get(key)), "")
    reason = " ".join(reason.split())[:240]

    guidance = {
        "4702": "Zoom rejected the client ID. Enable Public Client OAuth and use the Public Client ID.",
        "4704": "Zoom rejected the client ID. Enable Public Client OAuth and use the Public Client ID.",
        "4706": "Zoom reports missing client credentials. Enable Public Client OAuth and use the Public Client ID.",
        "4709": "Zoom rejected the callback URL. Register the exact redirect URL shown in Settings.",
        "4733": "The authorization code expired. Start authorization again.",
        "4734": "The authorization code was invalid or already used. Start authorization again.",
    }.get(code)
    if guidance:
        return f"Zoom token exchange failed (code {code}). {guidance}"

    details = f"HTTP {exc.code}"
    if code:
        details += f", code {code}"
    if reason:
        details += f": {reason}"
    return f"Zoom token exchange failed ({details}). Check the Public Client ID and callback URL, then start again."


class ZoomOAuth:
    def __init__(self, tokens: TokenStore, client_id: str, redirect_uri: str):
        self.tokens = tokens
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.pending: dict[str, tuple[str, str, float]] = {}
        self.lock = threading.Lock()

    def begin(self) -> tuple[str, str]:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            now = time.monotonic()
            self.pending = {key: value for key, value in self.pending.items() if value[2] > now}
            if len(self.pending) >= 100:
                raise HTTPException(429, "Too many authorization attempts. Try again later.")
            self.pending[state] = (verifier, browser, now + 600)
        url = "https://zoom.us/oauth/authorize?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": self.client_id,
            "redirect_uri": self.redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return url, browser

    def consume(self, state: str, browser: str) -> str:
        with self.lock:
            pending = self.pending.get(state)
            if not pending or pending[2] <= time.monotonic() or not secrets.compare_digest(pending[1], browser):
                raise HTTPException(400, "Authorization expired or browser state did not match. Start again.")
            del self.pending[state]
        return pending[0]

    def exchange(self, code: str, verifier: str) -> None:
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.redirect_uri, "client_id": self.client_id,
            "code_verifier": verifier,
        }).encode()
        request = urllib.request.Request(ZOOM_TOKEN_URL, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise HTTPException(502, _exchange_error(exc)) from None
        except (urllib.error.URLError, ValueError):
            raise HTTPException(502, "Zoom token exchange failed. Start authorization again.") from None
        if not isinstance(payload, dict) or not payload.get("access_token") or not payload.get("refresh_token"):
            raise HTTPException(502, "Zoom did not return the required tokens. Start authorization again.")
        try:
            self.tokens.save(payload)
        except OSError:
            raise HTTPException(500, "Could not save authorization in the Windows token vault.") from None


def oauth_router(tokens: TokenStore, client_id: str) -> APIRouter:
    redirect_uri = os.getenv("ZOOM_REDIRECT_URI", "http://127.0.0.1:8765/oauth/zoom/callback")
    parsed = urllib.parse.urlsplit(redirect_uri)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.port != 8765 or parsed.path != "/oauth/zoom/callback"
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("ZOOM_REDIRECT_URI must be http://localhost:8765/oauth/zoom/callback or its 127.0.0.1 equivalent.")
    flow = ZoomOAuth(tokens, client_id, redirect_uri)
    router = APIRouter()
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    @router.get("/api/auth/zoom")
    def status():
        return {"token_present": tokens.exists(), "client_id": client_id,
                "redirect_uri": redirect_uri, "start_url": f"http://{parsed.netloc}/oauth/zoom/start"}

    @router.get("/oauth/zoom/start", include_in_schema=False)
    def start(request: Request):
        # Set the cookie on the callback host even when the UI uses the other loopback alias.
        if request.url.netloc != parsed.netloc:
            return RedirectResponse(f"http://{parsed.netloc}/oauth/zoom/start", headers=headers)
        url, browser = flow.begin()
        response = RedirectResponse(url, headers=headers)
        response.set_cookie("zoom_oauth_browser", browser, max_age=600, httponly=True,
                            samesite="lax", path="/oauth/zoom")
        return response

    @router.get("/oauth/zoom/callback", include_in_schema=False)
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        verifier = flow.consume(state, request.cookies.get("zoom_oauth_browser", ""))
        if error or not code:
            raise HTTPException(400, "Zoom authorization was not granted. Start again when ready.")
        flow.exchange(code, verifier)
        response = HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8"><title>Zoom authorized</title>'
            '<h1>Zoom authorization completed</h1><p>Your tokens are saved in the Windows token vault.</p>'
            '<p><a href="/">Return to Knowledge Hub</a></p></html>', headers=headers)
        response.delete_cookie("zoom_oauth_browser", path="/oauth/zoom")
        return response

    return router
