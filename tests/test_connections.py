"""Tests for model-described connection plan building."""

from __future__ import annotations

import pytest

from network_terminal_mcp.config.models import OpenSpec
from network_terminal_mcp.connections.plan import (
    ConnectionPlan,
    build_plan,
    default_port,
    route_label,
)


def _spec(**overrides: object) -> OpenSpec:
    data: dict[str, object] = {
        "host": "192.0.2.1",
        "credentials": {"entry": "network/net", "username": "operator"},
    }
    data.update(overrides)
    return OpenSpec.model_validate(data)


def _plan(**overrides: object) -> ConnectionPlan:
    return build_plan(_spec(**overrides))


def test_default_port_per_protocol() -> None:
    assert default_port("ssh") == 22
    assert default_port("telnet") == 23
    assert default_port("console") == 22


def test_direct_plan_defaults() -> None:
    plan = _plan()
    assert plan.host == "192.0.2.1"
    assert plan.port == 22
    assert plan.protocol == "ssh"
    assert plan.route is None
    assert plan.host_key_policy == "strict"
    assert plan.warnings == ()
    assert route_label(plan) == "direct"


def test_telnet_plan_uses_port_23_and_warns() -> None:
    plan = _plan(protocol="telnet", allow_telnet=True)
    assert plan.port == 23
    assert route_label(plan) == "telnet"
    assert any("cleartext" in warning for warning in plan.warnings)


def test_console_plan_keeps_the_explicit_port() -> None:
    plan = _plan(protocol="console", port=2002, allow_telnet=True)
    assert plan.port == 2002
    assert route_label(plan) == "console"


def test_serial_plan_carries_serial_params_without_a_port() -> None:
    plan = _plan(
        protocol="serial",
        host="/dev/ttyUSB0",
        credentials=None,
        allow_serial=True,
        serial={"baudrate": 115200, "parity": "E", "stopbits": 2},
    )
    assert plan.port is None
    assert plan.baudrate == 115200
    assert plan.parity == "E"
    assert plan.stopbits == 2
    assert route_label(plan) == "serial"
    assert any("local serial console" in warning for warning in plan.warnings)


def test_socks_route_labels_and_warns() -> None:
    plan = _plan(route={"type": "socks", "host": "127.0.0.1", "port": 1080})
    assert route_label(plan) == "socks"
    assert any("SOCKS proxy" in warning for warning in plan.warnings)


def test_proxyjump_route_labels() -> None:
    plan = _plan(
        route={
            "type": "proxyjump",
            "host": "192.0.2.254",
            "credentials": {"entry": "network/jump", "username": "jump-operator"},
        }
    )
    assert route_label(plan) == "proxyjump"


def test_plaintext_and_legacy_are_reported_in_warnings() -> None:
    plan = _plan(
        credentials={
            "backend": "plaintext",
            "username": "operator",
            "password": "hunter2",
        },
        allow_plaintext_password=True,
        legacy={"ciphers": ["aes128-cbc"]},
    )
    assert any("plaintext password supplied" in warning for warning in plan.warnings)
    assert any("legacy SSH algorithm overrides" in warning for warning in plan.warnings)


def test_build_plan_requires_an_allow_flag_for_telnet() -> None:
    with pytest.raises(Exception, match="allow_telnet"):
        _spec(protocol="telnet")
