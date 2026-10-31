"""Terminal transports: real SSH/Telnet clients in a PTY and local serial.

The manager only needs four operations: write text, read until quiet or a time
budget expires, inspect process liveness, and close. Command knowledge and
device dialects belong to the model, not to this module.
"""

from __future__ import annotations

import codecs
import errno
import os
import select
import stat
import time
from typing import Any, Protocol

import ptyprocess

from network_terminal_mcp.errors import TransportError

_READ_CHUNK = 4096
_POLL_SECONDS = 0.02


class TerminalConnection(Protocol):
    """Small synchronous terminal API required by the session manager."""

    def write_channel(self, out_data: str) -> None: ...

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str: ...

    def is_alive(self) -> bool: ...

    @property
    def exit_code(self) -> int | None: ...

    def disconnect(self) -> None: ...


class _Decoder:
    """Incrementally decode UTF-8 so split multibyte characters are safe."""

    __slots__ = ("_decoder",)

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def decode(self, data: bytes, *, final: bool = False) -> str:
        return self._decoder.decode(data, final=final)


class PtyTerminal:
    """A real ssh/telnet process attached to a POSIX pseudo-terminal."""

    def __init__(self) -> None:
        self._process: ptyprocess.PtyProcess | None = None
        self._decoder = _Decoder()
        self._eof = False
        self._closed = False

    def start(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """Spawn a client command in a new PTY."""
        if not argv:
            raise TransportError("cannot start an empty terminal command")
        try:
            self._process = ptyprocess.PtyProcess.spawn(
                list(argv),
                env=dict(os.environ) if env is None else env,
                echo=False,
                dimensions=(24, 120),
            )
        except FileNotFoundError as exc:
            raise TransportError(
                f"cannot execute {argv[0]}: executable was not found"
            ) from exc
        except OSError as exc:
            raise TransportError(f"cannot execute {argv[0]}: {exc}") from exc

    def write_channel(self, out_data: str) -> None:
        if self._closed or self._process is None:
            raise TransportError("terminal process is not running")
        try:
            self._process.write(out_data.encode("utf-8"))
        except (EOFError, OSError) as exc:
            raise TransportError(f"terminal write failed: {exc}") from exc

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str:
        """Read until output has been quiet for ``last_read`` or time expires."""
        if self._process is None or self._closed:
            return ""
        if read_timeout <= 0:
            raise TransportError("terminal read timeout must be positive")

        output: list[str] = []
        deadline = time.monotonic() + read_timeout
        last_data_at: float | None = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if last_data_at is not None and now - last_data_at >= last_read:
                break
            chunk = self._read_once(min(_POLL_SECONDS, deadline - now))
            if chunk:
                output.append(self._decoder.decode(chunk))
                last_data_at = time.monotonic()
                continue
            if self._eof:
                break
            time.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        return "".join(output)

    def _read_once(self, timeout: float) -> bytes:
        process = self._process
        if process is None or self._closed or process.fd < 0:
            self._eof = True
            return b""
        try:
            ready, _, _ = select.select([process.fd], [], [], max(timeout, 0.0))
        except (OSError, ValueError):
            self._eof = True
            return b""
        if not ready:
            return b""
        try:
            data = os.read(process.fd, _READ_CHUNK)
        except OSError as exc:
            if exc.errno == errno.EIO:
                self._eof = True
                return b""
            raise TransportError(f"terminal read failed: {exc}") from exc
        if not data:
            self._eof = True
            return b""
        return data

    def is_alive(self) -> bool:
        if self._process is None or self._closed:
            return False
        try:
            # ptyprocess switches to a blocking waitpid() after EOF. A client
            # can close its terminal while staying alive, so avoid hanging the
            # manager thread merely to inspect status.
            if self._eof:
                self._process.flag_eof = False
            return bool(self._process.isalive())
        except ptyprocess.PtyProcessError:
            self._eof = True
            return False

    @property
    def exit_code(self) -> int | None:
        process = self._process
        if process is None:
            return None
        if self.is_alive():
            return None
        if process.exitstatus is not None:
            return int(process.exitstatus)
        if process.signalstatus is not None:
            return 128 + int(process.signalstatus)
        return None

    def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if self._eof:
                process.flag_eof = False
            process.close(force=True)
        except (OSError, ptyprocess.PtyProcessError):
            pass
        finally:
            self._eof = True


class SerialTerminal:
    """Local serial console backed by pyserial."""

    def __init__(self) -> None:
        self._serial: Any = None
        self._decoder = _Decoder()
        self._closed = False

    def open(
        self,
        *,
        device: str,
        baudrate: int,
        read_timeout: float = 0.1,
        write_timeout: float = 10.0,
    ) -> None:
        self._validate_device(device)
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise TransportError(
                "pyserial is required for serial sessions; install the package"
            ) from exc
        try:
            self._serial = serial.Serial(
                port=device,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=read_timeout,
                write_timeout=write_timeout,
            )
        except Exception as exc:
            self._serial = None
            raise TransportError(f"serial open failed for {device}: {exc}") from exc

    @staticmethod
    def _validate_device(device: str) -> None:
        try:
            mode = os.stat(device).st_mode
        except OSError as exc:
            raise TransportError(f"serial device {device} is not accessible: {exc}") from exc
        if not stat.S_ISCHR(mode):
            raise TransportError(f"serial device {device} is not a character device")

    def write_channel(self, out_data: str) -> None:
        if self._closed or self._serial is None:
            raise TransportError("serial terminal is not open")
        try:
            self._serial.write(out_data.encode("utf-8"))
            self._serial.flush()
        except Exception as exc:
            raise TransportError(f"serial write failed: {exc}") from exc

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str:
        serial_port = self._serial
        if serial_port is None or self._closed:
            return ""
        if read_timeout <= 0:
            raise TransportError("terminal read timeout must be positive")

        output: list[str] = []
        deadline = time.monotonic() + read_timeout
        last_data_at: float | None = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if last_data_at is not None and now - last_data_at >= last_read:
                break
            try:
                waiting = int(serial_port.in_waiting)
                data = serial_port.read(waiting or 1)
            except Exception as exc:
                raise TransportError(f"serial read failed: {exc}") from exc
            if data:
                output.append(self._decoder.decode(bytes(data)))
                last_data_at = time.monotonic()
            else:
                time.sleep(min(_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        return "".join(output)

    def is_alive(self) -> bool:
        return not self._closed and self._serial is not None and bool(self._serial.is_open)

    @property
    def exit_code(self) -> int | None:
        return None

    def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
