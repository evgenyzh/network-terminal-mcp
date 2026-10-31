"""Parse the single ``open_session(command)`` connection syntax.

The command is split like a POSIX shell command line, but no shell is ever
used. SSH and Telnet arguments are passed to the real clients unchanged; the
short ``serial DEVICE [BAUDRATE]`` form is handled by pyserial.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from typing import Literal, cast

from network_terminal_mcp.errors import CommandError

CommandKind = Literal["ssh", "telnet", "serial"]

DEFAULT_SERIAL_BAUDRATE = 9600
MAX_SERIAL_BAUDRATE = 4_000_000


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """A validated connection command ready for a terminal factory."""

    kind: CommandKind
    command: str
    argv: tuple[str, ...] = ()
    device: str | None = None
    baudrate: int | None = None

    @property
    def is_serial(self) -> bool:
        return self.kind == "serial"


def parse_command(command: str) -> ParsedCommand:
    """Split and validate a connection command without invoking a shell."""
    if not isinstance(command, str):
        raise CommandError("connection command must be a string")
    text = command.strip()
    if not text:
        raise CommandError("connection command is empty")
    try:
        argv = shlex.split(text, posix=True)
    except ValueError as exc:
        raise CommandError(f"cannot parse connection command: {exc}") from exc
    if not argv:
        raise CommandError("connection command is empty")

    kind, arguments = argv[0], argv[1:]
    if kind == "serial":
        return _parse_serial(text, arguments)
    if kind not in ("ssh", "telnet"):
        raise CommandError(
            f"unsupported command {kind!r}; expected ssh, telnet, or serial"
        )
    if not arguments:
        raise CommandError(
            f"{kind} requires connection arguments, for example "
            f"{kind} operator@192.0.2.10"
        )
    executable = shutil.which(kind)
    if executable is None:
        raise CommandError(
            f"{kind} executable was not found on PATH",
            code="executable_not_found",
        )
    return ParsedCommand(
        kind=cast(CommandKind, kind),
        command=text,
        argv=(executable, *arguments),
    )


def _parse_serial(command: str, arguments: list[str]) -> ParsedCommand:
    if not arguments:
        raise CommandError("serial requires a device path, for example serial /dev/ttyUSB0")
    if len(arguments) > 2:
        raise CommandError("serial accepts only DEVICE [BAUDRATE]")
    device = arguments[0]
    if not device:
        raise CommandError("serial device path is empty")
    baudrate = DEFAULT_SERIAL_BAUDRATE
    if len(arguments) == 2:
        try:
            baudrate = int(arguments[1], 10)
        except ValueError as exc:
            raise CommandError(
                f"serial baudrate must be an integer, got {arguments[1]!r}"
            ) from exc
        if baudrate <= 0 or baudrate > MAX_SERIAL_BAUDRATE:
            raise CommandError(
                f"serial baudrate must be between 1 and {MAX_SERIAL_BAUDRATE}"
            )
    return ParsedCommand(
        kind="serial",
        command=command,
        device=os.path.expanduser(device),
        baudrate=baudrate,
    )
