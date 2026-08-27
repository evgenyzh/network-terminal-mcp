"""Tests for the structured error hierarchy."""

from __future__ import annotations

import pytest

from network_terminal_mcp.errors import (
    AuditError,
    ConfigError,
    CredentialError,
    NetworkMCPError,
    PolicyError,
    SessionError,
    TargetError,
    TransportError,
)


def test_code_is_in_message() -> None:
    error = ConfigError("broken")
    assert error.code == "config_error"
    assert "config_error: broken" == str(error)


@pytest.mark.parametrize(
    "error",
    [
        ConfigError("x"),
        CredentialError("x"),
        PolicyError("x"),
        SessionError("x"),
        AuditError("x"),
        TransportError("x"),
        TargetError("x"),
    ],
)
def test_subclasses_carry_unique_codes(error: NetworkMCPError) -> None:
    assert error.code.endswith("_error")
    assert error.message == "x"


def test_as_dict_is_json_serializable() -> None:
    payload = ConfigError("bad config").as_dict()
    assert payload == {"code": "config_error", "message": "bad config"}
