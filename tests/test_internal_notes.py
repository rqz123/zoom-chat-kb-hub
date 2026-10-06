from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zoom_kb.ai_topics import TopicAIService, TopicInternalNoteResult
from zoom_kb.db import Database


class InternalNoteService(TopicAIService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.payloads = []

    def _summarize_internal_note(self, payload, settings):
        self.payloads.append(payload)
        notes = payload["internal_note_history"] + [payload["new_internal_note"]]
        return TopicInternalNoteResult(summary=" | ".join(notes))


class FailingInternalNoteService(TopicAIService):
    def _summarize_internal_note(self, payload, settings):
        raise RuntimeError("AI failed")


class InternalNoteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "notes.db")
        self.db.initialize()
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO channels(id,name,selected,last_seen_at) VALUES('c1','Product',1,'2026-10-05T12:00:00Z')"
            )
            run_id = connection.execute(
                """INSERT INTO ai_runs(
                     input_hash,prompt_version,model,channel_id,status,message_count,started_at
                   ) VALUES('note-test','test','test','c1','succeeded',0,'2026-10-05T12:00:00Z')"""
            ).lastrowid
            self.topic_id = connection.execute(
                """INSERT INTO conversation_topics(
                     ai_run_id,channel_id,title,problem_summary,context_summary,discussion_summary,
                     confirmed_facts_json,conclusions_json,open_questions_json,action_items_json,tags_json,
                     status,confidence,first_message_at,last_message_at,source_message_count,
                     ai_model,prompt_version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, "c1", "Private follow-up", "Customer issue", "Zoom context",
                    "Investigation continues", "[]", "[]", "[]", "[]", "[]", "waiting", 0.9,
                    "2026-10-05T11:00:00Z", "2026-10-05T12:00:00Z", 0,
                    "test", "test", "2026-10-05T12:00:00Z", "2026-10-05T12:00:00Z",
                ),
            ).lastrowid

    def tearDown(self):
        self.folder.cleanup()

    def test_notes_are_accumulated_and_raw_text_is_preserved_separately(self):
        service = InternalNoteService(self.db)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            first = service.add_internal_note(self.topic_id, "Internal test passed.")
            second = service.add_internal_note(self.topic_id, "Do not share pricing yet.")

        self.assertEqual(first["internal_note_count"], 1)
        self.assertEqual(second["internal_note_count"], 2)
        self.assertEqual(
            service.payloads[1]["existing_internal_summary"],
            "Internal test passed.",
        )
        self.assertEqual(service.payloads[1]["internal_note_history"], ["Internal test passed."])
        self.assertEqual(service.payloads[1]["zoom_background_only"]["context_summary"], "Zoom context")
        with self.db.connect() as connection:
            topic = connection.execute(
                "SELECT internal_context_summary,last_message_at FROM conversation_topics WHERE id=?",
                (self.topic_id,),
            ).fetchone()
            notes = connection.execute(
                "SELECT note_text FROM topic_internal_notes WHERE topic_id=? ORDER BY id",
                (self.topic_id,),
            ).fetchall()
            message_count = connection.execute("SELECT COUNT(*) count FROM messages").fetchone()["count"]
            source_count = connection.execute("SELECT COUNT(*) count FROM topic_sources").fetchone()["count"]
        self.assertEqual(topic["internal_context_summary"], "Internal test passed. | Do not share pricing yet.")
        self.assertEqual(topic["last_message_at"], "2026-10-05T12:00:00Z")
        self.assertEqual([row["note_text"] for row in notes], ["Internal test passed.", "Do not share pricing yet."])
        self.assertEqual(message_count, 0)
        self.assertEqual(source_count, 0)

    def test_ai_failure_does_not_save_note(self):
        service = FailingInternalNoteService(self.db)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with self.assertRaisesRegex(RuntimeError, "AI failed"):
                service.add_internal_note(self.topic_id, "Keep this private.")
        with self.db.connect() as connection:
            note_count = connection.execute("SELECT COUNT(*) count FROM topic_internal_notes").fetchone()["count"]
            summary = connection.execute(
                "SELECT internal_context_summary FROM conversation_topics WHERE id=?", (self.topic_id,)
            ).fetchone()["internal_context_summary"]
        self.assertEqual(note_count, 0)
        self.assertEqual(summary, "")


if __name__ == "__main__":
    unittest.main()
