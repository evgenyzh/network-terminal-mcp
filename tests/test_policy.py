"""Tests for the command policy engine."""

from __future__ import annotations

import pytest

from network_terminal_mcp.config.models import PolicyConfig, PolicyRule
from network_terminal_mcp.errors import PolicyError
from network_terminal_mcp.policy.engine import (
    PolicyEngine,
    check_structural_safety,
    is_config_mode_enter,
    is_destructive,
)


def _engine(rules: list[PolicyRule] | None = None, **defaults: str) -> PolicyEngine:
    policy = PolicyConfig.model_validate(
        {
            "defaults": defaults or {"unknown_exec_command": "ask"},
            "rules": [rule.model_dump() for rule in (rules or [])],
        }
    )
    return PolicyEngine(policy)


def test_allow_rule_matches_glob() -> None:
    engine = _engine([PolicyRule(id="ro", action="allow", command_patterns=["show *"])])
    assert engine.evaluate("show version") == "allow"
    assert engine.evaluate("display transceiver") == "ask"


def test_first_matching_rule_wins() -> None:
    engine = _engine(
        [
            PolicyRule(id="ro", action="allow", command_patterns=["show *"]),
            PolicyRule(id="exp", action="ask", command_patterns=["show tech-support*"]),
        ]
    )
    assert engine.evaluate("show version") == "allow"
    assert engine.evaluate("show tech-support brief") == "allow"


def test_unknown_exec_falls_back_to_default() -> None:
    engine = _engine(unknown_exec_command="ask")
    assert engine.evaluate("foobar baz") == "ask"


def test_destructive_commands_are_always_denied() -> None:
    engine = _engine(unknown_exec_command="allow")
    for command in ("reload", "reboot", "erase startup-config", "delete flash:file"):
        assert engine.evaluate(command) == "deny", command


def test_config_mode_enter_uses_config_default() -> None:
    engine = _engine(config_mode="deny")
    assert engine.evaluate("configure terminal") == "deny"
    assert engine.evaluate("system-view") == "deny"


def test_config_mode_enter_overrides_allow_rule() -> None:
    engine = _engine(
        [PolicyRule(id="catchall", action="allow", command_patterns=["configure *"])],
        config_mode="ask",
    )
    assert engine.evaluate("configure terminal") == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "show version\nreload",
        "show\rversion",
        "show version; reload",
        "show version && reload",
        "show $(hostname)",
        "show `id`",
        "show > /tmp/x",
    ],
)
def test_structural_hazards_raise_policy_error(command: str) -> None:
    engine = _engine(unknown_exec_command="allow")
    with pytest.raises(PolicyError, match="rejected"):
        engine.evaluate(command)


def test_check_structural_safety_accepts_plain_commands() -> None:
    check_structural_safety("show mac address-table")
    check_structural_safety("display current-configuration")


@pytest.mark.parametrize(
    "command,expected",
    [
        ("show version", False),
        ("reload", True),
        ("erase startup-config", True),
        ("clear counters", True),
        ("delete file", True),
    ],
)
def test_is_destructive(command: str, expected: bool) -> None:
    assert is_destructive(command) is expected


@pytest.mark.parametrize(
    "command,expected",
    [
        ("configure terminal", True),
        ("conf t", True),
        ("system-view", True),
        ("show running-config", False),
    ],
)
def test_is_config_mode_enter(command: str, expected: bool) -> None:
    assert is_config_mode_enter(command) is expected


def test_decision_label_matches_evaluate() -> None:
    engine = _engine(unknown_exec_command="ask")
    assert engine.decision_label("random command") == "ask"
