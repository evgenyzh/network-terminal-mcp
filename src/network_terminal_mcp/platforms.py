"""Mapping from project platform names to Netmiko device types."""

from __future__ import annotations

from dataclasses import dataclass

from netmiko.ssh_dispatcher import CLASS_MAPPER

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

# Netmiko names Telnet drivers as ``<driver>_telnet`` for most platforms, but a
# few stock drivers use a different Telnet class name. Kept local to the
# platform registry so the mapping stays explicit.
_TELNET_DRIVER_SUFFIX = "_telnet"


@dataclass(frozen=True)
class Platform:
    """A resolved Netmiko driver and descriptive CLI dialect."""

    name: str
    driver: str
    dialect: str
    cli_help_requires_enter: bool = False
    telnet_driver: str | None = None


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
            telnet_driver=self._resolve_telnet_driver(alias, driver),
        )

    @staticmethod
    def _resolve_telnet_driver(alias: object, driver: str) -> str | None:
        explicit = getattr(alias, "telnet_driver", None)
        if explicit is not None:
            candidate = str(explicit)
        else:
            candidate = f"{driver}{_TELNET_DRIVER_SUFFIX}"
        if candidate not in CLASS_MAPPER:
            return None
        return candidate
