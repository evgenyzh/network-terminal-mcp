"""Credential retrieval from the ``pass`` password store.

Secrets live in GPG-encrypted ``pass`` entries, never in MCP arguments or
results. The server shells out to ``pass`` without interpolation, so the entry
name is always taken from local configuration rather than model input.

The entry format is line based:

- first line: the password;
- ``username: ...`` — the AAA username;
- ``secret: ...`` — an optional enable secret.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from network_terminal_mcp.config.models import CredentialProfile
from network_terminal_mcp.errors import CredentialError

_PASS_BINARY = "pass"


@dataclass(frozen=True)
class Credentials:
    """Resolved credentials for one connection."""

    username: str
    password: str
    secret: str | None = None


def require_pass_binary() -> str:
    """Return the path to ``pass`` or raise :class:`CredentialError`."""
    binary = shutil.which(_PASS_BINARY)
    if binary is None:
        raise CredentialError(f"{_PASS_BINARY} executable not found on PATH")
    return binary


def parse_entry(content: str) -> Credentials:
    lines = content.splitlines()
    if not lines or not lines[0]:
        raise CredentialError("pass entry is empty: expected password on first line")
    password = lines[0]
    username: str | None = None
    secret: str | None = None
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "username":
            username = value
        elif key == "secret":
            secret = value
    if not username:
        raise CredentialError(
            "pass entry has no 'username:' field; expected password then username"
        )
    return Credentials(username=username, password=password, secret=secret)


class PassBackend:
    """Retrieve credentials for a :class:`CredentialProfile` via ``pass``."""

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary or require_pass_binary()

    def resolve(self, profile: CredentialProfile) -> Credentials:
        """Return credentials for ``profile``, raising on any failure."""
        if profile.backend != "pass":
            raise CredentialError(
                f"unsupported credential backend {profile.backend!r}"
            )
        try:
            completed = subprocess.run(
                [self._binary, "show", profile.entry],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise CredentialError(
                f"failed to run pass for entry {profile.entry!r}: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise CredentialError(
                f"pass could not show entry {profile.entry!r}"
            )
        try:
            return parse_entry(completed.stdout)
        except CredentialError as exc:
            raise CredentialError(
                f"invalid pass entry {profile.entry!r}: {exc}"
            ) from exc
