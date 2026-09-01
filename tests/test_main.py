"""Tests for the configuration health-check CLI."""

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


def _base_config(config_dir: Path) -> None:
    _write(
        config_dir,
        "inventory.yml",
        {
            "devices": {
                "sw1": {
                    "host": "192.0.2.1",
                    "credentials": "net",
                    "connection": "direct",
                }
            }
        },
    )
    _write(
        config_dir,
        "connections.yml",
        {"connections": {"direct": {"type": "direct", "protocol": "ssh"}}},
    )
    _write(
        config_dir,
        "credentials.yml",
        {
            "credentials": {
                "net": {"backend": "pass", "entry": "n/c", "username": "operator"}
            }
        },
    )


def test_check_summary(config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _base_config(config_dir)
    assert main(["--config-dir", str(config_dir), "check"]) == 0
    out = capsys.readouterr().out
    assert "devices=1" in out
    assert "config dir" in out


def test_check_device(config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _base_config(config_dir)
    assert main(["--config-dir", str(config_dir), "check", "--device", "sw1"]) == 0
    out = capsys.readouterr().out
    assert "host=192.0.2.1" in out
    assert "credentials=net" in out


def test_check_device_unknown_fails(config_dir: Path) -> None:
    _base_config(config_dir)
    assert main(["--config-dir", str(config_dir), "check", "--device", "nope"]) == 1


def test_check_invalid_config_fails(config_dir: Path) -> None:
    (config_dir / "policy.yml").write_text("::: nope :::", encoding="utf-8")
    assert main(["--config-dir", str(config_dir), "check"]) == 1
