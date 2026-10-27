from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import zoom_kb.app as app_module
from zoom_kb.db import Database


class MentionViewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "mentions.db")
        self.db.initialize()
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO channels(id,name,selected,last_seen_at) VALUES('c1','Product',1,'2026-10-01T00:00:00Z')"
            )
            for index, status in enumerate(("new", "in_progress", "done", "ignored")):
                message_id = connection.execute(
                    """INSERT INTO messages(channel_id,zoom_message_id,sender_name,sent_at,body,body_state,raw_json)
                       VALUES('c1',?,'Person',?,?,'readable',?)""",
                    (f"m{index}", f"2026-10-0{index + 1}T00:00:00Z", status, json.dumps({"id": index})),
                ).lastrowid
                connection.execute(
                    "INSERT INTO mention_tasks(message_id,status,updated_at) VALUES(?,?,'2026-10-01T00:00:00Z')",
                    (message_id, status),
                )
        self.patcher = patch.object(app_module, "db", self.db)
        self.patcher.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self.patcher.stop()
        self.folder.cleanup()

    def statuses(self, query: str) -> set[str]:
        return {row["status"] for row in self.client.get(f"/api/mentions{query}").json()}

    def test_open_and_handled_views(self):
        self.assertEqual(self.statuses("?view=open"), {"new", "in_progress"})
        self.assertEqual(self.statuses("?view=handled"), {"done", "ignored"})
        self.assertEqual(self.statuses(""), {"new", "in_progress", "done", "ignored"})

    def test_overview_counts_only_open_mentions(self):
        self.assertEqual(self.client.get("/api/dashboard").json()["open_mentions"], 2)


if __name__ == "__main__":
    unittest.main()
