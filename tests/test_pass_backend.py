"""Tests for the pass credential backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from network_terminal_mcp.config.models import CredentialSpec
from network_terminal_mcp.credentials.pass_backend import (
    Credentials,
    PassBackend,
    parse_entry,
)
from network_terminal_mcp.errors import CredentialError


def test_parse_entry_returns_first_line_password() -> None:
    assert parse_entry("hunter2\nusername: ignored\n") == "hunter2"


def test_parse_entry_empty_raises() -> None:
    with pytest.raises(CredentialError, match="empty"):
        parse_entry("")


def test_pass_backend_resolves_via_binary(tmp_path: Path) -> None:
    fake_pass = tmp_path / "pass"
    fake_pass.write_text(
        "#!/bin/sh\nprintf 'hunter2\\n'\n",
        encoding="utf-8",
    )
    fake_pass.chmod(0o755)

    backend = PassBackend(binary=str(fake_pass))
    profile = CredentialSpec.model_validate(
        {"entry": "network/credentials/net", "username": "operator"}
    )
    assert backend.resolve(profile) == Credentials(
        username="operator", password="hunter2"
    )


def test_pass_backend_resolves_an_explicit_ssh_key(tmp_path: Path) -> None:
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("not read by the backend", encoding="utf-8")
    backend = PassBackend(binary="unused")
    profile = CredentialSpec.model_validate(
        {"backend": "ssh_key", "key_file": str(key_file), "username": "operator"}
    )

    assert backend.resolve(profile) == Credentials(
        username="operator", key_file=str(key_file)
    )


def test_pass_backend_resolves_an_ssh_key_passphrase_from_pass(tmp_path: Path) -> None:
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("not read by the backend", encoding="utf-8")
    fake_pass = tmp_path / "pass"
    fake_pass.write_text(
        "#!/bin/sh\nprintf 'unlock-key\\n'\n",
        encoding="utf-8",
    )
    fake_pass.chmod(0o755)
    backend = PassBackend(binary=str(fake_pass))
    profile = CredentialSpec.model_validate(
        {
            "backend": "ssh_key",
            "key_file": str(key_file),
            "key_passphrase_entry": "network/jump-key",
            "username": "operator",
        }
    )

    assert backend.resolve(profile) == Credentials(
        username="operator",
        key_file=str(key_file),
        key_passphrase="unlock-key",
    )


def test_pass_backend_rejects_missing_ssh_key(tmp_path: Path) -> None:
    backend = PassBackend(binary="unused")
    profile = CredentialSpec.model_validate(
        {
            "backend": "ssh_key",
            "key_file": str(tmp_path / "missing-key"),
            "username": "operator",
        }
    )

    with pytest.raises(CredentialError, match="does not exist"):
        backend.resolve(profile)


def test_pass_backend_failure_raises(tmp_path: Path) -> None:
    fake_pass = tmp_path / "pass"
    fake_pass.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_pass.chmod(0o755)

    backend = PassBackend(binary=str(fake_pass))
    with pytest.raises(CredentialError, match="could not show"):
        backend.resolve(
            CredentialSpec.model_validate({"entry": "missing", "username": "operator"})
        )


def test_pass_backend_rejects_empty_entry(tmp_path: Path) -> None:
    fake_pass = tmp_path / "pass"
    fake_pass.write_text("#!/bin/sh\nprintf '\\n'\n")
    fake_pass.chmod(0o755)

    backend = PassBackend(binary=str(fake_pass))
    with pytest.raises(CredentialError, match="invalid pass entry"):
        backend.resolve(
            CredentialSpec.model_validate({"entry": "x", "username": "operator"})
        )


def test_pass_backend_resolves_a_plaintext_credential() -> None:
    backend = PassBackend(binary="unused")
    spec = CredentialSpec.model_validate(
        {"backend": "plaintext", "username": "operator", "password": "hunter2"}
    )

    assert backend.resolve(spec) == Credentials(
        username="operator", password="hunter2"
    )


def test_pass_backend_read_entry_returns_the_secret(tmp_path: Path) -> None:
    fake_pass = tmp_path / "pass"
    fake_pass.write_text(
        "#!/bin/sh\nprintf 'interactive-secret\\n'\n",
        encoding="utf-8",
    )
    fake_pass.chmod(0o755)

    backend = PassBackend(binary=str(fake_pass))
    assert backend.read_entry("net/device-pass") == "interactive-secret"


def test_credential_spec_rejects_traversal_entries() -> None:
    from pydantic import ValidationError

    for entry in ("../secret", "/etc/shadow", "a/../../b", "with space"):
        with pytest.raises(ValidationError, match="pass entry"):
            CredentialSpec.model_validate({"entry": entry, "username": "operator"})


def test_pass_backend_is_lazy_about_the_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import network_terminal_mcp.credentials.pass_backend as backend_mod

    monkeypatch.setattr(backend_mod.shutil, "which", lambda name: None)
    backend = PassBackend()

    plaintext = CredentialSpec.model_validate(
        {"backend": "plaintext", "username": "operator", "password": "hunter2"}
    )
    assert backend.resolve(plaintext).password == "hunter2"

    with pytest.raises(CredentialError, match="not found"):
        backend.resolve(
            CredentialSpec.model_validate({"entry": "a/b", "username": "operator"})
        )


def test_require_pass_binary_missing_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import network_terminal_mcp.credentials.pass_backend as backend_mod

    monkeypatch.setattr(backend_mod, "shutil", _NoWhich())
    with pytest.raises(CredentialError, match="not found"):
        backend_mod.require_pass_binary()


class _NoWhich:
    def which(self, name: str) -> None:
        return None
