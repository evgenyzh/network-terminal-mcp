"""Credential retrieval from ``pass``, explicit key files, and plaintext.

Secrets live in GPG-encrypted ``pass`` entries or explicit local key files;
plaintext passwords are an explicit insecure opt-in. The server shells out to
``pass`` without a shell, so an entry name can never become a command. Entry
names are validated by :class:`~network_terminal_mcp.config.models.CredentialSpec`
before reaching this module.

The entry's first line is the password. The non-secret AAA username is part of
the credential spec, so it can be changed independently of the store entry.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from network_terminal_mcp.config.models import CredentialSpec
from network_terminal_mcp.errors import CredentialError

_PASS_BINARY = "pass"


@dataclass(frozen=True)
class Credentials:
    """Resolved credentials for one connection."""

    username: str
    password: str | None = None
    key_file: str | None = None
    key_passphrase: str | None = None


def require_pass_binary() -> str:
    """Return the path to ``pass`` or raise :class:`CredentialError`."""
    binary = shutil.which(_PASS_BINARY)
    if binary is None:
        raise CredentialError(f"{_PASS_BINARY} executable not found on PATH")
    return binary


def parse_entry(content: str) -> str:
    """Return the password from a ``pass`` entry's first line."""
    lines = content.splitlines()
    if not lines or not lines[0]:
        raise CredentialError("pass entry is empty: expected password on first line")
    return lines[0]


class PassBackend:
    """Resolve pass entries, explicit SSH keys, and plaintext credentials."""

    def __init__(self, binary: str | None = None) -> None:
        # ``pass`` is located lazily so key-only or plaintext-only setups do
        # not require the binary to be installed.
        self._binary = binary

    def resolve(self, spec: CredentialSpec) -> Credentials:
        """Return credentials for ``spec``, raising on any failure."""
        if spec.backend == "plaintext":
            assert spec.password is not None
            return Credentials(
                username=spec.username,
                password=spec.password.get_secret_value(),
            )
        if spec.backend == "ssh_key":
            assert spec.key_file is not None
            if not spec.key_file.is_file():
                raise CredentialError(f"SSH key file does not exist: {spec.key_file}")
            passphrase = (
                self._read_entry(spec.key_passphrase_entry)
                if spec.key_passphrase_entry is not None
                else None
            )
            return Credentials(
                username=spec.username,
                key_file=str(spec.key_file),
                key_passphrase=passphrase,
            )
        password = self._read_entry(spec.entry or "")
        return Credentials(username=spec.username, password=password)

    def read_entry(self, entry: str) -> str:
        """Return one ``pass`` entry's secret value for the secret input tool."""
        return self._read_entry(entry)

    def _read_entry(self, entry: str) -> str:
        binary = self._binary or require_pass_binary()
        try:
            completed = subprocess.run(
                [binary, "show", entry],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise CredentialError(
                f"failed to run pass for entry {entry!r}: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise CredentialError(
                f"pass could not show entry {entry!r}"
            )
        try:
            return parse_entry(completed.stdout)
        except CredentialError as exc:
            raise CredentialError(
                f"invalid pass entry {entry!r}: {exc}"
            ) from exc
