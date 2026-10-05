"""Connection plans derived from model-described ``open_session`` specs.

The plan normalizes defaults (ports, warnings, serial parameters) before any
network operation and stays free of secrets: credentials are still references
resolved later by the session manager.
"""

from __future__ import annotations

from dataclasses import dataclass

from network_terminal_mcp.config.models import (
    CredentialSpec,
    HostKeyPolicy,
    LegacyAlgorithms,
    OpenSpec,
    Protocol,
    ProxyJumpRoute,
    SocksRoute,
)

Route = SocksRoute | ProxyJumpRoute


@dataclass(frozen=True)
class ConnectionPlan:
    """A validated connection request with defaults applied."""

    host: str
    port: int | None
    protocol: Protocol
    credentials: CredentialSpec | None
    route: Route | None
    host_key_policy: HostKeyPolicy
    legacy: LegacyAlgorithms | None
    allow_telnet: bool
    allow_plaintext_password: bool
    allow_serial: bool
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    warnings: tuple[str, ...] = ()


def default_port(protocol: Protocol) -> int:
    return 23 if protocol == "telnet" else 22


def build_plan(spec: OpenSpec) -> ConnectionPlan:
    """Normalize a validated spec into a connection plan with warnings."""
    if spec.protocol == "serial":
        port: int | None = None
    elif spec.protocol == "console":
        # The schema requires an explicit console port.
        assert spec.port is not None
        port = spec.port
    else:
        port = spec.port or default_port(spec.protocol)
    return ConnectionPlan(
        host=spec.host,
        port=port,
        protocol=spec.protocol,
        credentials=spec.credentials,
        route=spec.route,
        host_key_policy=spec.host_key_policy,
        legacy=spec.legacy,
        allow_telnet=spec.allow_telnet,
        allow_plaintext_password=spec.allow_plaintext_password,
        allow_serial=spec.allow_serial,
        baudrate=spec.serial.baudrate,
        bytesize=spec.serial.bytesize,
        parity=spec.serial.parity,
        stopbits=spec.serial.stopbits,
        warnings=tuple(_plan_warnings(spec)),
    )


def route_label(plan: ConnectionPlan) -> str:
    """Return the audit/session label for the plan's route."""
    if isinstance(plan.route, SocksRoute):
        return "socks"
    if isinstance(plan.route, ProxyJumpRoute):
        return "proxyjump"
    if plan.protocol == "telnet":
        return "telnet"
    if plan.protocol == "console":
        return "console"
    if plan.protocol == "serial":
        return "serial"
    return "direct"


def _plan_warnings(spec: OpenSpec) -> list[str]:
    warnings: list[str] = []
    if spec.credentials is not None and spec.credentials.backend == "plaintext":
        warnings.append(
            "plaintext password supplied in this call; the server does not "
            "store it, but the client conversation may retain it"
        )
    if spec.protocol in ("telnet", "console"):
        warnings.append("Telnet/console transmits credentials and traffic in cleartext")
    if spec.protocol == "serial":
        warnings.append(
            "local serial console: no authentication, no transport security, "
            "and the device is fully controlled by this process"
        )
    if isinstance(spec.route, SocksRoute):
        warnings.append(
            "target reached through a local SOCKS proxy; verify trust in the tunnel"
        )
    if spec.legacy is not None:
        warnings.append(
            f"explicit legacy SSH algorithm overrides in use for {spec.host}"
        )
    return warnings
