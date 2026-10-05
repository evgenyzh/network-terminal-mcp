"""Configuration loading and validation.

Only one optional YAML file remains: ``policy.yml`` (policy posture and
runtime limits) in ``~/.config/network-terminal-mcp/``, overridable via the
``NETWORK_MCP_CONFIG_DIR`` environment variable. Connection, credential, and
inventory descriptions are supplied per ``open_session`` call instead.

Missing files produce built-in defaults, so a fresh install works read-only
without any local configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from network_terminal_mcp.config.models import PolicyConfig
from network_terminal_mcp.errors import ConfigError

CONFIG_DIR_ENV = "NETWORK_MCP_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("~/.config/network-terminal-mcp")

_POLICY_FILE = "policy.yml"


class AppConfig:
    """Local policy and runtime configuration."""

    def __init__(self, *, policy: PolicyConfig, config_dir: Path) -> None:
        self.policy = policy
        self.config_dir = config_dir

    def to_summary(self) -> str:
        """Human-readable summary used by the CLI health check."""
        runtime = self.policy.runtime
        return (
            f"audit_file={runtime.audit_file} "
            f"known_hosts_file={runtime.known_hosts_file} "
            f"max_open_sessions={runtime.max_open_sessions} "
            f"session_idle_timeout={runtime.session_idle_timeout} "
            f"session_max_lifetime={runtime.session_max_lifetime}"
        )


def default_config_dir() -> Path:
    """Return the configured or default configuration directory."""
    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_DIR.expanduser()


def _load_yaml_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def load_config(config_dir: Path | None = None) -> AppConfig:
    """Load and validate the optional local policy file."""
    directory = (config_dir or default_config_dir()).resolve()
    policy_data = _load_yaml_file(directory / _POLICY_FILE)
    try:
        policy = PolicyConfig.model_validate(policy_data)
    except Exception as exc:
        raise ConfigError(f"invalid {_POLICY_FILE}: {exc}") from exc
    return AppConfig(policy=policy, config_dir=directory)
