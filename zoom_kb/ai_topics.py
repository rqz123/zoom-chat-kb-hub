from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import (
    AI_PROMPT_VERSION,
    AI_MODEL_TIERS,
    AI_OUTPUT_LANGUAGES,
    AI_WINDOW_GAP_HOURS,
    AI_WINDOW_MAX_CHARS,
    AI_WINDOW_MAX_MESSAGES,
    DATA_DIR,
    DEFAULT_AI_MODEL_TIER,
    DEFAULT_AI_OUTPUT_LANGUAGE,
    OPENAI_CONFIG_FILE,
    OPENAI_FALLBACK_CONFIG,
    TOPIC_CONTINUATION_LOOKBACK_DAYS,
    TOPIC_CONTINUATION_MIN_CONFIDENCE,
    TOPIC_CONTINUATION_PROMPT_VERSION,
    TOPIC_SOURCE_OVERLAP_MERGE_THRESHOLD,
    TOPIC_TRANSLATION_PROMPT_VERSION,
)
from .db import Database
from .sync import now_iso, parse_zoom_time


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "topic_extraction.md"
TRANSLATION_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "topic_translation.md"
CONTINUATION_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "topic_continuation.md"


class ActionItem(BaseModel):
    description: str
    owner: str
    due_date: str


class ExtractedTopic(BaseModel):
    title: str
    problem_summary: str
    context_summary: str
    discussion_summary: str
    confirmed_facts: list[str]
    conclusions: list[str]
    open_questions: list[str]
    action_items: list[ActionItem]
    tags: list[str]
    status: Literal["discussion", "resolved", "waiting", "inconclusive"]
    confidence: float = Field(ge=0, le=1)
    source_message_ids: list[str]


class TopicExtractionResult(BaseModel):
    topics: list[ExtractedTopic]


class TopicTranslationResult(BaseModel):
    title: str
    problem_summary: str
    context_summary: str
    discussion_summary: str
    confirmed_facts: list[str]
    conclusions: list[str]
    open_questions: list[str]
    action_items: list[ActionItem]
    tags: list[str]


class TopicContinuationDecision(BaseModel):
    same_issue: bool
    candidate_id: int
    confidence: float = Field(ge=0, le=1)
    reason: str


@dataclass(frozen=True)
class OpenAISettings:
    api_key: str
    model: str
    base_url: str
    source: str
    model_tier: str = DEFAULT_AI_MODEL_TIER
    output_language: str = DEFAULT_AI_OUTPUT_LANGUAGE

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def load_openai_settings() -> OpenAISettings:
    candidates: list[Path] = []
    configured_path = os.getenv("OPENAI_CONFIG_FILE", OPENAI_CONFIG_FILE)
    if configured_path:
        candidates.append(Path(configured_path))
    candidates.append(DATA_DIR / "openai_config.json")
    candidates.append(OPENAI_FALLBACK_CONFIG)

    payload: dict[str, Any] = {}
    source = "environment"
    for path in candidates:
        if not path.exists():
            continue
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            source = str(path)
            break

    return OpenAISettings(
        api_key=os.getenv("OPENAI_API_KEY", str(payload.get("api_key") or "")),
        model=os.getenv("OPENAI_MODEL", str(payload.get("model") or "gpt-5-mini")),
        base_url=os.getenv("OPENAI_BASE_URL", str(payload.get("base_url") or "")),
        source=source,
    )


class TopicAIService:
    def __init__(self, db: Database):
        self.db = db

    def status(self) -> dict[str, Any]:
        settings = self._resolved_settings()
        return {
            "configured": settings.configured,
            "model": settings.model,
            "base_url_configured": bool(settings.base_url),
            "config_source": settings.source,
            "prompt_version": AI_PROMPT_VERSION,
            "model_tier": settings.model_tier,
            "model_tiers": AI_MODEL_TIERS,
            "output_language": settings.output_language,
            "output_languages": list(AI_OUTPUT_LANGUAGES),
        }

    def translate_topic(self, topic_id: int, target_language: str) -> dict[str, Any]:
        if target_language not in AI_OUTPUT_LANGUAGES:
            raise ValueError("Target language must be chinese or english.")
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversation_topics WHERE id=?", (topic_id,)
            ).fetchone()
        if row is None:
            raise LookupError("Topic not found.")

        payload = self._topic_translation_payload(row)
        source_language = str(row["source_language"] or "unknown")
        if target_language == source_language:
            return {
                "topic_id": topic_id,
                "target_language": target_language,
                "cached": True,
                "translation": payload,
            }

        settings = self._resolved_settings()
        if not settings.configured:
            raise RuntimeError("OpenAI API key is not configured.")
        source_hash = self._translation_hash(payload, target_language)
        with self.db.connect() as connection:
            cached = connection.execute(
                """SELECT payload_json FROM topic_translations
                   WHERE topic_id=? AND target_language=? AND source_hash=?""",
                (topic_id, target_language, source_hash),
            ).fetchone()
        if cached:
            return {
                "topic_id": topic_id,
                "target_language": target_language,
                "cached": True,
                "translation": json.loads(cached["payload_json"]),
            }

        translated = self._translate_topic(payload, target_language, settings)
        translated_payload = translated.model_dump()
        with self.db.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO topic_translations(
                     topic_id,target_language,source_hash,payload_json,model,prompt_version,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    topic_id, target_language, source_hash,
                    json.dumps(translated_payload, ensure_ascii=False), settings.model,
                    TOPIC_TRANSLATION_PROMPT_VERSION, now_iso(),
                ),
            )
        return {
            "topic_id": topic_id,
            "target_language": target_language,
            "cached": False,
            "translation": translated_payload,
        }

    @staticmethod
    def _topic_translation_payload(row: Any) -> dict[str, Any]:
        return {
            "title": str(row["title"] or ""),
            "problem_summary": str(row["problem_summary"] or ""),
            "context_summary": str(row["context_summary"] or ""),
            "discussion_summary": str(row["discussion_summary"] or ""),
            "confirmed_facts": json.loads(row["confirmed_facts_json"] or "[]"),
            "conclusions": json.loads(row["conclusions_json"] or "[]"),
            "open_questions": json.loads(row["open_questions_json"] or "[]"),
            "action_items": json.loads(row["action_items_json"] or "[]"),
            "tags": json.loads(row["tags_json"] or "[]"),
        }

    @staticmethod
    def _translation_hash(payload: dict[str, Any], target_language: str) -> str:
        material = {
            "prompt_version": TOPIC_TRANSLATION_PROMPT_VERSION,
            "target_language": target_language,
            "topic": payload,
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _translate_topic(
        self,
        payload: dict[str, Any],
        target_language: str,
        settings: OpenAISettings,
    ) -> TopicTranslationResult:
        from openai import OpenAI

        client_kwargs: dict[str, Any] = {
            "api_key": settings.api_key,
            "timeout": 90.0,
            "max_retries": 2,
        }
        if settings.base_url:
            client_kwargs["base_url"] = settings.base_url
        client = OpenAI(**client_kwargs)
        instructions = TRANSLATION_PROMPT_PATH.read_text(encoding="utf-8")
        request = {"target_language": target_language, "topic": payload}
        response = client.responses.parse(
            model=settings.model,
            instructions=instructions,
            input=json.dumps(request, ensure_ascii=False),
            text_format=TopicTranslationResult,
            store=False,
        )
        if response.output_parsed is None:
            raise RuntimeError("OpenAI returned no translated topic output.")
        return response.output_parsed

    def _decide_continuation(
        self,
        topic: dict[str, Any],
        candidates: list[dict[str, Any]],
        settings: OpenAISettings,
    ) -> TopicContinuationDecision:
        from openai import OpenAI

        client_kwargs: dict[str, Any] = {
            "api_key": settings.api_key,
            "timeout": 90.0,
            "max_retries": 2,
        }
        if settings.base_url:
            client_kwargs["base_url"] = settings.base_url
        client = OpenAI(**client_kwargs)
        payload = {
            "version": TOPIC_CONTINUATION_PROMPT_VERSION,
            "new_topic": topic,
            "candidates": candidates,
        }
        response = client.responses.parse(
            model=settings.model,
            instructions=CONTINUATION_PROMPT_PATH.read_text(encoding="utf-8"),
            input=json.dumps(payload, ensure_ascii=False),
            text_format=TopicContinuationDecision,
            store=False,
        )
        if response.output_parsed is None:
            raise RuntimeError("OpenAI returned no topic continuation decision.")
        return response.output_parsed

    def _resolved_settings(self) -> OpenAISettings:
        settings = load_openai_settings()
        tier = self.db.get_setting("ai_model_tier") or DEFAULT_AI_MODEL_TIER
        if tier not in AI_MODEL_TIERS:
            tier = DEFAULT_AI_MODEL_TIER
        language = self.db.get_setting("ai_output_language") or DEFAULT_AI_OUTPUT_LANGUAGE
        if language not in AI_OUTPUT_LANGUAGES:
            language = DEFAULT_AI_OUTPUT_LANGUAGE
        return replace(
            settings,
            model=AI_MODEL_TIERS[tier],
            model_tier=tier,
            output_language=language,
        )

    def analyze_recent(
        self,
        days: int = 7,
        channel_ids: list[str] | None = None,
        max_windows: int = 8,
    ) -> dict[str, Any]:
        settings = self._resolved_settings()
        if not settings.configured:
            raise RuntimeError("OpenAI API key is not configured.")
        windows = self.build_windows(days, channel_ids)
        processed = skipped = topic_count = merged_count = 0
        errors: list[dict[str, str]] = []
        attempted = 0
        for window in windows:
            source_language = detect_source_language(window["messages"])
            window_settings = replace(settings, output_language=source_language)
            input_hash = self._window_hash(window, source_language)
            if self._already_succeeded(input_hash):
                skipped += 1
                continue
            if attempted >= max_windows:
                break
            attempted += 1
            run_id = self._start_run(window, input_hash, settings.model)
            try:
                result, metadata = self._extract(window, window_settings)
                stored_ids = self._store_result(run_id, window, result, settings.model)
                topic_count += len(stored_ids)
                self._finish_run(run_id, "succeeded", metadata=metadata)
                processed += 1
                for topic_id in stored_ids:
                    try:
                        merged_count += int(self._merge_semantic_continuation(topic_id, settings))
                    except Exception as merge_error:
                        errors.append({
                            "channel": window["channel_name"],
                            "error": f"Topic continuation check failed: {str(merge_error)[:220]}",
                        })
            except Exception as error:
                self._finish_run(run_id, "failed", error=str(error)[:1000])
                errors.append({"channel": window["channel_name"], "error": str(error)[:300]})
        return {
            "windows": len(windows),
            "processed": processed,
            "skipped": skipped,
            "topics": topic_count,
            "merged_topics": merged_count,
            "errors": errors,
        }

    def build_windows(self, days: int | None, channel_ids: list[str] | None = None) -> list[dict[str, Any]]:
        clauses = ["c.selected=1", "c.is_active=1", "m.body_state='readable'"]
        parameters: list[Any] = []
        if days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            clauses.append("m.sent_at>=?")
            parameters.append(cutoff)
        if channel_ids:
            placeholders = ",".join("?" for _ in channel_ids)
            clauses.append(f"c.id IN ({placeholders})")
            parameters.extend(channel_ids)
        with self.db.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT m.id local_id,m.zoom_message_id,m.channel_id,m.sender_name,m.sent_at,
                    m.body,m.thread_id,m.reply_to_message_id,c.name channel_name
                    FROM messages m JOIN channels c ON c.id=m.channel_id
                    WHERE {' AND '.join(clauses)} ORDER BY m.channel_id,m.sent_at""",
                parameters,
            )]

        # A Zoom thread is one conversation even when replies arrive days later.
        # Root messages do not carry thread_id, so discover roots referenced by replies.
        referenced_roots = {
            (row["channel_id"], root)
            for row in rows
            for root in (row["thread_id"], row["reply_to_message_id"])
            if root
        }
        thread_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        timeline_rows: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            root = row["thread_id"] or row["reply_to_message_id"]
            if root:
                thread_groups.setdefault((row["channel_id"], root), []).append(row)
            else:
                # Keep root messages in their local timeline cluster. Zoom may not
                # mark nearby channel replies as thread replies even though they
                # are part of the same visible discussion.
                timeline_rows.setdefault(row["channel_id"], []).append(row)

        windows: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        current_chars = 0
        timeline = [row for channel_rows in timeline_rows.values() for row in channel_rows]
        timeline.sort(key=lambda item: (item["channel_id"], item["sent_at"]))
        for row in timeline:
            sent_at = parse_zoom_time(row["sent_at"])
            body_length = len(row["body"] or "")
            should_split = False
            if current:
                previous_time = parse_zoom_time(current["messages"][-1]["sent_at"])
                should_split = (
                    current["channel_id"] != row["channel_id"]
                    or (sent_at and previous_time and sent_at - previous_time > timedelta(hours=AI_WINDOW_GAP_HOURS))
                    or len(current["messages"]) >= AI_WINDOW_MAX_MESSAGES
                    or current_chars + body_length > AI_WINDOW_MAX_CHARS
                )
            if current is None or should_split:
                if current:
                    windows.append(current)
                current = {
                    "window_key": f"timeline:{row['channel_id']}:{row['zoom_message_id']}",
                    "kind": "timeline",
                    "channel_id": row["channel_id"],
                    "channel_name": row["channel_name"],
                    "messages": [],
                }
                current_chars = 0
            current["messages"].append(row)
            current_chars += body_length
        if current:
            windows.append(current)

        # Merge each root's entire local conversation cluster with later explicit
        # thread replies. This preserves nearby unthreaded answers visible in Zoom.
        merged_windows: list[dict[str, Any]] = []
        for window in windows:
            roots = [
                item["zoom_message_id"]
                for item in window["messages"]
                if (window["channel_id"], item["zoom_message_id"]) in referenced_roots
            ]
            if not roots:
                merged_windows.append(window)
                continue
            combined = list(window["messages"])
            for root in roots:
                combined.extend(thread_groups.pop((window["channel_id"], root), []))
            unique = {item["zoom_message_id"]: item for item in combined}
            window["messages"] = sorted(unique.values(), key=lambda item: item["sent_at"])
            window["kind"] = "thread"
            window["window_key"] = f"thread:{window['channel_id']}:{','.join(sorted(roots))}"
            merged_windows.append(window)

        # A reply can be inside the selected date range while its root is outside it.
        for (channel_id, root), messages in thread_groups.items():
            messages.sort(key=lambda item: item["sent_at"])
            merged_windows.append({
                "window_key": f"thread:{channel_id}:{root}",
                "kind": "thread",
                "channel_id": channel_id,
                "channel_name": messages[0]["channel_name"],
                "messages": messages,
            })
        windows = merged_windows
        windows.sort(key=lambda item: (item["channel_id"], item["messages"][0]["sent_at"]))
        return windows

    def _extract(self, window: dict[str, Any], settings: OpenAISettings) -> tuple[TopicExtractionResult, dict[str, Any]]:
        from openai import OpenAI

        client_kwargs: dict[str, Any] = {
            "api_key": settings.api_key,
            "timeout": 90.0,
            "max_retries": 2,
        }
        if settings.base_url:
            client_kwargs["base_url"] = settings.base_url
        client = OpenAI(**client_kwargs)
        language_rules = {
            "english": "Write every human-readable output field in English.",
            "chinese": "Write every human-readable output field in Simplified Chinese.",
        }
        prompt = PROMPT_PATH.read_text(encoding="utf-8") + "\n\n# Output language\n" + language_rules[settings.output_language]
        payload = {
            "channel": window["channel_name"],
            "messages": [
                {
                    "message_id": item["zoom_message_id"],
                    "sent_at": item["sent_at"],
                    "sender": item["sender_name"] or "Unknown",
                    "thread_id": item["thread_id"] or "",
                    "reply_to_message_id": item["reply_to_message_id"] or "",
                    "text": item["body"],
                }
                for item in window["messages"]
            ],
        }
        response = client.responses.parse(
            model=settings.model,
            instructions=prompt,
            input=json.dumps(payload, ensure_ascii=False),
            text_format=TopicExtractionResult,
            store=False,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI returned no parsed topic output.")
        usage = getattr(response, "usage", None)
        metadata = {
            "response_id": getattr(response, "id", None),
            "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
            "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        }
        return parsed, metadata

    def _store_result(
        self,
        run_id: int,
        window: dict[str, Any],
        result: TopicExtractionResult,
        model: str,
    ) -> list[int]:
        message_map = {item["zoom_message_id"]: item for item in window["messages"]}
        stored_ids: list[int] = []
        stamp = now_iso()
        with self.db.connect() as connection:
            for topic in result.topics:
                source_rows = [message_map[item] for item in dict.fromkeys(topic.source_message_ids) if item in message_map]
                if not source_rows:
                    continue
                first_at = min(item["sent_at"] for item in source_rows)
                last_at = max(item["sent_at"] for item in source_rows)
                cursor = connection.execute(
                    """INSERT INTO conversation_topics(
                         ai_run_id,channel_id,title,problem_summary,context_summary,discussion_summary,
                         confirmed_facts_json,conclusions_json,open_questions_json,action_items_json,tags_json,
                         status,confidence,first_message_at,last_message_at,source_message_count,
                         ai_model,prompt_version,source_language,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, window["channel_id"], topic.title.strip(), topic.problem_summary.strip(),
                        topic.context_summary.strip(), topic.discussion_summary.strip(),
                        json.dumps(topic.confirmed_facts, ensure_ascii=False),
                        json.dumps(topic.conclusions, ensure_ascii=False),
                        json.dumps(topic.open_questions, ensure_ascii=False),
                        json.dumps([item.model_dump() for item in topic.action_items], ensure_ascii=False),
                        json.dumps(topic.tags, ensure_ascii=False), topic.status, topic.confidence,
                        first_at, last_at, len(source_rows), model, AI_PROMPT_VERSION,
                        detect_source_language(source_rows), stamp, stamp,
                    ),
                )
                topic_id = cursor.lastrowid
                connection.executemany(
                    "INSERT INTO topic_sources(topic_id,message_id) VALUES(?,?)",
                    [(topic_id, item["local_id"]) for item in source_rows],
                )
                self._merge_source_overlaps(connection, int(topic_id), window["channel_id"])
                stored_ids.append(int(topic_id))
        return stored_ids

    @staticmethod
    def _merge_topics(
        connection: Any,
        source_topic_id: int,
        target_topic_id: int,
        method: str,
        confidence: float,
        reason: str,
    ) -> None:
        source = connection.execute(
            "SELECT keep_tracking,ignored_at FROM conversation_topics WHERE id=?",
            (source_topic_id,),
        ).fetchone()
        target = connection.execute(
            "SELECT keep_tracking,ignored_at FROM conversation_topics WHERE id=?",
            (target_topic_id,),
        ).fetchone()
        if not source or not target:
            return
        connection.execute(
            """INSERT OR IGNORE INTO topic_sources(topic_id,message_id)
               SELECT ?,message_id FROM topic_sources WHERE topic_id=?""",
            (target_topic_id, source_topic_id),
        )
        source_stats = connection.execute(
            """SELECT MIN(m.sent_at) first_at,MAX(m.sent_at) last_at,COUNT(*) source_count
               FROM topic_sources ts JOIN messages m ON m.id=ts.message_id WHERE ts.topic_id=?""",
            (target_topic_id,),
        ).fetchone()
        merged_ignored = target["ignored_at"] or source["ignored_at"]
        merged_keep = int(bool(target["keep_tracking"] or source["keep_tracking"]) and not merged_ignored)
        stamp = now_iso()
        connection.execute(
            """UPDATE conversation_topics
               SET keep_tracking=?,ignored_at=?,first_message_at=?,last_message_at=?,
                   source_message_count=?,updated_at=? WHERE id=?""",
            (
                merged_keep, merged_ignored, source_stats["first_at"], source_stats["last_at"],
                source_stats["source_count"], stamp, target_topic_id,
            ),
        )
        connection.execute(
            """INSERT OR IGNORE INTO knowledge_topic_sources(knowledge_id,topic_id)
               SELECT knowledge_id,? FROM knowledge_topic_sources WHERE topic_id=?""",
            (target_topic_id, source_topic_id),
        )
        connection.execute(
            """UPDATE knowledge_items SET has_new_activity=1,updated_at=?
               WHERE id IN (SELECT knowledge_id FROM knowledge_topic_sources WHERE topic_id=?)""",
            (stamp, source_topic_id),
        )
        connection.execute(
            "UPDATE conversation_topics SET superseded_by_id=?,updated_at=? WHERE id=?",
            (target_topic_id, stamp, source_topic_id),
        )
        connection.execute(
            """INSERT OR IGNORE INTO topic_merge_events(
                 source_topic_id,target_topic_id,method,confidence,reason,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (source_topic_id, target_topic_id, method, confidence, reason[:500], stamp),
        )

    @classmethod
    def _merge_source_overlaps(cls, connection: Any, topic_id: int, channel_id: str) -> int:
        target_row = connection.execute(
            "SELECT * FROM conversation_topics WHERE id=? AND superseded_by_id IS NULL", (topic_id,)
        ).fetchone()
        if not target_row:
            return 0
        target_features = cls._topic_features(dict(target_row))
        new_sources = {
            int(row["message_id"])
            for row in connection.execute("SELECT message_id FROM topic_sources WHERE topic_id=?", (topic_id,))
        }
        candidates = connection.execute(
            """SELECT * FROM conversation_topics
               WHERE channel_id=? AND id<? AND review_status='unreviewed' AND superseded_by_id IS NULL""",
            (channel_id, topic_id),
        ).fetchall()
        merged = 0
        for candidate in candidates:
            old_sources = {
                int(row["message_id"])
                for row in connection.execute("SELECT message_id FROM topic_sources WHERE topic_id=?", (candidate["id"],))
            }
            overlap = len(old_sources & new_sources)
            similarity = overlap / len(old_sources | new_sources) if old_sources else 0
            contained = bool(old_sources) and old_sources.issubset(new_sources)
            candidate_features = cls._topic_features(dict(candidate))
            shared_features = target_features & candidate_features
            topic_similarity = (
                len(shared_features) / min(len(target_features), len(candidate_features))
                if target_features and candidate_features else 0
            )
            if candidate["ignored_at"] and similarity >= 0.5:
                connection.execute(
                    "UPDATE conversation_topics SET ignored_at=?,keep_tracking=0 WHERE id=?",
                    (candidate["ignored_at"], topic_id),
                )
            should_merge = similarity >= 0.95 or (
                (contained or similarity >= TOPIC_SOURCE_OVERLAP_MERGE_THRESHOLD)
                and len(shared_features) >= 2
                and topic_similarity >= 0.12
            )
            if should_merge:
                cls._merge_topics(
                    connection,
                    int(candidate["id"]),
                    topic_id,
                    "source_overlap",
                    1.0 if contained else similarity,
                    (
                        "Source message set was contained."
                        if contained
                        else f"Source-message Jaccard overlap was {similarity:.3f}; topic-feature similarity was {topic_similarity:.3f}."
                    ),
                )
                new_sources |= old_sources
                target_features |= candidate_features
                merged += 1
        return merged

    @staticmethod
    def _topic_features(topic: dict[str, Any]) -> set[str]:
        text = " ".join(str(topic.get(key) or "") for key in (
            "title", "problem_summary", "context_summary", "discussion_summary", "tags_json",
        )).casefold()
        ascii_tokens = {
            token for token in re.findall(r"[a-z0-9][a-z0-9+._-]+", text)
            if len(token) >= 2 and token not in {"the", "and", "for", "with", "from", "this", "that", "issue", "problem"}
        }
        chinese_tokens: set[str] = set()
        for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
            chinese_tokens.update(chunk[index:index + 2] for index in range(max(0, len(chunk) - 1)))
        return ascii_tokens | chinese_tokens

    def _continuation_candidates(self, topic_id: int, limit: int = 5) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM conversation_topics WHERE id=?", (topic_id,)).fetchone()
            if not row:
                return None, []
            topic = dict(row)
            if topic["ignored_at"] or topic["superseded_by_id"]:
                return topic, []
            rows = [dict(item) for item in connection.execute(
                """SELECT * FROM conversation_topics
                   WHERE channel_id=? AND id<? AND superseded_by_id IS NULL AND ignored_at IS NULL
                   ORDER BY last_message_at DESC LIMIT 100""",
                (topic["channel_id"], topic_id),
            )]
        topic_features = self._topic_features(topic)
        topic_first = parse_zoom_time(topic["first_message_at"])
        ranked: list[tuple[float, dict[str, Any]]] = []
        for candidate in rows:
            candidate_last = parse_zoom_time(candidate["last_message_at"])
            if not topic_first or not candidate_last:
                continue
            gap = topic_first - candidate_last
            if gap < -timedelta(days=1) or gap > timedelta(days=TOPIC_CONTINUATION_LOOKBACK_DAYS):
                continue
            candidate_features = self._topic_features(candidate)
            shared = topic_features & candidate_features
            score = len(shared) / min(len(topic_features), len(candidate_features)) if topic_features and candidate_features else 0
            if len(shared) < 2 or score < 0.12:
                continue
            candidate["candidate_score"] = round(score, 4)
            ranked.append((score, candidate))
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
        return topic, [item[1] for item in ranked[:limit]]

    @staticmethod
    def _continuation_payload(topic: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": int(topic["id"]),
            "title": topic["title"],
            "problem_summary": topic["problem_summary"],
            "context_summary": topic["context_summary"],
            "discussion_summary": topic["discussion_summary"],
            "tags": json.loads(topic.get("tags_json") or "[]"),
            "first_message_at": topic["first_message_at"],
            "last_message_at": topic["last_message_at"],
            "candidate_score": topic.get("candidate_score"),
        }

    def _merge_semantic_continuation(self, topic_id: int, settings: OpenAISettings) -> bool:
        topic, candidates = self._continuation_candidates(topic_id)
        if not topic or not candidates:
            return False
        decision = self._decide_continuation(
            self._continuation_payload(topic),
            [self._continuation_payload(candidate) for candidate in candidates],
            settings,
        )
        candidate_ids = {int(candidate["id"]) for candidate in candidates}
        if (
            not decision.same_issue
            or decision.candidate_id not in candidate_ids
            or decision.confidence < TOPIC_CONTINUATION_MIN_CONFIDENCE
        ):
            return False
        with self.db.connect() as connection:
            candidate = connection.execute(
                "SELECT superseded_by_id,ignored_at FROM conversation_topics WHERE id=?",
                (decision.candidate_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT superseded_by_id FROM conversation_topics WHERE id=?", (topic_id,)
            ).fetchone()
            if not candidate or candidate["superseded_by_id"] or candidate["ignored_at"] or not target or target["superseded_by_id"]:
                return False
            self._merge_topics(
                connection,
                decision.candidate_id,
                topic_id,
                "semantic_continuation",
                decision.confidence,
                decision.reason,
            )
        return True

    def consolidate_topic(self, topic_id: int) -> dict[str, int]:
        settings = self._resolved_settings()
        if not settings.configured:
            raise RuntimeError("OpenAI API key is not configured.")
        _, initial_candidates = self._continuation_candidates(topic_id, limit=20)
        candidate_ids = sorted((int(item["id"]) for item in initial_candidates), reverse=True)
        with self.db.connect() as connection:
            row = connection.execute("SELECT channel_id FROM conversation_topics WHERE id=?", (topic_id,)).fetchone()
            if not row:
                raise LookupError("Topic not found.")
            channel_id = row["channel_id"]
            overlap_merges = sum(
                self._merge_source_overlaps(connection, candidate_id, channel_id)
                for candidate_id in candidate_ids
            )
        semantic_merges = int(self._merge_semantic_continuation(topic_id, settings))
        return {"source_overlap": overlap_merges, "semantic_continuation": semantic_merges}

    def _window_hash(self, window: dict[str, Any], output_language: str | None = None) -> str:
        settings = self._resolved_settings()
        material = {
            "prompt_version": AI_PROMPT_VERSION,
            "model": settings.model,
            "output_language": output_language or settings.output_language,
            "window_key": window["window_key"],
            "channel_id": window["channel_id"],
            "messages": [
                [item["zoom_message_id"], item["sent_at"], item["sender_name"], item["body"]]
                for item in window["messages"]
            ],
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    def _already_succeeded(self, input_hash: str) -> bool:
        with self.db.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM ai_runs WHERE input_hash=? AND prompt_version=? AND status='succeeded'",
                (input_hash, AI_PROMPT_VERSION),
            ).fetchone() is not None

    def _start_run(self, window: dict[str, Any], input_hash: str, model: str) -> int:
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM ai_runs WHERE input_hash=? AND prompt_version=?",
                (input_hash, AI_PROMPT_VERSION),
            ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE ai_runs SET status='running',model=?,message_count=?,error=NULL,
                       response_id=NULL,input_tokens=NULL,output_tokens=NULL,started_at=?,finished_at=NULL WHERE id=?""",
                    (model, len(window["messages"]), now_iso(), existing["id"]),
                )
                connection.execute("DELETE FROM conversation_topics WHERE ai_run_id=?", (existing["id"],))
                return int(existing["id"])
            return int(connection.execute(
                """INSERT INTO ai_runs(input_hash,prompt_version,model,channel_id,status,message_count,started_at)
                   VALUES(?,?,?,?,'running',?,?)""",
                (input_hash, AI_PROMPT_VERSION, model, window["channel_id"], len(window["messages"]), now_iso()),
            ).lastrowid)

    def _finish_run(
        self,
        run_id: int,
        status: str,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        metadata = metadata or {}
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE ai_runs SET status=?,response_id=?,input_tokens=?,output_tokens=?,error=?,finished_at=?
                   WHERE id=?""",
                (
                    status, metadata.get("response_id"), metadata.get("input_tokens"),
                    metadata.get("output_tokens"), error, now_iso(), run_id,
                ),
            )


def detect_source_language(messages: list[dict[str, Any]]) -> Literal["chinese", "english"]:
    """Use the earliest thread root/question language, falling back to the full conversation."""
    ordered = sorted(messages, key=lambda item: str(item.get("sent_at") or ""))
    roots = [item for item in ordered if not item.get("thread_id") and not item.get("reply_to_message_id")]
    primary = next((str(item.get("body") or "") for item in roots if str(item.get("body") or "").strip()), "")
    text = primary or "\n".join(str(item.get("body") or "") for item in ordered)
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    return "chinese" if chinese >= 3 and chinese * 2 >= latin else "english"

