"""Tests for the fail-closed audit logger."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from network_terminal_mcp.audit import AuditLogger
from network_terminal_mcp.errors import AuditError


def test_write_appends_jsonl(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")
    logger.write({"tool": "run_command", "command": "show version"})

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool"] == "run_command"
    assert record["command"] == "show version"
    assert "ts" in record
    assert "correlation_id" in record


def test_write_appends_multiple_records(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")
    logger.write({"n": 1})
    logger.write({"n": 2})
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_file_permissions_are_0600(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")
    logger.write({"n": 1})
    mode = stat.S_IMODE((tmp_path / "audit.jsonl").stat().st_mode)
    assert mode == 0o600


def test_parent_dir_created_with_0700(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "state" / "audit.jsonl"
    AuditLogger(target).write({"n": 1})
    assert target.exists()
    parent_mode = stat.S_IMODE(target.parent.stat().st_mode)
    assert parent_mode == 0o700


def test_secret_shaped_fields_are_scrubbed(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")
    logger.write({"username": "operator", "password": "hunter2"})
    record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert record["username"] == "operator"
    assert record["password"] == "<redacted>"
    assert "hunter2" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_correlation_id_is_unique(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")
    logger.write({})
    logger.write({})
    ids = {
        json.loads(line)["correlation_id"]
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    }
    assert len(ids) == 2


def test_non_serializable_record_raises(tmp_path: Path) -> None:
    logger = AuditLogger(tmp_path / "audit.jsonl")
    with pytest.raises(AuditError):
        logger.write({"bad": object()})


def test_unwritable_path_raises_fail_closed(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(AuditError):
        AuditLogger(blocker / "audit.jsonl")
