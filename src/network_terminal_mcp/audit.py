"""Fail-closed JSONL audit logging.

Every privileged action must be recorded. If the audit sink cannot be written,
the operation is aborted (fail-closed): callers must treat a raised
:class:`~network_terminal_mcp.errors.AuditError` as a reason to stop.

Audit files and directories are created with 0600/0700 permissions and never
contain secrets — redaction is the caller's responsibility, but the logger
re-checks the record before writing.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from network_terminal_mcp.errors import AuditError
from network_terminal_mcp.redaction import REDACTED

SECRET_FIELD_NAMES = ("password", "secret", "token", "api_key")


class AuditLogger:
    """Append structured records to a JSONL file under a lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._ensure_parent()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_parent(self) -> None:
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        except OSError as exc:
            raise AuditError(f"cannot create audit directory {parent}: {exc}") from exc

    @staticmethod
    def new_correlation_id() -> str:
        """Return a fresh correlation id for a single logical operation."""
        return uuid.uuid4().hex

    def _scrub(self, record: dict[str, Any]) -> dict[str, Any]:
        """Remove any accidentally embedded secret-shaped fields."""
        return {
            key: REDACTED
            if any(name in key.lower() for name in SECRET_FIELD_NAMES)
            else value
            for key, value in record.items()
        }

    def write(self, record: dict[str, Any]) -> None:
        """Append one audit record or raise :class:`AuditError`."""
        record = self._scrub(dict(record))
        record.setdefault("ts", datetime.now(UTC).isoformat())
        record.setdefault("correlation_id", self.new_correlation_id())
        try:
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise AuditError(f"audit record is not JSON-serializable: {exc}") from exc

        with self._lock:
            try:
                self._ensure_parent()
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(self._path, 0o600)
            except OSError as exc:
                raise AuditError(f"failed to append audit record: {exc}") from exc
