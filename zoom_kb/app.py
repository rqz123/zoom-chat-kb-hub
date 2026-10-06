from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .ai_topics import TopicAIService
from .config import (
    AI_MODEL_TIERS,
    AI_OUTPUT_LANGUAGES,
    ALLOWED_INITIAL_SYNC_DAYS,
    DB_PATH,
    DEFAULT_AI_MODEL_TIER,
    DEFAULT_AI_OUTPUT_LANGUAGE,
    LEGACY_TOKEN_PATH,
    STATIC_DIR,
    TOKEN_PATH,
    ZOOM_CLIENT_ID,
    ALLOWED_KNOWLEDGE_MATURITY_DAYS,
    DEFAULT_KNOWLEDGE_MATURITY_DAYS,
)
from .db import Database
from .knowledge import KnowledgeService, normalize_question
from .knowledge_pipeline import KnowledgePipeline
from .sync import SyncService, now_iso
from .token_store import TokenStore
from .oauth import oauth_router
from .zoom_client import ZoomClient


db = Database(DB_PATH)
tokens = TokenStore(TOKEN_PATH, LEGACY_TOKEN_PATH)
client = ZoomClient(tokens, ZOOM_CLIENT_ID)
sync_service = SyncService(db, client)
knowledge_service = KnowledgeService(db)
topic_ai_service = TopicAIService(db)
knowledge_pipeline = KnowledgePipeline(db, topic_ai_service)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    tokens.import_legacy_if_needed()
    yield


app = FastAPI(title="Zoom Chat Knowledge Hub", version="0.2.0", lifespan=lifespan)
app.include_router(oauth_router(tokens, ZOOM_CLIENT_ID))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def revalidate_local_frontend(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class SelectionUpdate(BaseModel):
    selected: bool


class ProbeRequest(BaseModel):
    channel_ids: list[str] | None = None
    days: int = 30


class SyncDaysUpdate(BaseModel):
    days: int


class BackfillRequest(BaseModel):
    days: int


class IdentityUpdate(BaseModel):
    member_id: str = ""
    display_name: str = ""


class MentionUpdate(BaseModel):
    status: str
    note: str = ""


class KnowledgeUpdate(BaseModel):
    status: str
    answer: str = ""
    category: str = "uncategorized"
    problem_summary: str = ""
    conclusion_summary: str = ""
    context_summary: str = ""


class TopicAnalyzeRequest(BaseModel):
    channel_ids: list[str] | None = None
    max_windows: int = 8


class TopicReviewUpdate(BaseModel):
    review_status: str


class TopicTrackingUpdate(BaseModel):
    enabled: bool


class TopicTranslationRequest(BaseModel):
    target_language: str


class TopicInternalNoteRequest(BaseModel):
    note: str


class AISettingsUpdate(BaseModel):
    model_tier: str
    output_language: str


class KnowledgeSettingsUpdate(BaseModel):
    maturity_days: int


class HistoricalImportRequest(BaseModel):
    max_windows: int = 8


class KnowledgeSearchRequest(BaseModel):
    query: str
    limit: int = 10


def _parse_sync_detail(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        parsed = {}
    if isinstance(parsed, list):
        return {"channels": [], "errors": parsed}
    if not isinstance(parsed, dict):
        return {"channels": [], "errors": []}
    parsed.setdefault("channels", [])
    parsed.setdefault("errors", [])
    return parsed


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "authorized": tokens.exists(), "database": str(DB_PATH)}


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    with db.connect() as connection:
        channel_stats = {
            row["readability_status"]: row["count"]
            for row in connection.execute(
                "SELECT readability_status, COUNT(*) count FROM channels WHERE is_active=1 GROUP BY readability_status"
            )
        }
        selected = connection.execute("SELECT COUNT(*) count FROM channels WHERE selected=1 AND is_active=1").fetchone()["count"]
        messages = connection.execute("SELECT COUNT(*) count FROM messages").fetchone()["count"]
        mentions = connection.execute("SELECT COUNT(*) count FROM mention_tasks WHERE status!='done'").fetchone()["count"]
        run = connection.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    last_run = None
    if run:
        last_run = dict(run)
        detail = _parse_sync_detail(last_run.get("detail"))
        channel_results = detail.get("channels") if isinstance(detail.get("channels"), list) else []
        last_run["channel_results"] = channel_results
        last_run["updated_channels"] = [
            item for item in channel_results
            if isinstance(item, dict) and int(item.get("message_count") or 0) > 0
        ]
        last_run["channel_errors"] = detail.get("errors") if isinstance(detail.get("errors"), list) else []
        last_run["knowledge_archive"] = (
            detail.get("knowledge_archive") if isinstance(detail.get("knowledge_archive"), dict) else {}
        )
        last_run.pop("detail", None)
    return {
        "channels": channel_stats,
        "selected_channels": selected,
        "messages": messages,
        "open_mentions": mentions,
        "last_run": last_run,
    }


@app.get("/api/channels")
def list_channels(search: str = "", status: str = "") -> list[dict[str, Any]]:
    clauses = ["is_active=1"]
    parameters: list[Any] = []
    if search:
        clauses.append("name LIKE ?")
        parameters.append(f"%{search}%")
    if status:
        clauses.append("readability_status=?")
        parameters.append(status)
    sql = f"""SELECT c.*, s.last_sync_at, s.last_error,s.initial_sync_completed_at,
              s.covered_from_at,s.sync_watermark_at,s.last_success_at,
              CASE
                WHEN s.initial_sync_completed_at IS NULL AND s.last_error IS NOT NULL THEN 'initial_failed'
                WHEN s.initial_sync_completed_at IS NULL THEN 'not_started'
                WHEN s.last_error IS NOT NULL THEN 'incremental_failed'
                ELSE 'incremental'
              END sync_stage,
              (SELECT COUNT(*) FROM messages m WHERE m.channel_id=c.id) message_count
              FROM channels c LEFT JOIN sync_cursors s ON s.channel_id=c.id
              WHERE {' AND '.join(clauses)} ORDER BY c.selected DESC, c.name COLLATE NOCASE"""
    with db.connect() as connection:
        return [dict(row) for row in connection.execute(sql, parameters)]


@app.post("/api/channels/refresh")
def refresh_channels() -> dict[str, int]:
    _require_auth()
    try:
        return {"count": sync_service.refresh_channels()}
    except Exception as error:
        _raise_safe(error)


@app.patch("/api/channels/{channel_id}")
def update_channel(channel_id: str, body: SelectionUpdate) -> dict[str, Any]:
    with db.connect() as connection:
        cursor = connection.execute("UPDATE channels SET selected=? WHERE id=?", (int(body.selected), channel_id))
        if cursor.rowcount == 0:
            raise HTTPException(404, "Channel not found.")
    return {"id": channel_id, "selected": body.selected}


@app.post("/api/channels/probe")
def probe_channels(body: ProbeRequest) -> dict[str, Any]:
    _require_auth()
    try:
        results = sync_service.probe(body.channel_ids, max(1, min(body.days, 90)))
        return {"count": len(results), "results": results}
    except Exception as error:
        _raise_safe(error)


@app.post("/api/channels/{channel_id}/backfill")
def backfill_channel(channel_id: str, body: BackfillRequest) -> dict[str, Any]:
    _require_auth()
    if body.days not in ALLOWED_INITIAL_SYNC_DAYS:
        raise HTTPException(400, "Backfill days must be 30, 90, or 180.")
    try:
        return sync_service.backfill_channel(channel_id, body.days)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        _raise_safe(error)


@app.post("/api/sync-runs")
def run_sync() -> dict[str, Any]:
    _require_auth()
    try:
        result = sync_service.sync_selected()
        try:
            result["topic_extraction"] = topic_ai_service.analyze_recent(
                days=knowledge_pipeline.maturity_days(), max_windows=3
            )
        except Exception as error:
            result["topic_extraction"] = {"processed": 0, "topics": 0, "errors": [str(error)[:300]]}
        result["knowledge_archive"] = knowledge_pipeline.archive_mature_topics(max_topics=8)
        with db.connect() as connection:
            row = connection.execute(
                "SELECT detail FROM sync_runs WHERE id=?", (result["run_id"],)
            ).fetchone()
            detail = _parse_sync_detail(row["detail"] if row else None)
            detail["knowledge_archive"] = result["knowledge_archive"]
            connection.execute(
                "UPDATE sync_runs SET detail=? WHERE id=?",
                (json.dumps(detail, ensure_ascii=False), result["run_id"]),
            )
        return result
    except Exception as error:
        _raise_safe(error)


@app.get("/api/sync-runs")
def list_runs(limit: int = Query(10, ge=1, le=100)) -> list[dict[str, Any]]:
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = _parse_sync_detail(item["detail"])
        result.append(item)
    return result


@app.get("/api/messages")
def list_messages(channel_id: str = "", search: str = "", limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    parameters: list[Any] = []
    if channel_id:
        clauses.append("m.channel_id=?")
        parameters.append(channel_id)
    if search:
        clauses.append("m.body LIKE ?")
        parameters.append(f"%{search}%")
    parameters.append(limit)
    with db.connect() as connection:
        rows = connection.execute(
            f"""SELECT m.id,m.zoom_message_id,m.sender_name,m.sent_at,m.body,m.body_state,m.thread_id,
                c.name channel_name,c.id channel_id FROM messages m JOIN channels c ON c.id=m.channel_id
                WHERE {' AND '.join(clauses)} ORDER BY m.sent_at DESC LIMIT ?""", parameters
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/ai/status")
def ai_status() -> dict[str, Any]:
    return topic_ai_service.status()


@app.post("/api/topics/analyze")
def analyze_topics(body: TopicAnalyzeRequest) -> dict[str, Any]:
    if not 1 <= body.max_windows <= 20:
        raise HTTPException(400, "max_windows must be between 1 and 20.")
    try:
        days = knowledge_pipeline.maturity_days()
        result = topic_ai_service.analyze_recent(days, body.channel_ids, body.max_windows)
        result["analysis_days"] = days
        return result
    except RuntimeError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        _raise_safe(error)


@app.get("/api/topics")
def list_topics(search: str = "", status: str = "", limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    clauses = [
        "t.superseded_by_id IS NULL",
        "t.ignored_at IS NULL",
        "(t.keep_tracking=1 OR (t.archived_at IS NULL AND t.last_message_at>?))",
    ]
    parameters: list[Any] = [knowledge_pipeline.cutoff()]
    if search:
        clauses.append("(t.title LIKE ? OR t.problem_summary LIKE ? OR t.discussion_summary LIKE ? OR t.internal_context_summary LIKE ?)")
        parameters.extend([f"%{search}%"] * 4)
    if status:
        clauses.append("t.status=?")
        parameters.append(status)
    parameters.append(limit)
    with db.connect() as connection:
        rows = connection.execute(
            f"""SELECT t.*,c.name channel_name,
                (SELECT kts.knowledge_id FROM knowledge_topic_sources kts WHERE kts.topic_id=t.id LIMIT 1) knowledge_id,
                (SELECT COUNT(*) FROM topic_internal_notes tin WHERE tin.topic_id=t.id) internal_note_count
                FROM conversation_topics t
                JOIN channels c ON c.id=t.channel_id WHERE {' AND '.join(clauses)}
                ORDER BY t.last_message_at DESC,t.id DESC LIMIT ?""",
            parameters,
        ).fetchall()
    return [_topic_row(row) for row in rows]


@app.get("/api/topics/{topic_id}/search-knowledge")
def search_knowledge_for_topic(topic_id: int, limit: int = Query(5, ge=1, le=20)) -> list[dict[str, Any]]:
    with db.connect() as connection:
        topic = connection.execute(
            "SELECT title,problem_summary,context_summary FROM conversation_topics WHERE id=?", (topic_id,)
        ).fetchone()
    if not topic:
        raise HTTPException(404, "Topic not found.")
    query = "\n".join(str(topic[key] or "") for key in ("title", "problem_summary", "context_summary"))
    return knowledge_pipeline.search(query, limit=limit, translate=True)


@app.post("/api/topics/{topic_id}/translate")
def translate_topic(topic_id: int, body: TopicTranslationRequest) -> dict[str, Any]:
    try:
        return topic_ai_service.translate_topic(topic_id, body.target_language)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        _raise_safe(error)


@app.post("/api/topics/{topic_id}/internal-notes")
def add_topic_internal_note(topic_id: int, body: TopicInternalNoteRequest) -> dict[str, Any]:
    try:
        return topic_ai_service.add_internal_note(topic_id, body.note)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        _raise_safe(error)


@app.get("/api/topics/{topic_id}")
def get_topic(topic_id: int) -> dict[str, Any]:
    with db.connect() as connection:
        row = connection.execute(
            """SELECT t.*,c.name channel_name FROM conversation_topics t
               JOIN channels c ON c.id=t.channel_id WHERE t.id=?""",
            (topic_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Topic not found.")
        sources = connection.execute(
            """SELECT m.zoom_message_id,m.sender_name,m.sent_at,m.body,m.thread_id
               FROM topic_sources s JOIN messages m ON m.id=s.message_id
               WHERE s.topic_id=? ORDER BY m.sent_at""",
            (topic_id,),
        ).fetchall()
        internal_notes = connection.execute(
            """SELECT id,note_text,ai_summary,ai_model,prompt_version,created_at
               FROM topic_internal_notes WHERE topic_id=? ORDER BY created_at,id""",
            (topic_id,),
        ).fetchall()
    result = _topic_row(row)
    result["sources"] = [dict(item) for item in sources]
    result["internal_notes"] = [dict(item) for item in internal_notes]
    result["internal_note_count"] = len(internal_notes)
    return result


@app.patch("/api/topics/{topic_id}")
def review_topic(topic_id: int, body: TopicReviewUpdate) -> dict[str, Any]:
    raise HTTPException(410, "Topic review status was removed in v0.2. Use Do not track or automatic maturity archiving.")
    if body.review_status not in {"unreviewed", "approved", "rejected"}:
        raise HTTPException(400, "Invalid topic review status.")
    with db.connect() as connection:
        cursor = connection.execute(
            "UPDATE conversation_topics SET review_status=?,updated_at=datetime('now') WHERE id=?",
            (body.review_status, topic_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Topic not found.")
    return {"id": topic_id, "review_status": body.review_status}


@app.post("/api/topics/{topic_id}/ignore")
def ignore_topic(topic_id: int) -> dict[str, Any]:
    with db.connect() as connection:
        topic = connection.execute(
            "SELECT keep_tracking FROM conversation_topics WHERE id=?", (topic_id,)
        ).fetchone()
        if not topic:
            raise HTTPException(404, "Topic not found.")
        if topic["keep_tracking"]:
            raise HTTPException(409, "Turn off Keep Tracking before choosing Do not track.")
        connection.execute(
            "UPDATE conversation_topics SET ignored_at=?,updated_at=? WHERE id=?",
            (now_iso(), now_iso(), topic_id),
        )
    return {"id": topic_id, "ignored": True}


@app.patch("/api/topics/{topic_id}/tracking")
def set_topic_tracking(topic_id: int, body: TopicTrackingUpdate) -> dict[str, Any]:
    with db.connect() as connection:
        topic = connection.execute(
            "SELECT ignored_at FROM conversation_topics WHERE id=?", (topic_id,)
        ).fetchone()
        if not topic:
            raise HTTPException(404, "Topic not found.")
        if topic["ignored_at"] and body.enabled:
            raise HTTPException(409, "An ignored topic cannot be kept for tracking.")
        connection.execute(
            "UPDATE conversation_topics SET keep_tracking=? WHERE id=?",
            (int(body.enabled), topic_id),
        )
    return {"id": topic_id, "keep_tracking": body.enabled}


@app.post("/api/topics/{topic_id}/promote")
def promote_topic(topic_id: int) -> dict[str, Any]:
    raise HTTPException(410, "Manual Add to KB was removed in v0.2. Knowledge is archived automatically.")
    with db.connect() as connection:
        topic = connection.execute(
            "SELECT * FROM conversation_topics WHERE id=?", (topic_id,)
        ).fetchone()
        if not topic:
            raise HTTPException(404, "Topic not found.")
        linked_knowledge = connection.execute(
            "SELECT knowledge_id FROM knowledge_topic_sources WHERE topic_id=? LIMIT 1", (topic_id,)
        ).fetchone()
        connection.execute(
            "UPDATE conversation_topics SET review_status='approved',updated_at=? WHERE id=?",
            (now_iso(), topic_id),
        )
        if linked_knowledge:
            return {"knowledge_id": linked_knowledge["knowledge_id"], "topic_id": topic_id, "already_added": True}
        normalized = normalize_question(topic["title"])
        existing = connection.execute(
            "SELECT id FROM knowledge_items WHERE normalized_key=?", (normalized,)
        ).fetchone()
        stamp = now_iso()
        conclusions = json.loads(topic["conclusions_json"] or "[]")
        conclusion = "\n".join(conclusions) or topic["discussion_summary"]
        if existing:
            knowledge_id = existing["id"]
            linked = connection.execute(
                "SELECT 1 FROM knowledge_topic_sources WHERE knowledge_id=? AND topic_id=?",
                (knowledge_id, topic_id),
            ).fetchone()
            if not linked:
                connection.execute(
                    """UPDATE knowledge_items SET occurrences=occurrences+1,last_seen_at=?,updated_at=?,
                       version=version+1,problem_summary=CASE WHEN problem_summary='' THEN ? ELSE problem_summary END,
                       conclusion_summary=CASE WHEN conclusion_summary='' THEN ? ELSE conclusion_summary END,
                       context_summary=CASE WHEN context_summary='' THEN ? ELSE context_summary END
                       WHERE id=?""",
                    (topic["last_message_at"], stamp, topic["problem_summary"], conclusion, topic["context_summary"], knowledge_id),
                )
        else:
            knowledge_id = connection.execute(
                """INSERT INTO knowledge_items(
                     canonical_question,normalized_key,answer,category,status,occurrences,
                     first_seen_at,last_seen_at,updated_at,problem_summary,conclusion_summary,
                     context_summary,open_questions_json,action_items_json,last_verified_at
                   ) VALUES(?,?,?,'uncategorized','candidate',1,?,?,?,?,?,?,?,?,?)""",
                (
                    topic["title"], normalized, conclusion, topic["first_message_at"], topic["last_message_at"],
                    stamp, topic["problem_summary"], conclusion, topic["context_summary"],
                    topic["open_questions_json"], topic["action_items_json"], stamp,
                ),
            ).lastrowid
        connection.execute(
            "INSERT OR IGNORE INTO knowledge_topic_sources(knowledge_id,topic_id) VALUES(?,?)",
            (knowledge_id, topic_id),
        )
        source_rows = connection.execute(
            "SELECT message_id FROM topic_sources WHERE topic_id=?", (topic_id,)
        ).fetchall()
        connection.executemany(
            "INSERT OR IGNORE INTO knowledge_sources(knowledge_id,message_id,relation) VALUES(?,?,'topic')",
            [(knowledge_id, row["message_id"]) for row in source_rows],
        )
    return {"knowledge_id": knowledge_id, "topic_id": topic_id, "already_added": False}


@app.get("/api/mentions")
def list_mentions(status: str = "") -> list[dict[str, Any]]:
    where = "WHERE t.status=?" if status else ""
    parameters = (status,) if status else ()
    with db.connect() as connection:
        rows = connection.execute(
            f"""SELECT m.id message_id,m.body,m.sent_at,m.sender_name,c.name channel_name,
                t.status,t.note,t.updated_at FROM mention_tasks t
                JOIN messages m ON m.id=t.message_id JOIN channels c ON c.id=m.channel_id
                {where} ORDER BY m.sent_at DESC""", parameters
        ).fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/mentions/{message_id}")
def update_mention(message_id: int, body: MentionUpdate) -> dict[str, Any]:
    if body.status not in {"new", "in_progress", "done", "ignored"}:
        raise HTTPException(400, "Invalid mention status.")
    with db.connect() as connection:
        cursor = connection.execute(
            "UPDATE mention_tasks SET status=?,note=?,updated_at=datetime('now') WHERE message_id=?",
            (body.status, body.note, message_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Mention not found.")
    return {"message_id": message_id, "status": body.status}


@app.get("/api/settings/identity")
def get_identity() -> dict[str, str]:
    return {
        "member_id": db.get_setting("current_zoom_member_id") or "",
        "display_name": db.get_setting("current_zoom_display_name") or "",
    }


@app.put("/api/settings/identity")
def set_identity(body: IdentityUpdate) -> dict[str, str]:
    db.set_setting("current_zoom_member_id", body.member_id.strip())
    db.set_setting("current_zoom_display_name", body.display_name.strip())
    matched = sync_service.reindex_identity()
    return {"member_id": body.member_id.strip(), "display_name": body.display_name.strip(), "matched_mentions": str(matched)}


@app.get("/api/settings/sync")
def get_sync_settings() -> dict[str, Any]:
    return {
        "initial_sync_days": int(db.get_setting("initial_sync_days") or "90"),
        "allowed_days": list(ALLOWED_INITIAL_SYNC_DAYS),
    }


@app.put("/api/settings/sync")
def set_sync_settings(body: SyncDaysUpdate) -> dict[str, int]:
    if body.days not in ALLOWED_INITIAL_SYNC_DAYS:
        raise HTTPException(400, "Initial sync days must be 30, 90, or 180.")
    db.set_setting("initial_sync_days", str(body.days))
    return {"initial_sync_days": body.days}


@app.get("/api/settings/ai")
def get_ai_settings() -> dict[str, Any]:
    status = topic_ai_service.status()
    return {
        "configured": status["configured"],
        "config_source": status["config_source"],
        "model_tier": status["model_tier"],
        "model": status["model"],
        "model_tiers": status["model_tiers"],
        "output_language": status["output_language"],
        "output_languages": status["output_languages"],
    }


@app.put("/api/settings/ai")
def set_ai_settings(body: AISettingsUpdate) -> dict[str, Any]:
    if body.model_tier not in AI_MODEL_TIERS:
        raise HTTPException(400, "Invalid AI model tier.")
    if body.output_language not in AI_OUTPUT_LANGUAGES:
        raise HTTPException(400, "Invalid AI output language.")
    db.set_setting("ai_model_tier", body.model_tier)
    db.set_setting("ai_output_language", body.output_language)
    return {
        "model_tier": body.model_tier,
        "model": AI_MODEL_TIERS[body.model_tier],
        "output_language": body.output_language,
    }


@app.get("/api/settings/knowledge")
def get_knowledge_settings() -> dict[str, Any]:
    return {
        "maturity_days": knowledge_pipeline.maturity_days(),
        "allowed_days": list(ALLOWED_KNOWLEDGE_MATURITY_DAYS),
        "default_days": DEFAULT_KNOWLEDGE_MATURITY_DAYS,
    }


@app.put("/api/settings/knowledge")
def set_knowledge_settings(body: KnowledgeSettingsUpdate) -> dict[str, Any]:
    if body.maturity_days not in ALLOWED_KNOWLEDGE_MATURITY_DAYS:
        raise HTTPException(400, "Knowledge maturity days must be 7, 14, or 30.")
    db.set_setting("knowledge_maturity_days", str(body.maturity_days))
    return {"maturity_days": body.maturity_days}


@app.get("/api/knowledge/import-status")
def knowledge_import_status() -> dict[str, Any]:
    return knowledge_pipeline.bootstrap_status()


@app.get("/api/knowledge/bootstrap/status")
def knowledge_bootstrap_status() -> dict[str, Any]:
    return knowledge_pipeline.bootstrap_status()


@app.post("/api/knowledge/import-historical")
def import_historical_knowledge(body: HistoricalImportRequest) -> dict[str, Any]:
    if not 1 <= body.max_windows <= 20:
        raise HTTPException(400, "max_windows must be between 1 and 20.")
    try:
        return knowledge_pipeline.bootstrap_historical(body.max_windows)
    except RuntimeError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        _raise_safe(error)


@app.post("/api/knowledge/bootstrap")
def bootstrap_knowledge(body: HistoricalImportRequest) -> dict[str, Any]:
    return import_historical_knowledge(body)


@app.post("/api/knowledge/extract")
def extract_knowledge() -> dict[str, int]:
    raise HTTPException(410, "Legacy rule-based extraction was removed in v0.2. Use historical import.")


@app.get("/api/knowledge")
def list_knowledge(status: str = "", search: str = "") -> list[dict[str, Any]]:
    if search:
        return knowledge_pipeline.search(search, limit=100, translate=True)
    return knowledge_pipeline.recent("updated", 100)


@app.get("/api/knowledge/recent")
def recent_knowledge(view: str = "added", limit: int = Query(30, ge=1, le=100)) -> list[dict[str, Any]]:
    return knowledge_pipeline.recent(view, limit)


@app.post("/api/knowledge/search")
def search_knowledge(body: KnowledgeSearchRequest) -> list[dict[str, Any]]:
    if not body.query.strip():
        raise HTTPException(400, "Search query is required.")
    if not 1 <= body.limit <= 100:
        raise HTTPException(400, "Search limit must be between 1 and 100.")
    return knowledge_pipeline.search(body.query, body.limit, translate=True)


@app.get("/api/knowledge/{knowledge_id}")
def get_knowledge(knowledge_id: int) -> dict[str, Any]:
    item = knowledge_pipeline.detail(knowledge_id)
    if not item:
        raise HTTPException(404, "Knowledge item not found.")
    return item


@app.get("/api/knowledge/{knowledge_id}/sources")
def get_knowledge_sources(knowledge_id: int) -> list[dict[str, Any]]:
    with db.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM knowledge_items WHERE id=?", (knowledge_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Knowledge item not found.")
        rows = connection.execute(
            """SELECT m.zoom_message_id,m.sender_name,m.sent_at,m.body,m.thread_id,
                      c.name channel_name,s.relation
               FROM knowledge_sources s
               JOIN messages m ON m.id=s.message_id
               JOIN channels c ON c.id=m.channel_id
               WHERE s.knowledge_id=? ORDER BY m.sent_at""",
            (knowledge_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/knowledge/{knowledge_id}")
def update_knowledge(knowledge_id: int, body: KnowledgeUpdate) -> dict[str, Any]:
    raise HTTPException(410, "Manual knowledge review and editing were removed in v0.2.")
    if body.status not in {"candidate", "approved", "rejected", "stale"}:
        raise HTTPException(400, "Invalid knowledge status.")
    with db.connect() as connection:
        cursor = connection.execute(
            """UPDATE knowledge_items SET status=?,answer=?,category=?,problem_summary=?,
               conclusion_summary=?,context_summary=?,version=version+1,updated_at=datetime('now') WHERE id=?""",
            (
                body.status, body.answer.strip(), body.category.strip() or "uncategorized",
                body.problem_summary.strip(), body.conclusion_summary.strip(), body.context_summary.strip(), knowledge_id,
            ),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Knowledge item not found.")
    return {"id": knowledge_id, "status": body.status}


def _require_auth() -> None:
    if not tokens.exists():
        raise HTTPException(401, "Zoom authorization token is not available.")


def _raise_safe(error: Exception) -> None:
    message = str(error) or type(error).__name__
    raise HTTPException(502, message[:500]) from error


def _topic_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in (
        "confirmed_facts_json", "conclusions_json", "open_questions_json",
        "action_items_json", "tags_json",
    ):
        item[key.removesuffix("_json")] = json.loads(item.pop(key) or "[]")
    return item

