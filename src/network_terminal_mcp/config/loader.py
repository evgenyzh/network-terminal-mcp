"""Configuration loading and validation.

Configuration lives in ``~/.config/network-terminal-mcp/`` (overridable via the
``NETWORK_MCP_CONFIG_DIR`` environment variable) as four YAML files:

- ``inventory.yml``
- ``connections.yml``
- ``credentials.yml``
- ``policy.yml``

Files are optional: missing files produce empty/default configuration. Present
files are validated strictly and must not contain unknown fields.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from network_terminal_mcp.config.models import (
    ConnectionsConfig,
    CredentialsConfig,
    InventoryConfig,
    PolicyConfig,
    ProxyJumpConnection,
)
from network_terminal_mcp.errors import ConfigError

if TYPE_CHECKING:
    from pydantic import BaseModel

CONFIG_DIR_ENV = "NETWORK_MCP_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("~/.config/network-terminal-mcp")

_FILE_NAMES: tuple[str, ...] = (
    "inventory.yml",
    "connections.yml",
    "credentials.yml",
    "policy.yml",
)


class AppConfig:
    """Aggregate of all four configuration files."""

    def __init__(
        self,
        *,
        inventory: InventoryConfig,
        connections: ConnectionsConfig,
        credentials: CredentialsConfig,
        policy: PolicyConfig,
        config_dir: Path,
    ) -> None:
        self.inventory = inventory
        self.connections = connections
        self.credentials = credentials
        self.policy = policy
        self.config_dir = config_dir

    def validate_references(self) -> None:
        """Cross-check references between files.

        Credential and connection profiles referenced from inventory must exist,
        otherwise an ad-hoc or inventory target cannot be resolved.
        """
        for name, device in self.inventory.devices.items():
            if device.credentials not in self.credentials.credentials:
                raise ConfigError(
                    f"device {name!r} references unknown credential profile "
                    f"{device.credentials!r}"
                )
            if device.connection not in self.connections.connections:
                raise ConfigError(
                    f"device {name!r} references unknown connection profile "
                    f"{device.connection!r}"
                )
        for name, profile in self.connections.connections.items():
            if (
                isinstance(profile, ProxyJumpConnection)
                and profile.jump_credentials not in self.credentials.credentials
            ):
                raise ConfigError(
                    f"connection {name!r} references unknown jump credential profile "
                    f"{profile.jump_credentials!r}"
                )

    def to_summary(self) -> str:
        """Human-readable summary used by the CLI health check."""
        devices = len(self.inventory.devices)
        groups = len(self.inventory.groups)
        connections = len(self.connections.connections)
        platforms = len(self.connections.platforms)
        credentials = len(self.credentials.credentials)
        rules = len(self.policy.rules)
        return (
            f"devices={devices} groups={groups} "
            f"connections={connections} platforms={platforms} "
            f"credentials={credentials} policy_rules={rules}"
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
    """Load and validate all configuration files from ``config_dir``."""
    directory = (config_dir or default_config_dir()).resolve()

    inventory_data = _load_yaml_file(directory / "inventory.yml")
    connections_data = _load_yaml_file(directory / "connections.yml")
    credentials_data = _load_yaml_file(directory / "credentials.yml")
    policy_data = _load_yaml_file(directory / "policy.yml")

    def _construct[T: BaseModel](
        model: type[T], data: dict[str, object], filename: str
    ) -> T:
        try:
            return model.model_validate(data)
        except Exception as exc:
            raise ConfigError(f"invalid {filename}: {exc}") from exc

    inventory = _construct(InventoryConfig, inventory_data, "inventory.yml")
    connections = _construct(ConnectionsConfig, connections_data, "connections.yml")
    credentials = _construct(CredentialsConfig, credentials_data, "credentials.yml")
    policy = _construct(PolicyConfig, policy_data, "policy.yml")

    config = AppConfig(
        inventory=inventory,
        connections=connections,
        credentials=credentials,
        policy=policy,
        config_dir=directory,
    )
    config.validate_references()
    return config
