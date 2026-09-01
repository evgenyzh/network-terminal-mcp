"""Device target resolution.

A target is either a named inventory device or an ad-hoc description supplied
directly to ``open_session``. Ad-hoc targets may not carry secrets — passwords
always come from the configured credential profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from network_terminal_mcp.config.loader import AppConfig
from network_terminal_mcp.config.models import Device
from network_terminal_mcp.errors import TargetError


@dataclass(frozen=True)
class Target:
    """A fully resolved device target, ready for connection."""

    name: str
    host: str
    credentials: str
    connection: str
    port: int | None = None
    allow_telnet: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


class TargetResolver:
    """Resolve named and ad-hoc targets against an :class:`AppConfig`."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def resolve(self, name: str | None = None, **ad_hoc: object) -> Target:
        """Resolve a target by inventory name or from ad-hoc keyword fields.

        Exactly one of ``name`` or ``host`` (plus ``credentials`` and
        ``connection``) must be provided.
        """
        if name is not None:
            return self._resolve_named(name)
        return self._resolve_ad_hoc(ad_hoc)

    def _resolve_named(self, name: str) -> Target:
        device = self._config.inventory.devices.get(name)
        if device is None:
            raise TargetError(f"unknown device {name!r} in inventory")
        return self._from_device(name, device)

    def _resolve_ad_hoc(self, fields: dict[str, object]) -> Target:
        unknown = set(fields) - {"host", "credentials", "connection", "port"}
        if unknown:
            raise TargetError(
                f"unsupported ad-hoc target fields: {', '.join(sorted(unknown))}"
            )
        host = fields.get("host")
        if not isinstance(host, str) or not host:
            raise TargetError("ad-hoc target requires 'host'")
        credentials = fields.get("credentials")
        connection = fields.get("connection")
        if credentials is None:
            credentials = self._config.inventory.default_credentials
        if connection is None:
            connection = self._config.inventory.default_connection
        if not isinstance(credentials, str) or not credentials:
            raise TargetError("ad-hoc target requires 'credentials' (or a configured default)")
        if not isinstance(connection, str) or not connection:
            raise TargetError("ad-hoc target requires 'connection' (or a configured default)")
        port = fields.get("port")
        if port is not None and not isinstance(port, int):
            raise TargetError("ad-hoc target 'port' must be an integer")

        # Profiles must exist locally; never accept inline secrets or commands.
        if credentials not in self._config.credentials.credentials:
            raise TargetError(
                f"ad-hoc target references unknown credential profile "
                f"{credentials!r}"
            )
        if connection not in self._config.connections.connections:
            raise TargetError(
                f"ad-hoc target references unknown connection profile "
                f"{connection!r}"
            )
        return Target(
            name="<ad-hoc>",
            host=host,
            credentials=credentials,
            connection=connection,
            port=port,
        )

    @staticmethod
    def _from_device(name: str, device: Device) -> Target:
        return Target(
            name=name,
            host=device.host,
            credentials=device.credentials,
            connection=device.connection,
            port=device.port,
            allow_telnet=device.allow_telnet,
            tags=tuple(device.tags),
        )
