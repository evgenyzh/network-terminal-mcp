"""Tests for secret redaction."""

from __future__ import annotations

import pytest

from network_terminal_mcp.redaction import REDACTED, Redactor


def test_empty_redactor_returns_text() -> None:
    redactor = Redactor()
    assert redactor.redact("show version") == "show version"


def test_simple_replacement() -> None:
    redactor = Redactor(["hunter2"])
    assert redactor.redact("password: hunter2") == f"password: {REDACTED}"


def test_replaces_all_occurrences() -> None:
    redactor = Redactor(["abc"])
    assert redactor.redact("abc and abc again") == f"{REDACTED} and {REDACTED} again"


def test_prefix_secret_longest_first() -> None:
    redactor = Redactor(["secret", "secreto"])
    assert redactor.redact("secreto") == REDACTED


def test_redacts_error_message() -> None:
    redactor = Redactor(["hunter2"])
    original = RuntimeError("login failed with hunter2")
    redacted = redactor.redact_error(original)
    assert isinstance(redacted, RuntimeError)
    assert REDACTED in str(redacted)
    assert "hunter2" not in str(redacted)


def test_ignores_empty_secrets() -> None:
    redactor = Redactor(["", "  "])
    assert not redactor
    assert redactor.redact("unchanged") == "unchanged"


@pytest.mark.parametrize("text", ["", "no secrets here"])
def test_no_secret_no_change(text: str) -> None:
    assert Redactor(["x"]).redact(text) == text


def test_add_registers_new_secrets_longest_first() -> None:
    redactor = Redactor(["hunter2"])
    redactor.add("typed-secret", "typed", "")
    assert redactor.redact("typed typed-secret hunter2") == (
        f"{REDACTED} {REDACTED} {REDACTED}"
    )


def test_add_ignores_empty_values() -> None:
    redactor = Redactor()
    redactor.add("", "   ")
    assert not redactor
