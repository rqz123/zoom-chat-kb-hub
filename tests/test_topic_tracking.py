import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from zoom_kb.db import Database
from zoom_kb.knowledge_pipeline import KnowledgePipeline


app_module = importlib.import_module("zoom_kb.app")


class TopicTrackingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "tracking.db")
        self.db.initialize()
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO channels(id,name,last_seen_at) VALUES('channel-1','Support','2020-01-01T00:00:00Z')"
            )
            run_id = connection.execute(
                """INSERT INTO ai_runs(
                     input_hash,prompt_version,model,channel_id,status,message_count,started_at
                   ) VALUES('tracking-test','test','test','channel-1','succeeded',1,'2020-01-01T00:00:00Z')"""
            ).lastrowid
            self.topic_id = connection.execute(
                """INSERT INTO conversation_topics(
                     ai_run_id,channel_id,title,problem_summary,context_summary,discussion_summary,
                     confirmed_facts_json,conclusions_json,open_questions_json,action_items_json,tags_json,
                     status,confidence,first_message_at,last_message_at,source_message_count,
                     ai_model,prompt_version,created_at,updated_at,archived_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, "channel-1", "Long-running issue", "Problem", "Context", "Discussion",
                    "[]", "[]", "[]", "[]", "[]", "discussion", 0.9,
                    "2020-01-01T00:00:00Z", "2020-01-01T01:00:00Z", 1,
                    "test", "test", "2020-01-01T01:00:00Z", "2020-01-01T01:00:00Z",
                    "2020-01-20T00:00:00Z",
                ),
            ).lastrowid

    def tearDown(self):
        self.folder.cleanup()

    def test_keep_tracking_retains_archived_topic_and_blocks_ignore(self):
        pipeline = KnowledgePipeline(self.db)
        with patch.object(app_module, "db", self.db), patch.object(app_module, "knowledge_pipeline", pipeline):
            self.assertEqual(app_module.list_topics(search="", status="", limit=100), [])

            enabled = app_module.set_topic_tracking(
                self.topic_id, app_module.TopicTrackingUpdate(enabled=True)
            )
            self.assertTrue(enabled["keep_tracking"])
            rows = app_module.list_topics(search="", status="", limit=100)
            self.assertEqual([row["id"] for row in rows], [self.topic_id])
            self.assertEqual(rows[0]["keep_tracking"], 1)
            self.assertIsNotNone(rows[0]["archived_at"])

            with self.assertRaises(HTTPException) as blocked:
                app_module.ignore_topic(self.topic_id)
            self.assertEqual(blocked.exception.status_code, 409)

            app_module.set_topic_tracking(
                self.topic_id, app_module.TopicTrackingUpdate(enabled=False)
            )
            ignored = app_module.ignore_topic(self.topic_id)
            self.assertTrue(ignored["ignored"])
            self.assertEqual(app_module.list_topics(search="", status="", limit=100), [])

    def test_keep_tracking_does_not_change_topic_order_or_update_time(self):
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE conversation_topics
                   SET archived_at=NULL,last_message_at='2099-01-01T01:00:00Z',updated_at='2099-01-01T01:00:00Z'
                   WHERE id=?""",
                (self.topic_id,),
            )
            newer_id = connection.execute(
                """INSERT INTO conversation_topics(
                     ai_run_id,channel_id,title,problem_summary,context_summary,discussion_summary,
                     confirmed_facts_json,conclusions_json,open_questions_json,action_items_json,tags_json,
                     status,confidence,first_message_at,last_message_at,source_message_count,
                     ai_model,prompt_version,created_at,updated_at
                   ) SELECT ai_run_id,channel_id,'Newer topic',problem_summary,context_summary,discussion_summary,
                     confirmed_facts_json,conclusions_json,open_questions_json,action_items_json,tags_json,
                     status,confidence,'2099-01-02T00:00:00Z','2099-01-02T01:00:00Z',source_message_count,
                     ai_model,prompt_version,'2099-01-02T01:00:00Z','2099-01-02T01:00:00Z'
                   FROM conversation_topics WHERE id=?""",
                (self.topic_id,),
            ).lastrowid

        pipeline = KnowledgePipeline(self.db)
        with patch.object(app_module, "db", self.db), patch.object(app_module, "knowledge_pipeline", pipeline):
            before = app_module.list_topics(search="", status="", limit=100)
            app_module.set_topic_tracking(
                self.topic_id, app_module.TopicTrackingUpdate(enabled=True)
            )
            after = app_module.list_topics(search="", status="", limit=100)

        self.assertEqual([row["id"] for row in before], [newer_id, self.topic_id])
        self.assertEqual([row["id"] for row in after], [newer_id, self.topic_id])
        with self.db.connect() as connection:
            topic = connection.execute(
                "SELECT updated_at FROM conversation_topics WHERE id=?", (self.topic_id,)
            ).fetchone()
        self.assertEqual(topic["updated_at"], "2099-01-01T01:00:00Z")


if __name__ == "__main__":
    unittest.main()
