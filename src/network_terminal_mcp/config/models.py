"""Pydantic models for the four configuration files.

The schema mirrors the example files under ``config/``. Any field whose name or
meaning is not obvious from the YAML is documented here and must stay in sync
with ``docs/configuration.md``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Action = Literal["allow", "ask", "deny"]
Protocol = Literal["ssh", "telnet", "legacy_ssh"]
HostKeyPolicy = Literal["strict", "accept_new", "accept_changed"]


class StrictModel(BaseModel):
    """Reject unknown fields so typos fail loudly during validation."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# inventory.yml
# ---------------------------------------------------------------------------


class Device(StrictModel):
    host: str
    platform: str
    credentials: str
    connection: str
    tags: list[str] = Field(default_factory=list)
    allow_telnet: bool = False
    port: int | None = None


class Group(StrictModel):
    tags: list[str] = Field(default_factory=list)


class InventoryConfig(StrictModel):
    devices: dict[str, Device] = Field(default_factory=dict)
    groups: dict[str, Group] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# connections.yml
# ---------------------------------------------------------------------------


class DirectConnection(StrictModel):
    type: Literal["direct"] = "direct"
    protocol: Protocol = "ssh"
    host_key_policy: HostKeyPolicy = "strict"
    port: int | None = None
    host_key_algorithms: list[str] | None = None
    kex_algorithms: list[str] | None = None
    ciphers: list[str] | None = None


class ProxyJumpConnection(StrictModel):
    type: Literal["proxyjump"] = "proxyjump"
    protocol: Literal["ssh"] = "ssh"
    jump_host: str
    jump_credentials: str
    jump_port: int = 22
    host_key_policy: HostKeyPolicy = "strict"
    jump_host_key_policy: HostKeyPolicy = "strict"
    port: int | None = None


class NestedConnection(StrictModel):
    type: Literal["nested"] = "nested"
    host: str
    protocol: Protocol = "ssh"
    credentials: str
    next_protocol: Literal["ssh", "telnet"] = "ssh"
    host_key_policy: HostKeyPolicy = "strict"
    port: int | None = None
    next_port: int | None = None


class ConsoleConnection(StrictModel):
    type: Literal["console"] = "console"
    connect_command: str | None = None
    port: int | None = None

    @model_validator(mode="after")
    def _validate_console(self) -> ConsoleConnection:
        if self.connect_command is not None:
            raise ValueError(
                "console connect_command is not supported; use a TCP console port"
            )
        if self.port is None:
            raise ValueError("console connection requires a port")
        return self


ConnectionProfile = Annotated[
    DirectConnection | ProxyJumpConnection | NestedConnection | ConsoleConnection,
    Field(discriminator="type"),
]


class PlatformAlias(StrictModel):
    driver: str
    dialect: str | None = None
    cli_help_requires_enter: bool = False
    telnet_driver: str | None = None


class ConnectionsConfig(StrictModel):
    connections: dict[str, ConnectionProfile] = Field(default_factory=dict)
    platforms: dict[str, PlatformAlias] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# credentials.yml
# ---------------------------------------------------------------------------


class CredentialProfile(StrictModel):
    backend: Literal["pass", "ssh_key"] = "pass"
    entry: str | None = None
    username: str
    key_file: Path | None = None
    key_passphrase_entry: str | None = None

    @field_validator("key_file", mode="before")
    @classmethod
    def _expand_key_file(cls, value: object) -> object:
        if isinstance(value, str):
            return os.path.expanduser(value)
        return value

    @model_validator(mode="after")
    def _validate_backend_fields(self) -> CredentialProfile:
        if self.backend == "pass":
            if not self.entry:
                raise ValueError("pass credential requires entry")
            if self.key_file is not None:
                raise ValueError("pass credential cannot define key_file")
            if self.key_passphrase_entry is not None:
                raise ValueError("pass credential cannot define key_passphrase_entry")
        else:
            if self.key_file is None:
                raise ValueError("ssh_key credential requires key_file")
            if self.entry is not None:
                raise ValueError("ssh_key credential cannot define entry")
        return self


class CredentialsConfig(StrictModel):
    credentials: dict[str, CredentialProfile] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# policy.yml
# ---------------------------------------------------------------------------


class PolicyDefaults(StrictModel):
    cli_help: Action = "allow"
    unknown_exec_command: Action = "ask"
    config_mode: Action = "ask"
    raw_input: Action = "deny"
    telnet: Action = "deny"


class PolicyRule(StrictModel):
    id: str
    action: Action
    command_patterns: list[str] = Field(default_factory=list)


class RuntimeConfig(StrictModel):
    session_idle_timeout: int = 300
    session_max_lifetime: int = 1800
    command_timeout: int = 60
    cli_help_timeout: int = Field(default=5, ge=1)
    max_inline_output_bytes: int = 65536
    max_session_buffer_bytes: int = 1048576
    max_open_sessions: int = 10
    max_pager_pages: int = Field(default=32, ge=1)
    audit_file: Path = Path("~/.local/state/network-terminal-mcp/audit.jsonl")
    output_dir: Path = Path("~/.local/state/network-terminal-mcp/outputs")
    known_hosts_file: Path = Path("~/.local/state/network-terminal-mcp/known_hosts")
    transcripts_enabled: bool = False

    @field_validator("audit_file", "output_dir", "known_hosts_file", mode="before")
    @classmethod
    def _expand_user(cls, value: object) -> object:
        if isinstance(value, str):
            return os.path.expanduser(value)
        return value


class PolicyConfig(StrictModel):
    defaults: PolicyDefaults = Field(default_factory=PolicyDefaults)
    rules: list[PolicyRule] = Field(default_factory=list)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
