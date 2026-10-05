"""Pydantic models for local policy and model-described connections.

Connection, credential, and inventory descriptions are no longer stored in
YAML files: the model supplies a validated :class:`OpenSpec` to every
``open_session`` call. Only an optional ``policy.yml`` (policy posture and
runtime limits) remains local, and it is never writable from MCP tools.

Sessions are raw interactive terminals. The model writes exactly what it
wants with ``terminal_write``; secrets typed at device or SSH prompts must go
through ``terminal_write_secret`` so their values never appear in tool
arguments, tool results, or audit records.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

Protocol = Literal["ssh", "telnet", "console", "serial"]
HostKeyPolicy = Literal["strict", "accept_new", "accept_changed"]

_PASS_ENTRY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/@+-]*")


def validate_pass_entry(value: str) -> str:
    """Return a validated ``pass`` entry name or raise ``ValueError``."""
    if _PASS_ENTRY_RE.fullmatch(value) is None or ".." in value:
        raise ValueError(
            "pass entry must be a relative store name with letters, digits, "
            "and . _ / @ + - only"
        )
    return value


class StrictModel(BaseModel):
    """Reject unknown fields so typos fail loudly during validation."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Model-described connection
# ---------------------------------------------------------------------------


class CredentialSpec(StrictModel):
    """One credential source for a target or a route hop.

    ``pass`` reads the password from a local password-store entry, ``ssh_key``
    uses an explicit local private key file, and ``plaintext`` carries a
    password in the call itself and requires an explicit insecure opt-in on
    the enclosing :class:`OpenSpec`.
    """

    backend: Literal["pass", "ssh_key", "plaintext"] = "pass"
    username: str = Field(min_length=1)
    entry: str | None = None
    key_file: Path | None = None
    key_passphrase_entry: str | None = None
    password: SecretStr | None = None

    @field_validator("key_file", mode="before")
    @classmethod
    def _expand_key_file(cls, value: object) -> object:
        if isinstance(value, str):
            return os.path.expanduser(value)
        return value

    @field_validator("entry", "key_passphrase_entry")
    @classmethod
    def _validate_entry(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_pass_entry(value)

    @model_validator(mode="after")
    def _validate_backend_fields(self) -> CredentialSpec:
        if self.backend == "pass":
            if not self.entry:
                raise ValueError("pass credential requires entry")
            if self.key_file is not None:
                raise ValueError("pass credential cannot define key_file")
            if self.key_passphrase_entry is not None:
                raise ValueError("pass credential cannot define key_passphrase_entry")
            if self.password is not None:
                raise ValueError("pass credential cannot define password")
        elif self.backend == "ssh_key":
            if self.key_file is None:
                raise ValueError("ssh_key credential requires key_file")
            if self.entry is not None:
                raise ValueError("ssh_key credential cannot define entry")
            if self.password is not None:
                raise ValueError("ssh_key credential cannot define password")
        else:
            if self.password is None:
                raise ValueError("plaintext credential requires password")
            if self.entry is not None or self.key_file is not None:
                raise ValueError(
                    "plaintext credential cannot define entry or key_file"
                )
            if self.key_passphrase_entry is not None:
                raise ValueError(
                    "plaintext credential cannot define key_passphrase_entry"
                )
        return self


class LegacyAlgorithms(StrictModel):
    """Per-call allowlists for legacy SSH algorithms on an explicit host.

    Only the listed categories are restricted; the remaining categories keep
    Paramiko's defaults. Host key checking is never disabled.
    """

    host_key_algorithms: list[str] | None = None
    kex_algorithms: list[str] | None = None
    ciphers: list[str] | None = None

    @model_validator(mode="after")
    def _require_one_category(self) -> LegacyAlgorithms:
        if (
            self.host_key_algorithms is None
            and self.kex_algorithms is None
            and self.ciphers is None
        ):
            raise ValueError("legacy algorithms require at least one category")
        return self


class SocksRoute(StrictModel):
    """Reach the final target through a local SOCKS5 proxy."""

    type: Literal["socks"] = "socks"
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)


class ProxyJumpRoute(StrictModel):
    """Reach the final target through one SSH-only jump host."""

    type: Literal["proxyjump"] = "proxyjump"
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    credentials: CredentialSpec
    host_key_policy: HostKeyPolicy = "strict"


RouteSpec = Annotated[
    SocksRoute | ProxyJumpRoute,
    Field(discriminator="type"),
]


class SerialParams(StrictModel):
    """Local serial console parameters for ``/dev/tty*`` transports."""

    baudrate: int = Field(default=9600, ge=1, le=4_000_000)
    bytesize: Literal[5, 6, 7, 8] = 8
    parity: Literal["N", "E", "O", "M", "S"] = "N"
    stopbits: float = 1

    @field_validator("stopbits")
    @classmethod
    def _validate_stopbits(cls, value: float) -> float:
        if value not in (1, 1.5, 2):
            raise ValueError("stopbits must be 1, 1.5 or 2")
        return value


class OpenSpec(StrictModel):
    """A fully described, secret-free connection request for ``open_session``."""

    host: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Protocol = "ssh"
    credentials: CredentialSpec | None = None
    route: RouteSpec | None = None
    host_key_policy: HostKeyPolicy = "strict"
    legacy: LegacyAlgorithms | None = None
    allow_telnet: bool = False
    allow_plaintext_password: bool = False
    allow_serial: bool = False
    serial: SerialParams = Field(default_factory=SerialParams)

    @model_validator(mode="after")
    def _validate_request(self) -> OpenSpec:
        if self.protocol == "serial":
            if not self.allow_serial:
                raise ValueError("serial protocol requires allow_serial=true")
            if not self.host.startswith("/dev/"):
                raise ValueError("serial host must be an absolute /dev/ device path")
            if self.port is not None:
                raise ValueError("serial protocol does not use a TCP port")
            if self.route is not None:
                raise ValueError("serial protocol does not support a route")
            if self.legacy is not None:
                raise ValueError("legacy algorithms apply to SSH connections only")
            return self

        if self.credentials is None:
            raise ValueError(
                f"{self.protocol} protocol requires credentials"
            )
        if self.protocol == "console" and self.port is None:
            raise ValueError("console protocol requires an explicit port")
        if self.protocol == "console" and self.route is not None:
            raise ValueError("console protocol does not support a route")
        if self.protocol != "ssh" and self.legacy is not None:
            raise ValueError("legacy algorithms apply to SSH connections only")
        if isinstance(self.route, (SocksRoute, ProxyJumpRoute)) and self.protocol != "ssh":
            raise ValueError("SOCKS and proxyjump routes support SSH targets only")
        if self.protocol in ("telnet", "console") and not self.allow_telnet:
            raise ValueError(
                f"{self.protocol} requires allow_telnet=true on this call"
            )
        if (
            self.credentials.backend == "plaintext"
            and not self.allow_plaintext_password
        ):
            raise ValueError(
                "plaintext credential requires allow_plaintext_password=true"
            )
        return self


# ---------------------------------------------------------------------------
# policy.yml (optional local file)
# ---------------------------------------------------------------------------


class PolicyDefaults(StrictModel):
    allow_telnet: bool = True
    allow_plaintext_password: bool = True
    allow_legacy_algorithms: bool = True
    allow_serial: bool = True


class RuntimeConfig(StrictModel):
    session_idle_timeout: int = Field(default=3600, ge=1)
    session_max_lifetime: int = Field(default=86400, ge=1)
    io_timeout: int = Field(default=60, ge=1)
    max_read_timeout: int = Field(default=300, ge=1)
    max_write_bytes: int = Field(default=65536, ge=1)
    max_inline_output_bytes: int = Field(default=65536, ge=1)
    max_session_buffer_bytes: int = Field(default=1048576, ge=1)
    max_open_sessions: int = Field(default=10, ge=1)
    audit_file: Path = Field(
        default_factory=lambda: Path.home()
        / ".local/state/network-terminal-mcp/audit.jsonl"
    )
    output_dir: Path = Field(
        default_factory=lambda: Path.home()
        / ".local/state/network-terminal-mcp/outputs"
    )
    known_hosts_file: Path = Field(
        default_factory=lambda: Path.home()
        / ".local/state/network-terminal-mcp/known_hosts"
    )
    transcripts_enabled: bool = False

    @field_validator("audit_file", "output_dir", "known_hosts_file", mode="before")
    @classmethod
    def _expand_user(cls, value: object) -> object:
        if isinstance(value, str):
            return os.path.expanduser(value)
        return value


class PolicyConfig(StrictModel):
    defaults: PolicyDefaults = Field(default_factory=PolicyDefaults)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
