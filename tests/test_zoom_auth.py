from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from zoom_kb.db import Database
from zoom_kb.sync import SyncService
from zoom_kb.token_store import TokenStore
from zoom_kb.zoom_client import ZoomAuthError, ZoomClient


class PlainTokenStore(TokenStore):
    """Stores tokens in memory so the tests do not depend on Windows DPAPI."""

    def __init__(self, folder: Path, payload: dict):
        super().__init__(folder / "zoom-token.bin")
        self.payload = payload

    def exists(self) -> bool:
        return True

    def load(self) -> dict:
        return dict(self.payload)

    def save(self, payload: dict) -> None:
        self.payload = dict(payload)
        self.auth_error_path.unlink(missing_ok=True)


def http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://zoom.example", code, "error", {}, io.BytesIO(body))


class ZoomAuthTests(unittest.TestCase):
    def test_revoked_refresh_token_marks_authorization_expired(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PlainTokenStore(Path(folder), {"access_token": "old", "refresh_token": "revoked"})
            client = ZoomClient(store, "client")
            responses = [http_error(401), http_error(400, b'{"error":"invalid_grant"}')]
            with patch("zoom_kb.zoom_client.urllib.request.urlopen", side_effect=responses):
                with self.assertRaises(ZoomAuthError):
                    client.request_json("/chat/users/me/channels")
            self.assertIn("HTTP 400", store.auth_error()["reason"])

            store.save({"access_token": "new", "refresh_token": "fresh"})
            self.assertIsNone(store.auth_error())

    def test_sync_stops_at_first_auth_failure(self):
        class ExpiredClient:
            def __init__(self):
                self.channels = []

            def iter_messages(self, channel_id, start, end, max_pages=None):
                self.channels.append(channel_id)
                raise ZoomAuthError(401, "Zoom authorization has expired.")

        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.initialize()
            with db.connect() as connection:
                for channel_id in ("a", "b", "c"):
                    connection.execute(
                        "INSERT INTO channels(id,name,selected,readability_status,last_seen_at,is_active) VALUES(?,?,1,'readable','2026-10-01T00:00:00Z',1)",
                        (channel_id, channel_id),
                    )
            client = ExpiredClient()
            result = SyncService(db, client).sync_selected()
            self.assertTrue(result["auth_required"])
            self.assertEqual(client.channels, ["a"])
            self.assertEqual(len(result["errors"]), 1)

            with self.assertRaises(ZoomAuthError):
                SyncService(db, client).probe(["a"])
            with db.connect() as connection:
                status = connection.execute("SELECT readability_status FROM channels WHERE id='a'").fetchone()[0]
            self.assertEqual(status, "readable")


if __name__ == "__main__":
    unittest.main()
