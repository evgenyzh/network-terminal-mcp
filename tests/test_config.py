"""Tests for connection specs, policy models, and the loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from network_terminal_mcp.config.loader import (
    load_config,
)
from network_terminal_mcp.config.models import (
    CredentialSpec,
    OpenSpec,
    PolicyConfig,
    RuntimeConfig,
)
from network_terminal_mcp.errors import ConfigError


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    return directory


def _write(directory: Path, name: str, data: object) -> None:
    (directory / name).write_text(yaml.safe_dump(data), encoding="utf-8")


def _pass_credentials() -> dict[str, object]:
    return {"backend": "pass", "entry": "network/net", "username": "operator"}


def test_pass_credential_requires_entry_and_username() -> None:
    spec = CredentialSpec.model_validate(_pass_credentials())
    assert spec.entry == "network/net"
    assert spec.username == "operator"
    with pytest.raises(ValidationError):
        CredentialSpec.model_validate({"backend": "pass"})
    with pytest.raises(ValidationError):
        CredentialSpec.model_validate({"backend": "pass", "entry": "a/b"})


def test_ssh_key_credential_expands_home_and_rejects_entry() -> None:
    spec = CredentialSpec.model_validate(
        {
            "backend": "ssh_key",
            "key_file": "~/.ssh/id_ed25519",
            "username": "operator",
        }
    )
    assert spec.key_file == Path("~/.ssh/id_ed25519").expanduser()

    with pytest.raises(ValidationError):
        CredentialSpec.model_validate(
            {"backend": "ssh_key", "entry": "not-allowed", "username": "operator"}
        )


def test_plaintext_credential_requires_the_password_field() -> None:
    spec = CredentialSpec.model_validate(
        {"backend": "plaintext", "username": "operator", "password": "hunter2"}
    )
    assert spec.password is not None
    assert spec.password.get_secret_value() == "hunter2"
    with pytest.raises(ValidationError):
        CredentialSpec.model_validate(
            {"backend": "plaintext", "username": "operator"}
        )


def test_credential_spec_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CredentialSpec.model_validate(
            {"entry": "a/b", "username": "operator", "secret": "x"}
        )


def test_open_spec_defaults_to_direct_ssh_strict() -> None:
    spec = OpenSpec.model_validate(
        {"host": "192.0.2.1", "credentials": _pass_credentials()}
    )
    assert spec.protocol == "ssh"
    assert spec.port is None
    assert spec.route is None
    assert spec.host_key_policy == "strict"
    assert spec.legacy is None
    assert spec.allow_telnet is False
    assert spec.allow_plaintext_password is False
    assert spec.allow_serial is False


def test_open_spec_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "credentials": _pass_credentials(),
                "platform": "ios",
            }
        )


def test_open_spec_telnet_requires_allow_flag() -> None:
    with pytest.raises(ValidationError, match="allow_telnet"):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "protocol": "telnet",
                "credentials": _pass_credentials(),
            }
        )

    spec = OpenSpec.model_validate(
        {
            "host": "192.0.2.1",
            "protocol": "telnet",
            "credentials": _pass_credentials(),
            "allow_telnet": True,
        }
    )
    assert spec.allow_telnet is True


def test_open_spec_console_requires_an_explicit_port() -> None:
    with pytest.raises(ValidationError, match="requires an explicit port"):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "protocol": "console",
                "credentials": _pass_credentials(),
                "allow_telnet": True,
            }
        )


def test_open_spec_plaintext_requires_the_flag() -> None:
    with pytest.raises(ValidationError, match="allow_plaintext_password"):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "credentials": {
                    "backend": "plaintext",
                    "username": "operator",
                    "password": "hunter2",
                },
            }
        )


def test_open_spec_legacy_applies_to_ssh_only() -> None:
    with pytest.raises(ValidationError, match="SSH connections only"):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "protocol": "telnet",
                "credentials": _pass_credentials(),
                "allow_telnet": True,
                "legacy": {"ciphers": ["aes128-cbc"]},
            }
        )
    with pytest.raises(ValidationError, match="at least one category"):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "credentials": _pass_credentials(),
                "legacy": {},
            }
        )


def test_route_discriminated_union() -> None:
    spec = OpenSpec.model_validate(
        {
            "host": "192.0.2.1",
            "credentials": _pass_credentials(),
            "route": {"type": "socks", "host": "127.0.0.1", "port": 1080},
        }
    )
    assert spec.route is not None
    assert spec.route.type == "socks"

    for bad_type in ("warp", "nested"):
        with pytest.raises(ValidationError):
            OpenSpec.model_validate(
                {
                    "host": "192.0.2.1",
                    "credentials": _pass_credentials(),
                    "route": {"type": bad_type, "host": "127.0.0.1"},
                }
            )


def test_proxyjump_route_requires_credentials() -> None:
    with pytest.raises(ValidationError):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "credentials": _pass_credentials(),
                "route": {"type": "proxyjump", "host": "192.0.2.254"},
            }
        )


def test_serial_spec_requires_dev_path_and_flag() -> None:
    spec = OpenSpec.model_validate(
        {
            "host": "/dev/ttyUSB0",
            "protocol": "serial",
            "allow_serial": True,
            "serial": {"baudrate": 115200, "parity": "E", "stopbits": 1.5},
        }
    )
    assert spec.credentials is None
    assert spec.serial.baudrate == 115200

    with pytest.raises(ValidationError, match="allow_serial"):
        OpenSpec.model_validate(
            {"host": "/dev/ttyUSB0", "protocol": "serial"}
        )
    with pytest.raises(ValidationError, match="absolute /dev/"):
        OpenSpec.model_validate(
            {"host": "ttyUSB0", "protocol": "serial", "allow_serial": True}
        )
    with pytest.raises(ValidationError, match="does not support a route"):
        OpenSpec.model_validate(
            {
                "host": "/dev/ttyUSB0",
                "protocol": "serial",
                "allow_serial": True,
                "route": {"type": "socks", "host": "127.0.0.1", "port": 1080},
            }
        )
    with pytest.raises(ValidationError, match="stopbits"):
        OpenSpec.model_validate(
            {
                "host": "/dev/ttyUSB0",
                "protocol": "serial",
                "allow_serial": True,
                "serial": {"stopbits": 3},
            }
        )


def test_non_serial_specs_require_credentials() -> None:
    with pytest.raises(ValidationError, match="requires credentials"):
        OpenSpec.model_validate({"host": "192.0.2.1"})


def test_socks_route_rejects_telnet_targets() -> None:
    with pytest.raises(ValidationError, match="SSH targets only"):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "protocol": "telnet",
                "allow_telnet": True,
                "credentials": _pass_credentials(),
                "route": {"type": "socks", "host": "127.0.0.1", "port": 1080},
            }
        )


def test_nested_route_is_rejected_as_unknown() -> None:
    with pytest.raises(ValidationError):
        OpenSpec.model_validate(
            {
                "host": "192.0.2.1",
                "credentials": _pass_credentials(),
                "route": {
                    "type": "nested",
                    "host": "192.0.2.254",
                    "credentials": _pass_credentials(),
                },
            }
        )


def test_runtime_expands_user_home() -> None:
    runtime = RuntimeConfig.model_validate({"audit_file": "~/x/audit.jsonl"})
    assert str(runtime.audit_file) == str(Path("~/x/audit.jsonl").expanduser())
    assert runtime.session_idle_timeout == 3600
    assert runtime.session_max_lifetime == 86400
    assert runtime.io_timeout == 60
    assert runtime.max_read_timeout == 300
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"max_read_timeout": 0})


def test_runtime_default_paths_are_absolute() -> None:
    runtime = RuntimeConfig()
    for path in (runtime.audit_file, runtime.output_dir, runtime.known_hosts_file):
        assert path.is_absolute(), path
        assert "~" not in path.parts, path


def test_policy_defaults_allow_local_features() -> None:
    policy = PolicyConfig()
    assert policy.defaults.allow_telnet is True
    assert policy.defaults.allow_serial is True
    assert policy.defaults.allow_plaintext_password is True
    assert policy.defaults.allow_legacy_algorithms is True


def test_load_missing_policy_yields_defaults(config_dir: Path) -> None:
    config = load_config(config_dir)
    assert config.policy.defaults.allow_telnet is True
    assert config.policy.runtime.max_open_sessions == 10


def test_load_policy_file(config_dir: Path) -> None:
    _write(
        config_dir,
        "policy.yml",
        {
            "defaults": {"allow_telnet": False, "allow_serial": False},
            "runtime": {"session_idle_timeout": 60},
        },
    )

    config = load_config(config_dir)
    assert config.policy.defaults.allow_telnet is False
    assert config.policy.defaults.allow_serial is False
    assert config.policy.runtime.session_idle_timeout == 60


def test_load_raises_on_broken_yaml(config_dir: Path) -> None:
    (config_dir / "policy.yml").write_text("::: not yaml :::", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(config_dir)


def test_load_rejects_unknown_policy_fields(config_dir: Path) -> None:
    _write(config_dir, "policy.yml", {"defaults": {"typo": True}})
    with pytest.raises(ConfigError, match="invalid policy.yml"):
        load_config(config_dir)
