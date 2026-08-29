"""Mapping from project platform names to Netmiko device types."""

from __future__ import annotations

from dataclasses import dataclass

from network_terminal_mcp.config.models import ConnectionsConfig
from network_terminal_mcp.errors import TransportError

_STOCK_DRIVERS = frozenset(
    {
        "cisco_ios",
        "dlink_ds",
        "eltex",
        "eltex_esr",
        "huawei_olt",
        "huawei_vrp",
        "juniper_junos",
        "mikrotik_routeros",
    }
)


@dataclass(frozen=True)
class Platform:
    """A resolved Netmiko driver and descriptive CLI dialect."""

    name: str
    driver: str
    dialect: str
    cli_help_requires_enter: bool = False


class PlatformRegistry:
    """Resolve stock Netmiko platforms and locally configured aliases."""

    def __init__(self, connections: ConnectionsConfig) -> None:
        self._aliases = connections.platforms

    def resolve(self, name: str) -> Platform:
        """Return a supported stock Netmiko driver for ``name``."""
        alias = self._aliases.get(name)
        driver = alias.driver if alias is not None else name
        dialect = alias.dialect if alias is not None and alias.dialect else name
        if driver.startswith("local:"):
            raise TransportError(
                f"platform {name!r} requires local adapter {driver!r}, "
                "which is not implemented yet"
            )
        if driver not in _STOCK_DRIVERS:
            raise TransportError(
                f"unsupported platform {name!r}: Netmiko driver {driver!r} is unavailable"
            )
        return Platform(
            name=name,
            driver=driver,
            dialect=dialect,
            cli_help_requires_enter=(
                bool(alias.cli_help_requires_enter) if alias is not None else False
            ),
        )
