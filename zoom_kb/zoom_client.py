from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import ZOOM_API_BASE, ZOOM_TOKEN_URL
from .token_store import TokenStore


class ZoomAPIError(RuntimeError):
    def __init__(self, status: int, message: str, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def rfc3339_seconds(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ZoomClient:
    def __init__(self, token_store: TokenStore, client_id: str):
        self.token_store = token_store
        self.client_id = client_id

    def _refresh(self) -> str:
        tokens = self.token_store.load()
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise ZoomAPIError(401, "Zoom authorization must be renewed.")
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }).encode()
        request = urllib.request.Request(ZOOM_TOKEN_URL, data=body, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                refreshed = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ZoomAPIError(exc.code, "Zoom token refresh failed.") from exc
        if "refresh_token" not in refreshed:
            refreshed["refresh_token"] = refresh_token
        self.token_store.save(refreshed)
        return refreshed["access_token"]

    def request_json(self, path: str, params: dict[str, Any] | None = None, retry: bool = True) -> dict[str, Any]:
        url = f"{ZOOM_API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        token = self.token_store.load().get("access_token")
        request = urllib.request.Request(url)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and retry:
                self._refresh()
                return self.request_json(path, params, retry=False)
            try:
                detail = json.loads(exc.read().decode("utf-8", "replace"))
            except (ValueError, OSError):
                detail = {}
            message = detail.get("message", f"Zoom API returned HTTP {exc.code}.")
            raise ZoomAPIError(exc.code, message, detail.get("code")) from exc
        except urllib.error.URLError as exc:
            raise ZoomAPIError(503, "Could not reach Zoom API.") from exc

    def iter_channels(self) -> Iterator[dict[str, Any]]:
        token = ""
        while True:
            params = {"page_size": 50}
            if token:
                params["next_page_token"] = token
            page = self.request_json("/chat/users/me/channels", params)
            yield from page.get("channels", [])
            token = page.get("next_page_token", "")
            if not token:
                break

    def iter_messages(self, channel_id: str, start: datetime, end: datetime, max_pages: int | None = None) -> Iterator[dict[str, Any]]:
        token = ""
        page_count = 0
        while True:
            params = {
                "page_size": 50,
                "from": rfc3339_seconds(start),
                "to": rfc3339_seconds(end),
            }
            if token:
                params["next_page_token"] = token
            page = self.request_json(f"/chat/users/me/messages", {**params, "to_channel": channel_id})
            yield from page.get("messages", [])
            page_count += 1
            token = page.get("next_page_token", "")
            if not token or (max_pages and page_count >= max_pages):
                break

