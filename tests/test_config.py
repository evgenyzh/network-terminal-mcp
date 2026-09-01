"""Tests for configuration models and loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from network_terminal_mcp.config.loader import (
    ConfigError,
    load_config,
)
from network_terminal_mcp.config.models import (
    ConnectionsConfig,
    CredentialProfile,
    Device,
    InventoryConfig,
    PolicyConfig,
    RuntimeConfig,
)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    return directory


def _write(directory: Path, name: str, data: object) -> None:
    (directory / name).write_text(yaml.safe_dump(data), encoding="utf-8")


def test_device_model_valid() -> None:
    device = Device.model_validate(
        {
            "host": "192.0.2.10",
            "credentials": "network-tacacs",
            "connection": "direct",
            "tags": ["lab"],
        }
    )
    assert device.host == "192.0.2.10"
    assert device.allow_telnet is False
    assert device.allow_writes is False


def test_device_rejects_unknown_field() -> None:
    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        Device.model_validate({"host": "h", "typo": 1})


def test_connection_discriminated_union_direct() -> None:
    config = ConnectionsConfig.model_validate(
        {
            "connections": {
                "direct": {"type": "direct", "protocol": "ssh"},
                "legacy": {
                    "type": "direct",
                    "protocol": "legacy_ssh",
                    "host_key_algorithms": ["ssh-rsa"],
                },
            }
        }
    )
    assert config.connections["direct"].type == "direct"
    legacy = config.connections["legacy"]
    assert legacy.protocol == "legacy_ssh"
    assert legacy.host_key_algorithms == ["ssh-rsa"]  # type: ignore[attr-defined]


def test_connection_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        ConnectionsConfig.model_validate(
            {"connections": {"x": {"type": "warp"}}}
        )


def test_console_connection_requires_a_port_and_rejects_connect_command() -> None:
    config = ConnectionsConfig.model_validate(
        {"connections": {"console": {"type": "console", "port": 2002}}}
    )
    assert config.connections["console"].port == 2002  # type: ignore[attr-defined]

    with pytest.raises(ValidationError, match="requires a port"):
        ConnectionsConfig.model_validate({"connections": {"console": {"type": "console"}}})

    with pytest.raises(ValidationError, match="not supported"):
        ConnectionsConfig.model_validate(
            {
                "connections": {
                    "console": {"type": "console", "port": 2002, "connect_command": "picocom"}
                }
            }
        )


def test_proxyjump_connection_requires_ssh_and_defaults_to_strict_keys() -> None:
    config = ConnectionsConfig.model_validate(
        {
            "connections": {
                "jump": {
                    "type": "proxyjump",
                    "jump_host": "jump.example.net",
                    "jump_credentials": "terminal",
                }
            }
        }
    )

    profile = config.connections["jump"]
    assert profile.protocol == "ssh"
    assert profile.jump_port == 22  # type: ignore[attr-defined]
    assert profile.host_key_policy == "strict"  # type: ignore[attr-defined]
    assert profile.jump_host_key_policy == "strict"  # type: ignore[attr-defined]

    with pytest.raises(ValidationError):
        ConnectionsConfig.model_validate(
            {
                "connections": {
                    "jump": {
                        "type": "proxyjump",
                        "protocol": "telnet",
                        "jump_host": "jump.example.net",
                        "jump_credentials": "terminal",
                    }
                }
            }
        )


def test_socks_proxyjump_connection_defaults() -> None:
    config = ConnectionsConfig.model_validate(
        {
            "connections": {
                "jump": {
                    "type": "proxyjump",
                    "socks": {"host": "127.0.0.1", "port": 10900},
                }
            }
        }
    )

    profile = config.connections["jump"]
    assert profile.protocol == "ssh"  # type: ignore[attr-defined]
    assert profile.jump_host is None  # type: ignore[attr-defined]
    assert profile.socks.host == "127.0.0.1"  # type: ignore[attr-defined]
    assert profile.socks.port == 10900  # type: ignore[attr-defined]
    assert profile.host_key_policy == "strict"  # type: ignore[attr-defined]


def test_proxyjump_requires_socks_or_jump_host() -> None:
    with pytest.raises(ValidationError):
        ConnectionsConfig.model_validate(
            {"connections": {"jump": {"type": "proxyjump"}}}
        )

    with pytest.raises(ValidationError):
        ConnectionsConfig.model_validate(
            {
                "connections": {
                    "jump": {
                        "type": "proxyjump",
                        "socks": {"host": "127.0.0.1", "port": 10900},
                        "jump_host": "jump.example.net",
                        "jump_credentials": "terminal",
                    }
                }
            }
        )


def test_nested_connection_defaults_and_rejects_unsupported_protocols() -> None:
    config = ConnectionsConfig.model_validate(
        {
            "connections": {
                "nested": {
                    "type": "nested",
                    "host": "term.example.net",
                    "protocol": "ssh",
                    "credentials": "intermediate",
                    "next_protocol": "ssh",
                }
            }
        }
    )

    profile = config.connections["nested"]
    assert profile.protocol == "ssh"  # type: ignore[attr-defined]
    assert profile.next_protocol == "ssh"  # type: ignore[attr-defined]
    assert profile.host_key_policy == "strict"  # type: ignore[attr-defined]
    assert profile.port is None  # type: ignore[attr-defined]

    telnet_config = ConnectionsConfig.model_validate(
        {
            "connections": {
                "nested": {
                    "type": "nested",
                    "host": "term.example.net",
                    "protocol": "ssh",
                    "credentials": "intermediate",
                    "next_protocol": "telnet",
                }
            }
        }
    )
    assert telnet_config.connections["nested"].next_protocol == "telnet"


def test_credential_profile_requires_entry_and_username() -> None:
    profile = CredentialProfile.model_validate(
        {"backend": "pass", "entry": "a/b", "username": "operator"}
    )
    assert profile.entry == "a/b"
    assert profile.username == "operator"
    with pytest.raises(ValidationError):
        CredentialProfile.model_validate({"backend": "pass"})

    key = CredentialProfile.model_validate(
        {"backend": "ssh_key", "key_file": "~/.ssh/id_ed25519", "username": "operator"}
    )
    assert key.key_file == Path("~/.ssh/id_ed25519").expanduser()

    with pytest.raises(ValidationError):
        CredentialProfile.model_validate(
            {"backend": "ssh_key", "entry": "not-allowed", "username": "operator"}
        )

    with pytest.raises(ValidationError):
        CredentialProfile.model_validate(
            {
                "backend": "pass",
                "entry": "network/password",
                "key_passphrase_entry": "not-allowed",
                "username": "operator",
            }
        )


def test_runtime_expands_user_home() -> None:
    runtime = RuntimeConfig.model_validate({"audit_file": "~/x/audit.jsonl"})
    assert str(runtime.audit_file) == str(Path("~/x/audit.jsonl").expanduser())
    assert runtime.session_idle_timeout == 300
    assert runtime.cli_help_timeout == 5
    assert runtime.max_pager_pages == 32
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"max_pager_pages": 0})


def test_load_missing_files_yields_defaults(config_dir: Path) -> None:
    config = load_config(config_dir)
    assert config.inventory.devices == {}
    assert config.connections.connections == {}
    assert config.credentials.credentials == {}
    assert config.policy.rules == []


def test_load_full_config(config_dir: Path) -> None:
    _write(
        config_dir,
        "inventory.yml",
        {
            "devices": {
                "sw1": {
                    "host": "192.0.2.1",
                    "credentials": "net",
                    "connection": "direct",
                }
            }
        },
    )
    _write(
        config_dir,
        "connections.yml",
        {"connections": {"direct": {"type": "direct", "protocol": "ssh"}}},
    )
    _write(
        config_dir,
        "credentials.yml",
        {
            "credentials": {
                "net": {"backend": "pass", "entry": "n/c", "username": "operator"}
            }
        },
    )
    _write(config_dir, "policy.yml", {"defaults": {"unknown_exec_command": "ask"}})

    config = load_config(config_dir)
    assert config.inventory.devices["sw1"].host == "192.0.2.1"
    assert config.credentials.credentials["net"].entry == "n/c"
    assert config.policy.defaults.unknown_exec_command == "ask"


def test_load_raises_on_broken_yaml(config_dir: Path) -> None:
    (config_dir / "policy.yml").write_text("::: not yaml :::", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(config_dir)


def test_load_rejects_unknown_fields(config_dir: Path) -> None:
    _write(
        config_dir,
        "inventory.yml",
        {"devices": {"sw1": {"host": "h", "typo": True}}},
    )
    with pytest.raises(ConfigError, match="invalid inventory.yml"):
        load_config(config_dir)


def test_reference_validation_catches_missing_profiles(config_dir: Path) -> None:
    _write(
        config_dir,
        "inventory.yml",
        {
            "devices": {
                "sw1": {
                    "host": "192.0.2.1",
                    "credentials": "missing-creds",
                    "connection": "missing-conn",
                }
            }
        },
    )
    with pytest.raises(ConfigError, match="unknown credential profile"):
        load_config(config_dir)


def test_reference_validation_catches_missing_jump_credentials(config_dir: Path) -> None:
    _write(
        config_dir,
        "inventory.yml",
        {
            "devices": {
                "sw1": {
                    "host": "192.0.2.1",
                    "credentials": "net",
                    "connection": "jump",
                }
            }
        },
    )
    _write(
        config_dir,
        "connections.yml",
        {
            "connections": {
                "jump": {
                    "type": "proxyjump",
                    "jump_host": "192.0.2.254",
                    "jump_credentials": "missing-jump",
                }
            }
        },
    )
    _write(
        config_dir,
        "credentials.yml",
        {"credentials": {"net": {"backend": "pass", "entry": "n/c", "username": "operator"}}},
    )

    with pytest.raises(ConfigError, match="unknown jump credential profile"):
        load_config(config_dir)


def test_reference_validation_catches_missing_nested_credentials(config_dir: Path) -> None:
    _write(
        config_dir,
        "inventory.yml",
        {
            "devices": {
                "sw1": {
                    "host": "192.0.2.1",
                    "credentials": "net",
                    "connection": "nested-term",
                }
            }
        },
    )
    _write(
        config_dir,
        "connections.yml",
        {
            "connections": {
                "nested-term": {
                    "type": "nested",
                    "host": "192.0.2.254",
                    "credentials": "missing-intermediate",
                    "next_protocol": "ssh",
                }
            }
        },
    )
    _write(
        config_dir,
        "credentials.yml",
        {"credentials": {"net": {"backend": "pass", "entry": "n/c", "username": "operator"}}},
    )

    with pytest.raises(ConfigError, match="unknown intermediate credential profile"):
        load_config(config_dir)


def test_inventory_config_roundtrip() -> None:
    data = {"devices": {"a": {"host": "h", "credentials": "c", "connection": "d"}}}
    inventory = InventoryConfig.model_validate(data)
    assert list(inventory.devices) == ["a"]


def test_policy_config_defaults() -> None:
    policy = PolicyConfig()
    assert policy.defaults.cli_help == "allow"
    assert policy.defaults.raw_input == "deny"
    assert policy.defaults.telnet == "deny"
    assert policy.defaults.write_change == "deny"
