"""Tests for the runtime health-check CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

import network_terminal_mcp.__main__ as cli
from network_terminal_mcp.__main__ import main


def _which(*available: str):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def test_check_reports_runtime_prerequisites(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", _which("ssh", "telnet"))
    monkeypatch.setattr(
        cli, "default_audit_path", lambda: tmp_path / "state" / "audit.jsonl"
    )

    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "network-terminal-mcp" in out
    assert "ssh: /usr/bin/ssh" in out
    assert "telnet: /usr/bin/telnet" in out
    assert "serial: pyserial" in out
    assert "audit:" in out
    assert "config: none required" in out


def test_check_does_not_fail_when_only_telnet_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", _which("ssh"))
    monkeypatch.setattr(
        cli, "default_audit_path", lambda: tmp_path / "state" / "audit.jsonl"
    )

    assert main(["check"]) == 0
    assert "telnet: not found" in capsys.readouterr().out


def test_check_fails_when_no_client_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", _which())
    monkeypatch.setattr(
        cli, "default_audit_path", lambda: tmp_path / "state" / "audit.jsonl"
    )

    assert main(["check"]) == 1


def test_check_fails_when_audit_directory_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", _which("ssh", "telnet"))
    monkeypatch.setattr(
        cli, "default_audit_path", lambda: blocker / "audit.jsonl"
    )

    assert main(["check"]) == 1


def test_no_command_starts_stdio_server(monkeypatch: pytest.MonkeyPatch) -> None:
    transports: list[str] = []

    class FakeServer:
        def run(self, *, transport: str) -> None:
            transports.append(transport)

    monkeypatch.setattr(
        "network_terminal_mcp.server.create_server", lambda: FakeServer()
    )
    assert main([]) == 0
    assert transports == ["stdio"]
