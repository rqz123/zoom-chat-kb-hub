from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from zoom_kb.db import Database
from zoom_kb.knowledge import KnowledgeService, is_question, normalize_question
from zoom_kb.sync import ENCRYPTED_PLACEHOLDER, SyncService, classify_messages
from zoom_kb.zoom_client import rfc3339_seconds


class FakeClient:
    def __init__(self):
        self.calls = []

    def iter_channels(self):
        yield {"id": "c1", "name": "Readable", "type": 1}
        yield {"id": "c2", "name": "Encrypted", "type": 1}

    def iter_messages(self, channel_id, start, end, max_pages=None):
        self.calls.append((channel_id, start, end, max_pages))
        if channel_id == "c1":
            yield {"id": "m1", "date_time": "2026-09-30T12:00:00Z", "message": "How do I configure audio?", "sender": {"name": "Sales"}}
        else:
            yield {"id": "m2", "date_time": "2026-09-30T12:01:00Z", "message": ENCRYPTED_PLACEHOLDER}


class EmptyClient(FakeClient):
    def iter_channels(self):
        yield {"id": "empty", "name": "Empty", "type": 1}

    def iter_messages(self, channel_id, start, end, max_pages=None):
        self.calls.append((channel_id, start, end, max_pages))
        return iter(())


class CoreTests(unittest.TestCase):
    def test_readability_classification(self):
        self.assertEqual(classify_messages([])[0], "unknown_no_messages")
        self.assertEqual(classify_messages([{"message": "hello"}])[0], "readable")
        self.assertEqual(classify_messages([{"message": ENCRYPTED_PLACEHOLDER}])[0], "encrypted")
        self.assertEqual(classify_messages([{"message": "hello"}, {"message": ENCRYPTED_PLACEHOLDER}])[0], "mixed")

    def test_rfc3339_has_no_microseconds(self):
        value = datetime(2026, 9, 30, 12, 3, 4, 987654, tzinfo=timezone.utc)
        self.assertEqual(rfc3339_seconds(value), "2026-09-30T12:03:04Z")

    def test_question_detection_and_normalization(self):
        self.assertTrue(is_question("How do I configure audio?"))
        self.assertTrue(is_question("请问这个功能如何配置"))
        self.assertFalse(is_question("Thank you for the update"))
        self.assertEqual(normalize_question("How  do I configure AUDIO?!"), "how do i configure audio")

    def test_legacy_cursor_schema_is_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE channels(id TEXT PRIMARY KEY,name TEXT,type INTEGER,last_seen_at TEXT,is_active INTEGER,selected INTEGER,readability_status TEXT,readability_reason TEXT,last_probed_at TEXT)")
            connection.execute("CREATE TABLE sync_cursors(channel_id TEXT PRIMARY KEY,last_message_at TEXT,last_sync_at TEXT,last_error TEXT)")
            connection.commit()
            connection.close()
            db = Database(path)
            db.initialize()
            with db.connect() as connection:
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(sync_cursors)")}
                topic_columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversation_topics)")}
            self.assertIn("initial_sync_completed_at", columns)
            self.assertIn("sync_watermark_at", columns)
            self.assertIn("ignored_at", topic_columns)
            self.assertIn("keep_tracking", topic_columns)

    def test_empty_first_sync_still_becomes_incremental(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "empty.db")
            db.initialize()
            service = SyncService(db, EmptyClient())
            service.refresh_channels()
            with db.connect() as connection:
                connection.execute("UPDATE channels SET selected=1 WHERE id='empty'")
            service.sync_selected()
            with db.connect() as connection:
                cursor = connection.execute("SELECT * FROM sync_cursors WHERE channel_id='empty'").fetchone()
            self.assertIsNotNone(cursor["initial_sync_completed_at"])
            self.assertIsNotNone(cursor["covered_from_at"])
            self.assertIsNotNone(cursor["sync_watermark_at"])

    def test_refresh_probe_sync_and_dedupe(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.db")
            db.initialize()
            client = FakeClient()
            service = SyncService(db, client)
            self.assertEqual(db.get_setting("initial_sync_days"), "90")
            self.assertEqual(db.get_setting("ai_model_tier"), "mid")
            self.assertEqual(db.get_setting("ai_output_language"), "chinese")
            self.assertEqual(service.refresh_channels(), 2)
            statuses = {item["id"]: item["status"] for item in service.probe()}
            self.assertEqual(statuses, {"c1": "readable", "c2": "encrypted"})
            with db.connect() as connection:
                connection.execute("UPDATE channels SET selected=1 WHERE id='c1'")
            first = service.sync_selected()
            second = service.sync_selected()
            self.assertEqual(first["status"], "completed")
            self.assertEqual(second["status"], "completed")
            with db.connect() as connection:
                count = connection.execute("SELECT COUNT(*) count FROM messages").fetchone()["count"]
                raw = connection.execute("SELECT raw_json FROM messages").fetchone()["raw_json"]
                cursor = connection.execute("SELECT * FROM sync_cursors WHERE channel_id='c1'").fetchone()
            self.assertEqual(count, 1)
            self.assertEqual(json.loads(raw)["id"], "m1")
            self.assertIsNotNone(cursor["initial_sync_completed_at"])
            self.assertIsNotNone(cursor["covered_from_at"])
            self.assertIsNotNone(cursor["sync_watermark_at"])
            first_sync_call, incremental_call = client.calls[-2:]
            self.assertGreaterEqual((first_sync_call[2] - first_sync_call[1]).days, 89)
            self.assertLess((incremental_call[2] - incremental_call[1]).total_seconds(), 360)
            backfill = service.backfill_channel("c1", 180)
            self.assertTrue(backfill["changed"])
            knowledge = KnowledgeService(db)
            self.assertEqual(knowledge.extract()["created"], 1)
            self.assertEqual(knowledge.extract(), {"created": 0, "merged": 0})


if __name__ == "__main__":
    unittest.main()

