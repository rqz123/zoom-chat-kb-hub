from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .db import Database
from .zoom_client import ZoomAPIError, ZoomClient


ENCRYPTED_PLACEHOLDER = "[This is an encrypted message]"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_zoom_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def message_body(message: dict[str, Any]) -> str:
    return str(message.get("message") or "").strip()


def classify_messages(messages: Iterable[dict[str, Any]]) -> tuple[str, str]:
    bodies = [message_body(message) for message in messages]
    bodies = [body for body in bodies if body]
    if not bodies:
        return "unknown_no_messages", "No messages were returned in the probe window."
    encrypted = sum(body == ENCRYPTED_PLACEHOLDER for body in bodies)
    readable = len(bodies) - encrypted
    if encrypted and readable:
        return "mixed", f"{readable} readable and {encrypted} encrypted message(s) sampled."
    if encrypted:
        return "encrypted", "Zoom returned encrypted-message placeholders instead of body text."
    return "readable", f"{readable} readable message(s) sampled."


def classify_error(error: ZoomAPIError) -> tuple[str, str]:
    if error.status == 403:
        return "forbidden", str(error)
    if error.status == 404:
        return "missing", str(error)
    return "error", str(error)


class SyncService:
    def __init__(self, db: Database, client: ZoomClient):
        self.db = db
        self.client = client

    def refresh_channels(self) -> int:
        channels = list(self.client.iter_channels())
        seen_at = now_iso()
        with self.db.connect() as connection:
            connection.execute("UPDATE channels SET is_active = 0")
            for channel in channels:
                channel_id = channel.get("id") or channel.get("channel_id")
                if not channel_id:
                    continue
                connection.execute(
                    """INSERT INTO channels(id, name, type, last_seen_at, is_active)
                       VALUES(?, ?, ?, ?, 1)
                       ON CONFLICT(id) DO UPDATE SET
                         name=excluded.name, type=excluded.type,
                         last_seen_at=excluded.last_seen_at, is_active=1""",
                    (channel_id, channel.get("name") or "(Unnamed channel)", channel.get("type"), seen_at),
                )
        return len(channels)

    def probe(self, channel_ids: list[str] | None = None, days: int = 30) -> list[dict[str, str]]:
        with self.db.connect() as connection:
            if channel_ids:
                placeholders = ",".join("?" for _ in channel_ids)
                rows = connection.execute(f"SELECT id, name FROM channels WHERE id IN ({placeholders})", channel_ids).fetchall()
            else:
                rows = connection.execute("SELECT id, name FROM channels WHERE is_active=1 ORDER BY name").fetchall()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        results = []
        for row in rows:
            try:
                sample = list(self.client.iter_messages(row["id"], start, end, max_pages=1))[:20]
                status, reason = classify_messages(sample)
            except ZoomAPIError as error:
                status, reason = classify_error(error)
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE channels SET readability_status=?, readability_reason=?, last_probed_at=? WHERE id=?",
                    (status, reason, now_iso(), row["id"]),
                )
            results.append({"id": row["id"], "name": row["name"], "status": status, "reason": reason})
        return results

    def sync_selected(self, initial_days: int | None = None) -> dict[str, Any]:
        if initial_days is None:
            initial_days = int(self.db.get_setting("initial_sync_days") or "90")
        started_at = now_iso()
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE sync_runs SET status='interrupted',finished_at=? WHERE status='running'",
                (started_at,),
            )
            run_id = connection.execute(
                "INSERT INTO sync_runs(started_at, status) VALUES(?, 'running')", (started_at,)
            ).lastrowid
            channels = connection.execute(
                """SELECT c.id, c.name, s.last_message_at, s.last_sync_at,
                          s.initial_sync_completed_at, s.covered_from_at,
                          s.sync_watermark_at, s.last_success_at
                   FROM channels c LEFT JOIN sync_cursors s ON s.channel_id=c.id
                   WHERE c.selected=1 AND c.is_active=1 ORDER BY c.name"""
            ).fetchall()

        total_messages = 0
        errors: list[dict[str, str]] = []
        for channel in channels:
            end = datetime.now(timezone.utc)
            is_initial = not channel["initial_sync_completed_at"]
            watermark = parse_zoom_time(
                channel["sync_watermark_at"] or channel["last_success_at"] or channel["last_sync_at"]
            )
            start = (end - timedelta(days=initial_days)) if is_initial else ((watermark or end) - timedelta(minutes=5))
            try:
                messages = list(self.client.iter_messages(channel["id"], start, end))
                stored, latest = self._store_channel_messages(channel["id"], messages)
                total_messages += stored
                completed_at = now_iso()
                start_iso = self._to_iso(start)
                end_iso = self._to_iso(end)
                with self.db.connect() as connection:
                    connection.execute(
                        """INSERT INTO sync_cursors(
                             channel_id,last_message_at,last_sync_at,last_error,
                             initial_sync_completed_at,covered_from_at,sync_watermark_at,last_success_at
                           ) VALUES(?,?,?,NULL,?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET
                           last_message_at=COALESCE(excluded.last_message_at,sync_cursors.last_message_at),
                           last_sync_at=excluded.last_sync_at,last_error=NULL,
                           initial_sync_completed_at=COALESCE(sync_cursors.initial_sync_completed_at,excluded.initial_sync_completed_at),
                           covered_from_at=COALESCE(sync_cursors.covered_from_at,excluded.covered_from_at),
                           sync_watermark_at=excluded.sync_watermark_at,last_success_at=excluded.last_success_at""",
                        (channel["id"], latest, completed_at, completed_at, start_iso, end_iso, completed_at),
                    )
                    if messages:
                        readability, reason = classify_messages(messages)
                        connection.execute(
                            "UPDATE channels SET readability_status=?,readability_reason=?,last_probed_at=? WHERE id=?",
                            (readability, reason, completed_at, channel["id"]),
                        )
            except Exception as error:
                errors.append({"channel": channel["name"], "error": str(error)})
                with self.db.connect() as connection:
                    connection.execute(
                        """INSERT INTO sync_cursors(channel_id,last_sync_at,last_error) VALUES(?,?,?)
                           ON CONFLICT(channel_id) DO UPDATE SET last_sync_at=excluded.last_sync_at,last_error=excluded.last_error""",
                        (channel["id"], now_iso(), str(error)),
                    )

        status = "completed" if not errors else "partial"
        finished_at = now_iso()
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE sync_runs SET finished_at=?,status=?,channel_count=?,message_count=?,error_count=?,detail=?
                   WHERE id=?""",
                (finished_at, status, len(channels), total_messages, len(errors), json.dumps(errors, ensure_ascii=False), run_id),
            )
        return {"run_id": run_id, "status": status, "channels": len(channels), "messages": total_messages, "errors": errors}

    def backfill_channel(self, channel_id: str, days: int) -> dict[str, Any]:
        with self.db.connect() as connection:
            channel = connection.execute(
                """SELECT c.id,c.name,s.last_message_at,s.initial_sync_completed_at,s.covered_from_at
                   FROM channels c LEFT JOIN sync_cursors s ON s.channel_id=c.id WHERE c.id=?""",
                (channel_id,),
            ).fetchone()
        if not channel:
            raise ValueError("Channel not found.")
        if not channel["initial_sync_completed_at"]:
            raise ValueError("Complete the channel's first sync before backfilling history.")

        end = datetime.now(timezone.utc)
        target_start = end - timedelta(days=days)
        covered_from = parse_zoom_time(channel["covered_from_at"])
        if covered_from and covered_from <= target_start:
            return {"channel_id": channel_id, "messages": 0, "covered_from_at": self._to_iso(covered_from), "changed": False}

        query_end = min(end, (covered_from + timedelta(minutes=5)) if covered_from else end)
        messages = list(self.client.iter_messages(channel_id, target_start, query_end))
        stored, latest = self._store_channel_messages(channel_id, messages)
        latest_values = [value for value in (channel["last_message_at"], latest) if value]
        last_message_at = max(latest_values) if latest_values else None
        completed_at = now_iso()
        with self.db.connect() as connection:
            connection.execute(
                """UPDATE sync_cursors SET covered_from_at=?,last_message_at=?,last_sync_at=?,
                   last_success_at=?,last_error=NULL WHERE channel_id=?""",
                (self._to_iso(target_start), last_message_at, completed_at, completed_at, channel_id),
            )
            if messages:
                readability, reason = classify_messages(messages)
                connection.execute(
                    "UPDATE channels SET readability_status=?,readability_reason=?,last_probed_at=? WHERE id=?",
                    (readability, reason, completed_at, channel_id),
                )
        return {
            "channel_id": channel_id,
            "messages": stored,
            "covered_from_at": self._to_iso(target_start),
            "changed": True,
        }

    @staticmethod
    def _to_iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _store_channel_messages(self, channel_id: str, messages: list[dict[str, Any]]) -> tuple[int, str | None]:
        latest: datetime | None = None
        stored = 0
        current_member_id = self.db.get_setting("current_zoom_member_id")
        current_name = (self.db.get_setting("current_zoom_display_name") or "").casefold()
        with self.db.connect() as connection:
            for message in messages:
                message_id = str(message.get("id") or message.get("message_id") or "")
                sent_at = message.get("date_time") or message.get("timestamp")
                if not message_id or not sent_at:
                    continue
                body = message_body(message)
                state = "encrypted" if body == ENCRYPTED_PLACEHOLDER else ("readable" if body else "empty")
                raw_sender = message.get("sender")
                sender = raw_sender if isinstance(raw_sender, dict) else {}
                sender_name = sender.get("name") or message.get("sender_display_name") or message.get("sender_name")
                if not sender_name and isinstance(raw_sender, str):
                    sender_name = raw_sender
                before = connection.total_changes
                connection.execute(
                    """INSERT INTO messages(channel_id,zoom_message_id,sender_name,sender_member_id,sent_at,body,body_state,thread_id,reply_to_message_id,raw_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(channel_id,zoom_message_id) DO UPDATE SET
                       sender_name=excluded.sender_name,sender_member_id=excluded.sender_member_id,
                       sent_at=excluded.sent_at,body=excluded.body,body_state=excluded.body_state,
                       thread_id=excluded.thread_id,reply_to_message_id=excluded.reply_to_message_id,raw_json=excluded.raw_json""",
                    (channel_id, message_id, sender_name,
                     sender.get("member_id") or message.get("send_member_id") or message.get("sender_member_id"), sent_at, body, state,
                     message.get("thread_id") or message.get("reply_main_message_id"),
                     message.get("reply_to_message_id") or message.get("reply_main_message_id"), json.dumps(message, ensure_ascii=False)),
                )
                stored += int(connection.total_changes > before)
                local_id = connection.execute(
                    "SELECT id FROM messages WHERE channel_id=? AND zoom_message_id=?", (channel_id, message_id)
                ).fetchone()["id"]
                mentions = message.get("mentions") or message.get("at_items") or []
                for mention in mentions:
                    member_id = mention.get("member_id") or mention.get("at_contact_member_id") or mention.get("id")
                    display_name = mention.get("name") or mention.get("display_name")
                    is_current = bool(
                        (current_member_id and member_id == current_member_id)
                        or (current_name and (display_name or "").casefold() == current_name)
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO message_mentions(message_id,member_id,display_name,is_current_user)
                           VALUES(?,?,?,?)""", (local_id, member_id, display_name, int(is_current)),
                    )
                    if is_current:
                        connection.execute(
                            "INSERT OR IGNORE INTO mention_tasks(message_id,status,updated_at) VALUES(?,'new',?)",
                            (local_id, now_iso()),
                        )
                for attachment in message.get("files") or []:
                    connection.execute(
                        """INSERT OR IGNORE INTO attachments(message_id,file_id,file_name,file_type,file_size)
                           VALUES(?,?,?,?,?)""",
                        (local_id, attachment.get("id"), attachment.get("name"), attachment.get("type"), attachment.get("size")),
                    )
                parsed = parse_zoom_time(sent_at)
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
        latest_iso = latest.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if latest else None
        return stored, latest_iso

    def reindex_identity(self) -> int:
        """Rebuild mention matches and normalized sender fields from stored raw JSON."""
        current_member_id = self.db.get_setting("current_zoom_member_id")
        current_name = (self.db.get_setting("current_zoom_display_name") or "").casefold()
        matched = 0
        with self.db.connect() as connection:
            rows = connection.execute("SELECT id,raw_json FROM messages").fetchall()
            for row in rows:
                message = json.loads(row["raw_json"])
                raw_sender = message.get("sender")
                sender = raw_sender if isinstance(raw_sender, dict) else {}
                sender_name = sender.get("name") or message.get("sender_display_name") or message.get("sender_name")
                if not sender_name and isinstance(raw_sender, str):
                    sender_name = raw_sender
                sender_id = sender.get("member_id") or message.get("send_member_id") or message.get("sender_member_id")
                connection.execute(
                    "UPDATE messages SET sender_name=?,sender_member_id=? WHERE id=?",
                    (sender_name, sender_id, row["id"]),
                )
                for mention in message.get("mentions") or message.get("at_items") or []:
                    member_id = mention.get("member_id") or mention.get("at_contact_member_id") or mention.get("id")
                    display_name = mention.get("name") or mention.get("display_name")
                    is_current = bool(
                        (current_member_id and member_id == current_member_id)
                        or (current_name and (display_name or "").casefold() == current_name)
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO message_mentions(message_id,member_id,display_name,is_current_user)
                           VALUES(?,?,?,?)""", (row["id"], member_id, display_name, int(is_current)),
                    )
                    if is_current:
                        matched += 1
                        connection.execute(
                            "INSERT OR IGNORE INTO mention_tasks(message_id,status,updated_at) VALUES(?,'new',?)",
                            (row["id"], now_iso()),
                        )
        return matched

