"""Tests for the pass credential backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from network_terminal_mcp.config.models import CredentialProfile
from network_terminal_mcp.credentials.pass_backend import (
    Credentials,
    PassBackend,
    parse_entry,
)
from network_terminal_mcp.errors import CredentialError


def test_parse_entry_password_username_and_secret() -> None:
    entry = "hunter2\nusername: operator\nsecret: enable-secret\n"
    credentials = parse_entry(entry)
    assert credentials == Credentials(
        username="operator", password="hunter2", secret="enable-secret"
    )


def test_parse_entry_without_secret() -> None:
    credentials = parse_entry("hunter2\nusername: operator\n")
    assert credentials.secret is None


def test_parse_entry_missing_username_raises() -> None:
    with pytest.raises(CredentialError, match="username"):
        parse_entry("hunter2\n")


def test_parse_entry_empty_raises() -> None:
    with pytest.raises(CredentialError, match="empty"):
        parse_entry("")


def test_pass_backend_resolves_via_binary(tmp_path: Path) -> None:
    fake_pass = tmp_path / "pass"
    fake_pass.write_text(
        "#!/bin/sh\nprintf 'hunter2\\nusername: operator\\n'\n",
        encoding="utf-8",
    )
    fake_pass.chmod(0o755)

    backend = PassBackend(binary=str(fake_pass))
    profile = CredentialProfile.model_validate({"entry": "network/credentials/net"})
    assert backend.resolve(profile) == Credentials(
        username="operator", password="hunter2"
    )


def test_pass_backend_failure_raises(tmp_path: Path) -> None:
    fake_pass = tmp_path / "pass"
    fake_pass.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_pass.chmod(0o755)

    backend = PassBackend(binary=str(fake_pass))
    with pytest.raises(CredentialError, match="could not show"):
        backend.resolve(CredentialProfile.model_validate({"entry": "missing"}))


def test_pass_backend_rejects_bad_entry_format(tmp_path: Path) -> None:
    fake_pass = tmp_path / "pass"
    fake_pass.write_text("#!/bin/sh\nprintf 'hunter2\\nno-username-line\\n'\n")
    fake_pass.chmod(0o755)

    backend = PassBackend(binary=str(fake_pass))
    with pytest.raises(CredentialError, match="invalid pass entry"):
        backend.resolve(CredentialProfile.model_validate({"entry": "x"}))


def test_require_pass_binary_missing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import network_terminal_mcp.credentials.pass_backend as backend_mod

    monkeypatch.setattr(backend_mod, "shutil", _NoWhich())
    with pytest.raises(CredentialError, match="not found"):
        backend_mod.require_pass_binary()


class _NoWhich:
    def which(self, name: str) -> None:
        return None
