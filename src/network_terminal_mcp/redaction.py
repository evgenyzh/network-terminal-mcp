"""Secret redaction helpers.

Passwords, enable secrets, and TACACS keys must never leak into tool results,
audit records, exceptions, or logs. This module provides a single place that
rewrites text, replacing known secrets with a fixed placeholder.

Redaction is best-effort: it only works for secrets we actually know about.
The real control is that secrets are held only inside the credential backend
and are never accepted from or returned to MCP tool arguments/results.
"""

from __future__ import annotations

from collections.abc import Iterable

REDACTED = "<redacted>"


class Redactor:
    """Replace a known set of secret values inside arbitrary text."""

    __slots__ = ("_secrets",)

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        # Longest-first so a secret that is a prefix of another never shadows
        # the longer replacement in a partially redacted string. Empty and
        # whitespace-only secrets are dropped.
        self._secrets: tuple[str, ...] = tuple(
            sorted({s for s in secrets if s.strip()}, key=len, reverse=True)
        )

    @property
    def secrets(self) -> tuple[str, ...]:
        """The known secret values (read-only)."""
        return self._secrets

    def __bool__(self) -> bool:
        return bool(self._secrets)

    def redact(self, text: str) -> str:
        """Return ``text`` with every known secret replaced."""
        if not self._secrets:
            return text
        result = text
        for secret in self._secrets:
            if secret in result:
                result = result.replace(secret, REDACTED)
        return result

    def redact_error(self, error: BaseException) -> BaseException:
        """Return an equivalent exception whose message has secrets removed."""
        message = self.redact(str(error))
        return type(error)(message)


def make_redactor(secrets: Iterable[str]) -> Redactor:
    """Convenience factory used by callers that do not hold a Redactor yet."""
    return Redactor(secrets)
