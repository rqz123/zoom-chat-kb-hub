from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from zoom_kb.ai_topics import ActionItem, ExtractedTopic, TopicAIService, TopicExtractionResult, TopicTranslationResult
from zoom_kb.db import Database


def iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FakeTopicService(TopicAIService):
    translation_calls = 0

    def _extract(self, window, settings):
        ids = [item["zoom_message_id"] for item in window["messages"]]
        return TopicExtractionResult(topics=[ExtractedTopic(
            title=f"Topic {ids[0]}",
            problem_summary="A summarized problem",
            context_summary="Product context",
            discussion_summary="The team discussed and answered it.",
            confirmed_facts=["A confirmed fact"],
            conclusions=["A supported conclusion"],
            open_questions=[],
            action_items=[ActionItem(description="Verify", owner="", due_date="")],
            tags=["test"],
            status="resolved",
            confidence=0.9,
            source_message_ids=ids,
        )]), {"response_id": "resp_test", "input_tokens": 10, "output_tokens": 5}

    def _translate_topic(self, payload, target_language, settings):
        self.translation_calls += 1
        return TopicTranslationResult(
            title="翻译后的主题",
            problem_summary="翻译后的问题",
            context_summary="翻译后的背景",
            discussion_summary="翻译后的讨论",
            confirmed_facts=["已确认事实"],
            conclusions=["结论"],
            open_questions=["待确认问题"],
            action_items=[ActionItem(description="验证", owner="", due_date="")],
            tags=["测试"],
        )


class AITopicTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "topics.db")
        self.db.initialize()
        now = datetime.now(timezone.utc)
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO channels(id,name,type,selected,last_seen_at,is_active)
                   VALUES('c1','Leadership',1,1,?,1)""", (iso(now),)
            )
            messages = [
                ("z1", iso(now - timedelta(hours=10)), "First issue"),
                ("z2", iso(now - timedelta(hours=1)), "Second issue question"),
                ("z3", iso(now - timedelta(minutes=30)), "Second issue answer"),
            ]
            for message_id, sent_at, body in messages:
                connection.execute(
                    """INSERT INTO messages(channel_id,zoom_message_id,sender_name,sent_at,body,body_state,raw_json)
                       VALUES('c1',?,'Person',?,?,'readable',?)""",
                    (message_id, sent_at, body, json.dumps({"id": message_id})),
                )

    def tearDown(self):
        self.folder.cleanup()

    def test_windows_split_on_time_gap(self):
        windows = TopicAIService(self.db).build_windows(2)
        self.assertEqual(len(windows), 2)
        self.assertEqual([len(window["messages"]) for window in windows], [1, 2])

    def test_thread_reply_is_grouped_with_root_across_time_gap(self):
        now = datetime.now(timezone.utc)
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO messages(channel_id,zoom_message_id,sender_name,sent_at,body,body_state,raw_json)
                   VALUES('c1','z5','Person',?,'Nearby channel reply','readable',?)""",
                (iso(now - timedelta(hours=9, minutes=30)), json.dumps({"id": "z5"})),
            )
            connection.execute(
                """INSERT INTO messages(channel_id,zoom_message_id,sender_name,sent_at,body,body_state,thread_id,reply_to_message_id,raw_json)
                   VALUES('c1','z4','Person',?,'Late thread update','readable','z1','z1',?)""",
                (iso(now), json.dumps({"id": "z4"})),
            )
        windows = TopicAIService(self.db).build_windows(2)
        thread = next(window for window in windows if window["kind"] == "thread")
        self.assertEqual([item["zoom_message_id"] for item in thread["messages"]], ["z1", "z5", "z4"])

    def test_skipped_windows_do_not_consume_processing_limit(self):
        service = FakeTopicService(self.db)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            first = service.analyze_recent(days=2, max_windows=1)
            second = service.analyze_recent(days=2, max_windows=1)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 1)
        self.assertEqual(second["skipped"], 1)

    def test_model_tier_and_language_come_from_app_settings(self):
        self.db.set_setting("ai_model_tier", "high")
        self.db.set_setting("ai_output_language", "english")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            settings = TopicAIService(self.db)._resolved_settings()
        self.assertEqual(settings.model, "gpt-5.6-terra")
        self.assertEqual(settings.output_language, "english")

    def test_new_topic_supersedes_unreviewed_topic_with_contained_sources(self):
        service = FakeTopicService(self.db)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            service.analyze_recent(days=2, max_windows=1)
            with self.db.connect() as connection:
                old = connection.execute("SELECT id FROM conversation_topics ORDER BY id LIMIT 1").fetchone()["id"]
                connection.execute("UPDATE ai_runs SET input_hash='old-hash' WHERE id=(SELECT ai_run_id FROM conversation_topics WHERE id=?)", (old,))
                connection.execute("UPDATE conversation_topics SET prompt_version='old-version',ignored_at='2026-01-01T00:00:00Z' WHERE id=?", (old,))
            service.analyze_recent(days=2, max_windows=1)
        with self.db.connect() as connection:
            row = connection.execute("SELECT superseded_by_id FROM conversation_topics WHERE id=?", (old,)).fetchone()
            replacement = connection.execute("SELECT ignored_at FROM conversation_topics WHERE id=?", (row["superseded_by_id"],)).fetchone()
        self.assertIsNotNone(row["superseded_by_id"])
        self.assertEqual(replacement["ignored_at"], "2026-01-01T00:00:00Z")

    def test_analysis_is_structured_traceable_and_idempotent(self):
        service = FakeTopicService(self.db)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model"}):
            first = service.analyze_recent(days=2)
            second = service.analyze_recent(days=2)
        self.assertEqual(first["processed"], 2)
        self.assertEqual(first["topics"], 2)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(second["skipped"], 2)
        with self.db.connect() as connection:
            topics = connection.execute("SELECT * FROM conversation_topics ORDER BY id").fetchall()
            sources = connection.execute("SELECT COUNT(*) count FROM topic_sources").fetchone()["count"]
            run = connection.execute("SELECT * FROM ai_runs ORDER BY id LIMIT 1").fetchone()
        self.assertEqual(len(topics), 2)
        self.assertEqual(sources, 3)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["response_id"], "resp_test")

    def test_topic_translation_is_persistently_cached(self):
        service = FakeTopicService(self.db)
        service.translation_calls = 0
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            service.analyze_recent(days=2, max_windows=1)
            with self.db.connect() as connection:
                topic_id = connection.execute("SELECT id FROM conversation_topics LIMIT 1").fetchone()["id"]
            first = service.translate_topic(topic_id, "chinese")
            second = service.translate_topic(topic_id, "chinese")
            original = service.translate_topic(topic_id, "english")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["translation"], second["translation"])
        self.assertEqual(service.translation_calls, 1)
        self.assertTrue(original["cached"])
        self.assertEqual(original["translation"]["title"], "Topic z1")


if __name__ == "__main__":
    unittest.main()

