"""Command policy engine.

Decisions are ``allow``, ``ask``, or ``deny``. Rules are matched in order; the
first matching rule wins. Unknown exec commands fall back to
``defaults.unknown_exec_command``.

Structural safety checks run before any pattern matching: newlines, carriage
returns, command chaining separators, and shell metacharacters are always
rejected.
"""

from __future__ import annotations

import fnmatch
import re

from network_terminal_mcp.config.models import Action, PolicyConfig, PolicyRule
from network_terminal_mcp.errors import PolicyError

# Always-denied structural features, independent of configuration.
_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("newline", re.compile(r"[\r\n]")),
    ("command chaining", re.compile(r"[;&|]")),
    ("shell metacharacters", re.compile(r"[`$()<>{}]")),
]

_KEYWORDS_DENY: tuple[str, ...] = (
    "reload",
    "reboot",
    "reset",
    "erase",
    "factory-reset",
    "delete",
    "clear",
)

# Word-boundary patterns for entering configuration mode. Plain "config"
# is intentionally absent: "running-config" and similar are not mode entries.
_CONFIG_MODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bconfigure terminal\b"),
    re.compile(r"\bconf t\b"),
    re.compile(r"\bconfigure\b"),
    re.compile(r"\bconfig terminal\b"),
    re.compile(r"\bsystem-view\b"),
    re.compile(r"\bsystem-view-port\b"),
    re.compile(r"\bedit\b"),
    re.compile(r"\bcli\b"),
)


def check_structural_safety(command: str) -> None:
    """Reject command strings that are structurally dangerous.

    Raises :class:`PolicyError` if the command contains newlines, chaining
    operators, or shell metacharacters.
    """
    for label, pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(command):
            raise PolicyError(
                f"command rejected: {label} not allowed in run_command"
            )


def is_destructive(command: str) -> bool:
    """Return True if the command targets destructive vendor operations."""
    first = command.strip().split(maxsplit=1)[0].lower()
    return first in _KEYWORDS_DENY or first in ("erase", "clear")


def is_config_mode_enter(command: str) -> bool:
    """Return True if the command switches the device into config mode."""
    return any(pattern.search(command) for pattern in _CONFIG_MODE_PATTERNS)


class PolicyEngine:
    """Evaluate commands against configured defaults and rules."""

    def __init__(self, policy: PolicyConfig) -> None:
        self._policy = policy

    @property
    def policy(self) -> PolicyConfig:
        return self._policy

    def _match_rule(self, command: str) -> PolicyRule | None:
        for rule in self._policy.rules:
            for pattern in rule.command_patterns:
                if fnmatch.fnmatch(command, pattern):
                    return rule
        return None

    def evaluate(self, command: str) -> Action:
        """Return the policy decision for ``command``.

        Structural hazards raise :class:`PolicyError` (effectively a hard deny
        that cannot be overridden by confirmation).
        """
        check_structural_safety(command)

        if is_destructive(command):
            return "deny"

        if is_config_mode_enter(command):
            # Entering configuration mode always requires confirmation: the
            # config_mode default gates it and only an explicit deny rule wins.
            rule = self._match_rule(command)
            if rule is not None and rule.action == "deny":
                return "deny"
            return self._policy.defaults.config_mode

        rule = self._match_rule(command)
        if rule is not None:
            return rule.action

        return self._policy.defaults.unknown_exec_command

    def decision_label(self, command: str) -> str:
        """Human-readable decision for logging."""
        return self.evaluate(command)
