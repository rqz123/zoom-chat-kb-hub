from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from zoom_kb.ai_topics import ActionItem, ExtractedTopic, TopicAIService, TopicExtractionResult
from zoom_kb.db import Database
from zoom_kb.knowledge_pipeline import ArchiveDecision, KnowledgePipeline


def iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FakeArchiveAI(TopicAIService):
    def _extract(self, window, settings):
        ids = [item["zoom_message_id"] for item in window["messages"]]
        chinese = settings.output_language == "chinese"
        topic = ExtractedTopic(
            title="音频配置问题" if chinese else "Audio configuration issue",
            problem_summary="无法输出音频" if chinese else "Audio output is unavailable",
            context_summary="会议室设备" if chinese else "Meeting room device",
            discussion_summary="已找到配置方法" if chinese else "A configuration was identified",
            confirmed_facts=[],
            conclusions=["已解决" if chinese else "Resolved"],
            open_questions=[],
            action_items=[ActionItem(description="Verify", owner="", due_date="")],
            tags=["audio"],
            status="resolved",
            confidence=0.9,
            source_message_ids=ids,
        )
        return TopicExtractionResult(topics=[topic]), {}


class FakeKnowledgePipeline(KnowledgePipeline):
    def _embed(self, text):
        lowered = text.lower()
        return [1.0 if "audio" in lowered or "音频" in lowered else 0.0, 1.0 if "config" in lowered or "配置" in lowered else 0.0]

    def _translate(self, query):
        return []

    def _decide(self, topic, language, candidates):
        if not candidates:
            return ArchiveDecision(action="create", target_knowledge_id=0, confidence=1, reason="test")
        best = candidates[0]
        action = "update" if best["source_language"] == language else "related"
        return ArchiveDecision(action=action, target_knowledge_id=best["id"], confidence=.95, reason="test")


class KnowledgePipelineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "knowledge.db")
        self.db.initialize()
        now = datetime.now(timezone.utc)
        with self.db.connect() as connection:
            connection.execute("INSERT INTO channels(id,name,selected,last_seen_at,is_active) VALUES('c1','Sales',1,?,1)", (iso(now),))
            for message_id, age, body in (
                ("old1", 21, "How do we configure the audio output?"),
                ("old2", 21, "Use the room audio settings."),
                ("new1", 1, "A recent question should stay in topics."),
            ):
                connection.execute(
                    """INSERT INTO messages(channel_id,zoom_message_id,sender_name,sent_at,body,body_state,raw_json)
                       VALUES('c1',?,'Sales',?,?,'readable',?)""",
                    (message_id, iso(now - timedelta(days=age)), body, json.dumps({"id": message_id})),
                )
        self.pipeline = FakeKnowledgePipeline(self.db, FakeArchiveAI(self.db))

    def tearDown(self):
        self.folder.cleanup()

    def test_historical_import_is_source_language_and_idempotent(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            first = self.pipeline.bootstrap_historical(max_windows=10)
            second = self.pipeline.bootstrap_historical(max_windows=10)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertGreaterEqual(second["skipped"], 1)
        with self.db.connect() as connection:
            rows = connection.execute("SELECT * FROM knowledge_items").fetchall()
            sources = connection.execute("SELECT COUNT(*) count FROM knowledge_sources").fetchone()["count"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_language"], "english")
        self.assertEqual(sources, 2)
        self.assertEqual(self.pipeline.bootstrap_status()["remaining_windows"], 0)

    def test_search_returns_recent_knowledge_with_score(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            self.pipeline.bootstrap_historical(max_windows=10)
        rows = self.pipeline.search("audio configuration", translate=False)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["match_score"], 0.5)

    def test_legacy_items_are_hidden_from_v02_views(self):
        now = iso(datetime.now(timezone.utc))
        with self.db.connect() as connection:
            connection.execute("""INSERT INTO knowledge_items(canonical_question,normalized_key,first_seen_at,last_seen_at,updated_at)
                                  VALUES('Legacy','legacy',?,?,?)""", (now, now, now))
        self.assertEqual(self.pipeline.recent(), [])

    def test_same_language_updates_version_and_cross_language_stays_related(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            self.pipeline.bootstrap_historical(max_windows=10)
        with self.db.connect() as connection:
            source = dict(connection.execute("""SELECT id local_id,zoom_message_id,channel_id,sender_name,sent_at,body,
                                               thread_id,reply_to_message_id FROM messages WHERE zoom_message_id='old1'""").fetchone())
        english = FakeArchiveAI(self.db)._extract({"messages": [source]}, type("S", (), {"output_language": "english"})())[0].topics[0]
        chinese = FakeArchiveAI(self.db)._extract({"messages": [source]}, type("S", (), {"output_language": "chinese"})())[0].topics[0]
        self.pipeline._archive_topic(english, [source], "english", None, "test", "h2")
        self.pipeline._archive_topic(chinese, [source], "chinese", None, "test", "h3")
        with self.db.connect() as connection:
            active = connection.execute("SELECT COUNT(*) n FROM knowledge_items WHERE origin_version='v0.2'").fetchone()["n"]
            versions = connection.execute("SELECT COUNT(*) n FROM knowledge_versions").fetchone()["n"]
            related = connection.execute("SELECT COUNT(*) n FROM knowledge_relations WHERE relation_type='related'").fetchone()["n"]
        self.assertEqual(active, 2)
        self.assertEqual(versions, 3)
        self.assertEqual(related, 1)

    def test_english_query_can_find_chinese_original_language_knowledge(self):
        with self.db.connect() as connection:
            source = dict(connection.execute("""SELECT id local_id,zoom_message_id,channel_id,sender_name,sent_at,body,
                                               thread_id,reply_to_message_id FROM messages WHERE zoom_message_id='old1'""").fetchone())
        chinese = FakeArchiveAI(self.db)._extract({"messages": [source]}, type("S", (), {"output_language": "chinese"})())[0].topics[0]
        self.pipeline._archive_topic(chinese, [source], "chinese", None, "test", "cross-language")
        results = self.pipeline.search("audio configuration", translate=False)
        self.assertEqual(results[0]["source_language"], "chinese")
        self.assertGreater(results[0]["semantic_score"], .9)


if __name__ == "__main__":
    unittest.main()
