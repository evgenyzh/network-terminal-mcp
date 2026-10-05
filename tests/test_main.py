"""Tests for the policy health-check CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from network_terminal_mcp.__main__ import main


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    return directory


def _write(directory: Path, name: str, data: object) -> None:
    (directory / name).write_text(yaml.safe_dump(data), encoding="utf-8")


def test_check_summary_with_defaults(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config-dir", str(config_dir), "check"]) == 0
    out = capsys.readouterr().out
    assert "config dir" in out
    assert "known_hosts_file" in out
    assert "max_open_sessions=10" in out


def test_check_reads_the_optional_policy_file(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        config_dir,
        "policy.yml",
        {
            "defaults": {"allow_telnet": False},
            "runtime": {"session_idle_timeout": 120},
        },
    )
    assert main(["--config-dir", str(config_dir), "check"]) == 0
    out = capsys.readouterr().out
    assert "session_idle_timeout=120" in out


def test_check_invalid_policy_fails(config_dir: Path) -> None:
    (config_dir / "policy.yml").write_text("::: nope :::", encoding="utf-8")
    assert main(["--config-dir", str(config_dir), "check"]) == 1


def test_check_ignores_removed_inventory_file(
    config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Old inventory/connection files are ignored, not fatal.
    _write(config_dir, "inventory.yml", {"devices": {"sw1": {"host": "192.0.2.1"}}})
    assert main(["--config-dir", str(config_dir), "check"]) == 0
    assert "known_hosts_file" in capsys.readouterr().out


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
