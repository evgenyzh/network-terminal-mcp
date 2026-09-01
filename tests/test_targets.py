"""Tests for device target resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from network_terminal_mcp.config.loader import AppConfig
from network_terminal_mcp.config.models import (
    ConnectionsConfig,
    CredentialsConfig,
    InventoryConfig,
    PolicyConfig,
)
from network_terminal_mcp.errors import TargetError
from network_terminal_mcp.targets.resolver import TargetResolver


def _config(**inventory: object) -> AppConfig:
    devices = inventory.get("devices", {})
    connections = inventory.get("connections", {"direct": {"type": "direct", "protocol": "ssh"}})
    credentials = inventory.get(
        "credentials",
        {"net": {"backend": "pass", "entry": "n/c", "username": "operator"}},
    )
    inv_data: dict[str, object] = {"devices": devices}
    for key in ("default_credentials", "default_connection"):
        if inventory.get(key) is not None:
            inv_data[key] = inventory[key]
    return AppConfig(
        inventory=InventoryConfig.model_validate(inv_data),
        connections=ConnectionsConfig.model_validate(
            {"connections": connections}
        ),
        credentials=CredentialsConfig.model_validate(
            {"credentials": credentials}
        ),
        policy=PolicyConfig(),
        config_dir=Path("."),
    )


def test_resolve_named_device() -> None:
    config = _config(
        devices={
            "sw1": {
                "host": "192.0.2.1",
                "credentials": "net",
                "connection": "direct",
                "tags": ["lab"],
            }
        }
    )
    target = TargetResolver(config).resolve(name="sw1")
    assert target.name == "sw1"
    assert target.host == "192.0.2.1"
    assert target.tags == ("lab",)
    assert target.allow_writes is False


def test_resolve_named_device_carries_allow_writes() -> None:
    config = _config(
        devices={
            "sw1": {
                "host": "192.0.2.1",
                "credentials": "net",
                "connection": "direct",
                "allow_writes": True,
            }
        }
    )
    target = TargetResolver(config).resolve(name="sw1")
    assert target.allow_writes is True


def test_resolve_unknown_device_raises() -> None:
    with pytest.raises(TargetError, match="unknown device"):
        TargetResolver(_config()).resolve(name="nope")


def test_resolve_ad_hoc_target() -> None:
    config = _config()
    target = TargetResolver(config).resolve(
        host="192.0.2.5",
        credentials="net",
        connection="direct",
    )
    assert target.host == "192.0.2.5"
    assert target.name == "<ad-hoc>"


def test_ad_hoc_requires_host() -> None:
    with pytest.raises(TargetError, match="host"):
        TargetResolver(_config()).resolve(
            credentials="net", connection="direct"
        )


def test_ad_hoc_uses_configured_defaults_for_credentials_and_connection() -> None:
    config = _config(
        default_credentials="net",
        default_connection="direct",
    )
    target = TargetResolver(config).resolve(host="192.0.2.7")
    assert target.credentials == "net"
    assert target.connection == "direct"
    assert target.host == "192.0.2.7"


def test_ad_hoc_requires_credentials_without_default() -> None:
    config = _config()
    with pytest.raises(TargetError, match="credentials"):
        TargetResolver(config).resolve(host="192.0.2.7")


def test_ad_hoc_rejects_unknown_fields() -> None:
    with pytest.raises(TargetError, match="unsupported ad-hoc"):
        TargetResolver(_config()).resolve(
            host="h", credentials="net", connection="direct", password="x"
        )


def test_ad_hoc_rejects_unknown_credentials_profile() -> None:
    with pytest.raises(TargetError, match="unknown credential profile"):
        TargetResolver(_config()).resolve(
            host="h", credentials="nope", connection="direct"
        )


def test_ad_hoc_rejects_unknown_connection_profile() -> None:
    with pytest.raises(TargetError, match="unknown connection profile"):
        TargetResolver(_config()).resolve(
            host="h", credentials="net", connection="nope"
        )


def test_ad_hoc_port_must_be_int() -> None:
    with pytest.raises(TargetError, match="integer"):
        TargetResolver(_config()).resolve(
            host="h", credentials="net", connection="direct", port="22"
        )
