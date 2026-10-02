from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from .db import Database
from .sync import now_iso, parse_zoom_time


QUESTION_PREFIXES = (
    "how ", "what ", "why ", "where ", "when ", "which ", "can ", "could ",
    "does ", "do ", "is ", "are ", "anyone ", "如何", "怎么", "怎样", "为什么",
    "是否", "能否", "可以", "请问", "哪里", "哪个", "有没有", "谁知道",
)


def is_question(text: str) -> bool:
    cleaned = text.strip()
    lowered = cleaned.casefold()
    return len(cleaned) >= 6 and (
        "?" in cleaned or "？" in cleaned or any(lowered.startswith(prefix) for prefix in QUESTION_PREFIXES)
    )


def normalize_question(text: str) -> str:
    value = text.casefold()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"@[\w.\-\u4e00-\u9fff]+", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value)
    return " ".join(value.split())[:500]


class KnowledgeService:
    def __init__(self, db: Database):
        self.db = db

    def extract(self) -> dict[str, int]:
        with self.db.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT id,channel_id,sender_member_id,sent_at,body FROM messages
                   WHERE body_state='readable' AND length(trim(COALESCE(body,'')))>0
                   ORDER BY channel_id,sent_at"""
            )]
            existing = [dict(row) for row in connection.execute(
                "SELECT id,normalized_key FROM knowledge_items"
            )]

            created = merged = 0
            for index, message in enumerate(rows):
                question = message["body"].strip()
                if not is_question(question):
                    continue
                normalized = normalize_question(question)
                if not normalized:
                    continue
                knowledge_id = self._find_match(normalized, existing)
                answer_ids, answer = self._find_answers(rows, index)
                stamp = message["sent_at"] or now_iso()
                if knowledge_id is not None and connection.execute(
                    "SELECT 1 FROM knowledge_sources WHERE knowledge_id=? AND message_id=? AND relation='question'",
                    (knowledge_id, message["id"]),
                ).fetchone():
                    continue
                if knowledge_id is None:
                    cursor = connection.execute(
                        """INSERT INTO knowledge_items(canonical_question,normalized_key,answer,first_seen_at,last_seen_at,updated_at)
                           VALUES(?,?,?,?,?,?)""",
                        (question, normalized, answer, stamp, stamp, now_iso()),
                    )
                    knowledge_id = cursor.lastrowid
                    existing.append({"id": knowledge_id, "normalized_key": normalized})
                    created += 1
                else:
                    connection.execute(
                        """UPDATE knowledge_items SET occurrences=occurrences+1,last_seen_at=?,updated_at=?,
                           answer=CASE WHEN COALESCE(answer,'')='' THEN ? ELSE answer END WHERE id=?""",
                        (stamp, now_iso(), answer, knowledge_id),
                    )
                    merged += 1
                connection.execute(
                    "INSERT OR IGNORE INTO knowledge_sources(knowledge_id,message_id,relation) VALUES(?,?,'question')",
                    (knowledge_id, message["id"]),
                )
                for answer_id in answer_ids:
                    connection.execute(
                        "INSERT OR IGNORE INTO knowledge_sources(knowledge_id,message_id,relation) VALUES(?,?,'answer')",
                        (knowledge_id, answer_id),
                    )
        return {"created": created, "merged": merged}

    @staticmethod
    def _find_match(normalized: str, existing: list[dict[str, Any]]) -> int | None:
        for item in existing:
            other = item["normalized_key"]
            if normalized == other or SequenceMatcher(None, normalized, other).ratio() >= 0.88:
                return int(item["id"])
        return None

    @staticmethod
    def _find_answers(rows: list[dict[str, Any]], question_index: int) -> tuple[list[int], str]:
        question = rows[question_index]
        question_time = parse_zoom_time(question["sent_at"])
        answers: list[dict[str, Any]] = []
        for candidate in rows[question_index + 1:question_index + 7]:
            if candidate["channel_id"] != question["channel_id"]:
                break
            candidate_time = parse_zoom_time(candidate["sent_at"])
            if question_time and candidate_time and candidate_time > question_time + timedelta(hours=24):
                break
            if candidate["sender_member_id"] == question["sender_member_id"] or is_question(candidate["body"]):
                continue
            answers.append(candidate)
            if len(answers) == 2:
                break
        return [item["id"] for item in answers], "\n\n".join(item["body"].strip() for item in answers)

