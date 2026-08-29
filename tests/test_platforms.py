"""Tests for platform alias resolution."""

from __future__ import annotations

import pytest

from network_terminal_mcp.config.models import ConnectionsConfig
from network_terminal_mcp.errors import TransportError
from network_terminal_mcp.platforms import PlatformRegistry


def test_stock_driver_resolves_without_alias() -> None:
    registry = PlatformRegistry(ConnectionsConfig())
    assert registry.resolve("cisco_ios").driver == "cisco_ios"


def test_alias_carries_driver_and_dialect() -> None:
    registry = PlatformRegistry(
        ConnectionsConfig.model_validate(
            {
                "platforms": {
                    "snr_29xx": {"driver": "cisco_ios", "dialect": "snr_29xx"}
                }
            }
        )
    )
    platform = registry.resolve("snr_29xx")
    assert platform.driver == "cisco_ios"
    assert platform.dialect == "snr_29xx"


def test_alias_carries_cli_help_requires_enter() -> None:
    registry = PlatformRegistry(
        ConnectionsConfig.model_validate(
            {
                "platforms": {
                    "dlink_ds": {
                        "driver": "dlink_ds",
                        "dialect": "dlink_ds",
                        "cli_help_requires_enter": True,
                    }
                }
            }
        )
    )
    assert registry.resolve("dlink_ds").cli_help_requires_enter is True
    assert registry.resolve("cisco_ios").cli_help_requires_enter is False


def test_local_adapter_is_deferred() -> None:
    registry = PlatformRegistry(
        ConnectionsConfig.model_validate(
            {"platforms": {"bdcom": {"driver": "local:bdcom_huawei_like"}}}
        )
    )
    with pytest.raises(TransportError, match="not implemented"):
        registry.resolve("bdcom")


def test_unknown_driver_rejected() -> None:
    registry = PlatformRegistry(ConnectionsConfig())
    with pytest.raises(TransportError, match="unsupported platform"):
        registry.resolve("not-a-driver")
