import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from zoom_kb.ai_topics import TopicAIService, TopicContinuationDecision
from zoom_kb.db import Database


def iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ContinuationTopicService(TopicAIService):
    def __init__(self, db):
        super().__init__(db)
        self.seen_candidates = []

    def _decide_continuation(self, topic, candidates, settings):
        self.seen_candidates = candidates
        return TopicContinuationDecision(
            same_issue=True,
            candidate_id=candidates[0]["candidate_id"],
            confidence=0.96,
            reason="Same customer, product, and echo issue with later follow-up.",
        )


class TopicMergingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "merging.db")
        self.db.initialize()
        now = datetime.now(timezone.utc)
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO channels(id,name,selected,last_seen_at) VALUES('c1','Product',1,?)",
                (iso(now),),
            )
            self.run_id = connection.execute(
                """INSERT INTO ai_runs(
                     input_hash,prompt_version,model,channel_id,status,message_count,started_at
                   ) VALUES('merge-test','test','test','c1','succeeded',8,?)""",
                (iso(now),),
            ).lastrowid
            self.messages = []
            for index in range(8):
                sent_at = iso(now - timedelta(days=2) + timedelta(hours=index))
                message_id = connection.execute(
                    """INSERT INTO messages(
                         channel_id,zoom_message_id,sender_name,sent_at,body,body_state,raw_json
                       ) VALUES('c1',?,'Person',?,?,'readable',?)""",
                    (f"m{index + 1}", sent_at, f"Message {index + 1}", json.dumps({"id": index + 1})),
                ).lastrowid
                self.messages.append((message_id, sent_at))

    def tearDown(self):
        self.folder.cleanup()

    def _topic(self, title, problem, message_indexes, keep_tracking=0):
        selected = [self.messages[index] for index in message_indexes]
        with self.db.connect() as connection:
            topic_id = connection.execute(
                """INSERT INTO conversation_topics(
                     ai_run_id,channel_id,title,problem_summary,context_summary,discussion_summary,
                     confirmed_facts_json,conclusions_json,open_questions_json,action_items_json,tags_json,
                     status,confidence,first_message_at,last_message_at,source_message_count,
                     ai_model,prompt_version,keep_tracking,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.run_id, "c1", title, problem, "Cooley meeting room", "Ongoing investigation",
                    "[]", "[]", "[]", "[]", '["Cooley","D7X","AEC"]',
                    "waiting", 0.95, min(item[1] for item in selected), max(item[1] for item in selected),
                    len(selected), "test", "test", keep_tracking, max(item[1] for item in selected),
                    max(item[1] for item in selected),
                ),
            ).lastrowid
            connection.executemany(
                "INSERT INTO topic_sources(topic_id,message_id) VALUES(?,?)",
                [(topic_id, item[0]) for item in selected],
            )
        return int(topic_id)

    def test_high_source_overlap_merges_without_subset(self):
        old_id = self._topic("Earlier echo topic", "External speaker echo", range(0, 7))
        new_id = self._topic("Updated echo topic", "External speaker echo", range(1, 8))
        with self.db.connect() as connection:
            merged = TopicAIService._merge_source_overlaps(connection, new_id, "c1")
        self.assertEqual(merged, 1)
        with self.db.connect() as connection:
            old = connection.execute(
                "SELECT superseded_by_id FROM conversation_topics WHERE id=?", (old_id,)
            ).fetchone()
            new = connection.execute(
                "SELECT source_message_count FROM conversation_topics WHERE id=?", (new_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT method,confidence FROM topic_merge_events WHERE source_topic_id=? AND target_topic_id=?",
                (old_id, new_id),
            ).fetchone()
        self.assertEqual(old["superseded_by_id"], new_id)
        self.assertEqual(new["source_message_count"], 8)
        self.assertEqual(event["method"], "source_overlap")
        self.assertAlmostEqual(event["confidence"], 0.75)

    def test_high_source_overlap_does_not_merge_different_subjects(self):
        old_id = self._topic("Quarterly pricing review", "Update reseller pricing", range(0, 7))
        new_id = self._topic("Camera firmware crash", "Camera reboots during calls", range(1, 8))
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE conversation_topics SET context_summary='Commercial planning',discussion_summary='Pricing analysis',tags_json='[\"pricing\"]' WHERE id=?",
                (old_id,),
            )
            connection.execute(
                "UPDATE conversation_topics SET context_summary='Video device',discussion_summary='Firmware debugging',tags_json='[\"camera\"]' WHERE id=?",
                (new_id,),
            )
            merged = TopicAIService._merge_source_overlaps(connection, new_id, "c1")
            old = connection.execute(
                "SELECT superseded_by_id FROM conversation_topics WHERE id=?", (old_id,)
            ).fetchone()
        self.assertEqual(merged, 0)
        self.assertIsNone(old["superseded_by_id"])

    def test_semantic_continuation_merges_disjoint_sources_and_preserves_tracking(self):
        old_id = self._topic(
            "Cooley D7X AI Board ceiling speaker echo",
            "Cooley hears echo with an external ceiling speaker and AEC is unavailable.",
            [0],
            keep_tracking=1,
        )
        new_id = self._topic(
            "Cooley D7X AI Board 外接吸顶扬声器回声跟进",
            "Cooley 对 D7X AI Board 外接扬声器的回声和 AEC 问题进行了后续测试。",
            [7],
        )
        service = ContinuationTopicService(self.db)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            result = service.consolidate_topic(new_id)
        self.assertEqual(result, {"source_overlap": 0, "semantic_continuation": 1})
        self.assertEqual(service.seen_candidates[0]["candidate_id"], old_id)
        with self.db.connect() as connection:
            old = connection.execute(
                "SELECT superseded_by_id FROM conversation_topics WHERE id=?", (old_id,)
            ).fetchone()
            new = connection.execute(
                "SELECT source_message_count,keep_tracking FROM conversation_topics WHERE id=?", (new_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT method,confidence,reason FROM topic_merge_events WHERE source_topic_id=? AND target_topic_id=?",
                (old_id, new_id),
            ).fetchone()
        self.assertEqual(old["superseded_by_id"], new_id)
        self.assertEqual(new["source_message_count"], 2)
        self.assertEqual(new["keep_tracking"], 1)
        self.assertEqual(event["method"], "semantic_continuation")
        self.assertAlmostEqual(event["confidence"], 0.96)

    def test_merge_moves_internal_notes_and_preserves_both_summaries(self):
        old_id = self._topic("Earlier topic", "Earlier problem", [0])
        new_id = self._topic("Current topic", "Current problem", [7])
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE conversation_topics SET internal_context_summary='Earlier private context' WHERE id=?",
                (old_id,),
            )
            connection.execute(
                "UPDATE conversation_topics SET internal_context_summary='Current private context' WHERE id=?",
                (new_id,),
            )
            connection.execute(
                """INSERT INTO topic_internal_notes(
                     topic_id,note_text,ai_summary,ai_model,prompt_version,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (old_id, "Raw private note", "Earlier private context", "test", "test", "2026-10-05T12:00:00Z"),
            )
            TopicAIService._merge_topics(connection, old_id, new_id, "test", 1.0, "test merge")
        with self.db.connect() as connection:
            target = connection.execute(
                "SELECT internal_context_summary FROM conversation_topics WHERE id=?", (new_id,)
            ).fetchone()
            notes = connection.execute(
                "SELECT topic_id,note_text FROM topic_internal_notes"
            ).fetchall()
        self.assertIn("Earlier private context", target["internal_context_summary"])
        self.assertIn("Current private context", target["internal_context_summary"])
        self.assertEqual([(row["topic_id"], row["note_text"]) for row in notes], [(new_id, "Raw private note")])


if __name__ == "__main__":
    unittest.main()
