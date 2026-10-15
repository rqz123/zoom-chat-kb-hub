from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .security import protect, unprotect


class TokenStore:
    def __init__(self, protected_path: Path, legacy_path: Path | None = None):
        self.protected_path = protected_path
        self.legacy_path = legacy_path
        # Kept beside the token, not in the database: the token belongs to this
        # computer, while the database moves between computers during handoff.
        self.auth_error_path = protected_path.with_name("zoom-auth-error.json")

    def import_legacy_if_needed(self) -> bool:
        if self.protected_path.exists() or not self.legacy_path or not self.legacy_path.exists():
            return False
        payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise ValueError("Legacy Zoom token file is not valid.")
        self.save(payload)
        return True

    def exists(self) -> bool:
        return self.protected_path.exists()

    def load(self) -> dict[str, Any]:
        try:
            raw = unprotect(self.protected_path.read_bytes())
        except OSError:
            # Sandboxed/elevated Windows processes can occasionally have different
            # DPAPI profiles. Recover only from the explicitly configured local
            # OAuth file, and immediately protect it for the current profile.
            if not self.legacy_path or not self.legacy_path.exists():
                raise
            payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not payload.get("access_token"):
                raise ValueError("Legacy Zoom token file is not valid.")
            self.save(payload)
            return payload
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Stored token payload is invalid.")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        self.protected_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        temporary = self.protected_path.with_suffix(".tmp")
        temporary.write_bytes(protect(encoded))
        temporary.replace(self.protected_path)
        self.auth_error_path.unlink(missing_ok=True)

    def mark_invalid(self, reason: str) -> None:
        payload = {"reason": reason, "failed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
        self.auth_error_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_error_path.write_text(json.dumps(payload), encoding="utf-8")

    def auth_error(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.auth_error_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

