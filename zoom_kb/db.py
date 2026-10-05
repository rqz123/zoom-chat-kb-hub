from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type INTEGER,
    selected INTEGER NOT NULL DEFAULT 0,
    readability_status TEXT NOT NULL DEFAULT 'unknown_no_messages',
    readability_reason TEXT,
    last_probed_at TEXT,
    last_seen_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sync_cursors (
    channel_id TEXT PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    last_message_at TEXT,
    last_sync_at TEXT,
    last_error TEXT,
    initial_sync_completed_at TEXT,
    covered_from_at TEXT,
    sync_watermark_at TEXT,
    last_success_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    zoom_message_id TEXT NOT NULL,
    sender_name TEXT,
    sender_member_id TEXT,
    sent_at TEXT NOT NULL,
    body TEXT,
    body_state TEXT NOT NULL,
    thread_id TEXT,
    reply_to_message_id TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(channel_id, zoom_message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_time ON messages(channel_id, sent_at DESC);

CREATE TABLE IF NOT EXISTS message_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    member_id TEXT,
    display_name TEXT,
    is_current_user INTEGER NOT NULL DEFAULT 0,
    UNIQUE(message_id, member_id, display_name)
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    file_id TEXT,
    file_name TEXT,
    file_type TEXT,
    file_size INTEGER,
    UNIQUE(message_id, file_id, file_name)
);

CREATE TABLE IF NOT EXISTS mention_tasks (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'new',
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    channel_count INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_question TEXT NOT NULL,
    normalized_key TEXT NOT NULL UNIQUE,
    answer TEXT,
    category TEXT NOT NULL DEFAULT 'uncategorized',
    status TEXT NOT NULL DEFAULT 'candidate',
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_status ON knowledge_items(status, occurrences DESC);

CREATE TABLE IF NOT EXISTS knowledge_sources (
    knowledge_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    PRIMARY KEY(knowledge_id, message_id, relation)
);

CREATE TABLE IF NOT EXISTS knowledge_topic_sources (
    knowledge_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES conversation_topics(id) ON DELETE CASCADE,
    PRIMARY KEY(knowledge_id, topic_id)
);

CREATE TABLE IF NOT EXISTS ai_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    input_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    response_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(input_hash, prompt_version)
);

CREATE TABLE IF NOT EXISTS conversation_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_run_id INTEGER NOT NULL REFERENCES ai_runs(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    problem_summary TEXT NOT NULL,
    context_summary TEXT NOT NULL,
    discussion_summary TEXT NOT NULL,
    confirmed_facts_json TEXT NOT NULL,
    conclusions_json TEXT NOT NULL,
    open_questions_json TEXT NOT NULL,
    action_items_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    first_message_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    source_message_count INTEGER NOT NULL,
    ai_model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    superseded_by_id INTEGER REFERENCES conversation_topics(id) ON DELETE SET NULL,
    ignored_at TEXT,
    keep_tracking INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_recent ON conversation_topics(last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_topics_review ON conversation_topics(review_status, status);

CREATE TABLE IF NOT EXISTS topic_sources (
    topic_id INTEGER NOT NULL REFERENCES conversation_topics(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    PRIMARY KEY(topic_id, message_id)
);

CREATE TABLE IF NOT EXISTS topic_translations (
    topic_id INTEGER NOT NULL REFERENCES conversation_topics(id) ON DELETE CASCADE,
    target_language TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(topic_id, target_language, source_hash)
);
CREATE INDEX IF NOT EXISTS idx_topic_translations_lookup
    ON topic_translations(topic_id, target_language, source_hash);

CREATE TABLE IF NOT EXISTS topic_merge_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_topic_id INTEGER NOT NULL REFERENCES conversation_topics(id) ON DELETE CASCADE,
    target_topic_id INTEGER NOT NULL REFERENCES conversation_topics(id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_topic_id, target_topic_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_merge_target
    ON topic_merge_events(target_topic_id, created_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_sync_cursors(connection)
            self._migrate_knowledge_items(connection)
            connection.execute(
                "INSERT OR IGNORE INTO app_settings(key,value) VALUES('initial_sync_days','90')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_settings(key,value) VALUES('ai_model_tier','mid')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_settings(key,value) VALUES('ai_output_language','chinese')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_settings(key,value) VALUES('knowledge_maturity_days','14')"
            )
            connection.execute(
                "UPDATE app_settings SET value='chinese' WHERE key='ai_output_language' AND value IN ('bilingual','source')"
            )
            self._migrate_conversation_topics(connection)
            self._migrate_knowledge_pipeline(connection)

    @staticmethod
    def _migrate_sync_cursors(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(sync_cursors)")
        }
        additions = {
            "initial_sync_completed_at": "TEXT",
            "covered_from_at": "TEXT",
            "sync_watermark_at": "TEXT",
            "last_success_at": "TEXT",
        }
        for name, column_type in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE sync_cursors ADD COLUMN {name} {column_type}")

        # Existing successful cursors represent completed initial syncs. The exact
        # historical query boundary was not stored, so use the oldest local message
        # as a conservative coverage marker and allow an explicit backfill later.
        connection.execute(
            """UPDATE sync_cursors
               SET initial_sync_completed_at=COALESCE(initial_sync_completed_at,last_sync_at),
                   sync_watermark_at=COALESCE(sync_watermark_at,last_sync_at),
                   last_success_at=COALESCE(last_success_at,last_sync_at),
                   covered_from_at=COALESCE(
                     covered_from_at,
                     (SELECT MIN(m.sent_at) FROM messages m WHERE m.channel_id=sync_cursors.channel_id)
                   )
               WHERE last_error IS NULL AND last_sync_at IS NOT NULL"""
        )

    @staticmethod
    def _migrate_knowledge_items(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(knowledge_items)")
        }
        additions = {
            "problem_summary": "TEXT NOT NULL DEFAULT ''",
            "conclusion_summary": "TEXT NOT NULL DEFAULT ''",
            "context_summary": "TEXT NOT NULL DEFAULT ''",
            "open_questions_json": "TEXT NOT NULL DEFAULT '[]'",
            "action_items_json": "TEXT NOT NULL DEFAULT '[]'",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "last_verified_at": "TEXT",
            "source_language": "TEXT NOT NULL DEFAULT 'unknown'",
            "resolution_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "tags_json": "TEXT NOT NULL DEFAULT '[]'",
            "origin_version": "TEXT NOT NULL DEFAULT 'legacy'",
            "has_new_activity": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE knowledge_items ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_conversation_topics(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversation_topics)")}
        if "superseded_by_id" not in columns:
            connection.execute(
                "ALTER TABLE conversation_topics ADD COLUMN superseded_by_id INTEGER REFERENCES conversation_topics(id) ON DELETE SET NULL"
            )
        if "ignored_at" not in columns:
            connection.execute("ALTER TABLE conversation_topics ADD COLUMN ignored_at TEXT")
        if "keep_tracking" not in columns:
            connection.execute("ALTER TABLE conversation_topics ADD COLUMN keep_tracking INTEGER NOT NULL DEFAULT 0")
        if "archived_at" not in columns:
            connection.execute("ALTER TABLE conversation_topics ADD COLUMN archived_at TEXT")
        if "source_language" not in columns:
            connection.execute("ALTER TABLE conversation_topics ADD COLUMN source_language TEXT NOT NULL DEFAULT 'unknown'")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS topic_merge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_topic_id INTEGER NOT NULL REFERENCES conversation_topics(id) ON DELETE CASCADE,
                target_topic_id INTEGER NOT NULL REFERENCES conversation_topics(id) ON DELETE CASCADE,
                method TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_topic_id, target_topic_id)
            );
            CREATE INDEX IF NOT EXISTS idx_topic_merge_target
                ON topic_merge_events(target_topic_id, created_at DESC);
            """
        )

    @staticmethod
    def _migrate_knowledge_pipeline(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_archive_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                input_hash TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL,
                channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                knowledge_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_archive_runs_status
                ON knowledge_archive_runs(mode,status,started_at DESC);

            CREATE TABLE IF NOT EXISTS knowledge_import_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                total_windows INTEGER NOT NULL DEFAULT 0,
                processed_windows INTEGER NOT NULL DEFAULT 0,
                skipped_windows INTEGER NOT NULL DEFAULT 0,
                knowledge_created INTEGER NOT NULL DEFAULT 0,
                knowledge_merged INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS knowledge_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(knowledge_id,version)
            );
            CREATE TABLE IF NOT EXISTS knowledge_version_sources (
                version_id INTEGER NOT NULL REFERENCES knowledge_versions(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                PRIMARY KEY(version_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS knowledge_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_knowledge_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
                target_knowledge_id INTEGER NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL CHECK(relation_type IN ('related','duplicate','conflict','supersedes')),
                confidence REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(source_knowledge_id,target_knowledge_id,relation_type)
            );
            CREATE TABLE IF NOT EXISTS knowledge_embeddings (
                knowledge_id INTEGER PRIMARY KEY REFERENCES knowledge_items(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                vector_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_language TEXT NOT NULL,
                translated INTEGER NOT NULL DEFAULT 0,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                result_count INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                knowledge_id UNINDEXED,title,problem,context,conclusion,tags,
                tokenize='unicode61'
            );
            """
        )
        archive_columns = {row["name"] for row in connection.execute("PRAGMA table_info(knowledge_archive_runs)")}
        for name, definition in {
            "model": "TEXT",
            "prompt_version": "TEXT",
            "input_tokens": "INTEGER",
            "output_tokens": "INTEGER",
        }.items():
            if name not in archive_columns:
                connection.execute(f"ALTER TABLE knowledge_archive_runs ADD COLUMN {name} {definition}")
        import_columns = {row["name"] for row in connection.execute("PRAGMA table_info(knowledge_import_runs)")}
        for name, definition in {
            "scanned_messages": "INTEGER NOT NULL DEFAULT 0",
            "identified_topics": "INTEGER NOT NULL DEFAULT 0",
            "knowledge_updated": "INTEGER NOT NULL DEFAULT 0",
            "knowledge_related": "INTEGER NOT NULL DEFAULT 0",
            "knowledge_conflicts": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in import_columns:
                connection.execute(f"ALTER TABLE knowledge_import_runs ADD COLUMN {name} {definition}")
        version_columns = {row["name"] for row in connection.execute("PRAGMA table_info(knowledge_versions)")}
        for name, definition in {
            "model": "TEXT",
            "prompt_version": "TEXT",
            "input_hash": "TEXT",
            "source_language": "TEXT",
            "resolution_status": "TEXT",
            "change_type": "TEXT NOT NULL DEFAULT 'create'",
        }.items():
            if name not in version_columns:
                connection.execute(f"ALTER TABLE knowledge_versions ADD COLUMN {name} {definition}")
        connection.execute("UPDATE knowledge_items SET created_at=COALESCE(created_at,updated_at)")
        connection.execute(
            """UPDATE knowledge_items SET origin_version='v0.2'
               WHERE EXISTS(SELECT 1 FROM knowledge_versions v WHERE v.knowledge_id=knowledge_items.id)"""
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_setting(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO app_settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

