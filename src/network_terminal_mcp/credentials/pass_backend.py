"""Credential retrieval from the ``pass`` password store.

Secrets live in GPG-encrypted ``pass`` entries, never in MCP arguments or
results. The server shells out to ``pass`` without interpolation, so the entry
name is always taken from local configuration rather than model input.

The entry's first line is the password. The non-secret AAA username belongs to
the local credential profile, so it can be changed independently of the
password-store entry.
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
            return Credentials(username=profile.username, password=parse_entry(completed.stdout))
        except CredentialError as exc:
            raise CredentialError(
                f"invalid pass entry {profile.entry!r}: {exc}"
            ) from exc
