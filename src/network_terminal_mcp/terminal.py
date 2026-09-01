"""Raw SSH/Telnet terminal transport without a driver abstraction.

The session manager needs a small synchronous terminal API: write bytes, read
until a regex matches or a time budget expires, and find the current prompt.
This module implements that API directly on top of Paramiko (SSH) and
telnetlib3 (Telnet and console). No vendor drivers are involved; the model
interprets device output itself.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any, Protocol

import paramiko

from network_terminal_mcp.errors import TransportError

_READ_CHUNK = 4096
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class TerminalConnection(Protocol):
    """Small synchronous terminal API required by the session manager."""

    def find_prompt(self) -> str: ...

    def send_command(
        self,
        command_string: str,
        *,
        expect_string: str | None = None,
        read_timeout: float,
        strip_prompt: bool = True,
        strip_command: bool = True,
        cmd_verify: bool = True,
    ) -> str: ...

    def write_channel(self, out_data: str) -> None: ...

    def read_until_pattern(self, pattern: str, *, read_timeout: float) -> str: ...

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str: ...

    def disconnect(self) -> None: ...


class _BaseTerminal:
    """Shared read-loop and prompt helpers.

    Subclasses provide ``_read_channel()`` returning the next chunk of decoded
    terminal text (possibly empty).
    """

    _buffer = ""
    _initial_delay = 0.25
    _retry_delay = 0.5
    _prompt_retries = 12

    def _read_channel(self) -> str:
        raise NotImplementedError

    def _read_any(self) -> str:
        """Return buffered or freshly-read text, normalizing line feeds."""
        if self._buffer:
            data = self._buffer
            self._buffer = ""
            return data
        data = _ANSI_ESCAPE.sub("", self._read_channel())
        return data.replace("\r\n", "\n").replace("\r", "\n")

    def read_until_pattern(self, pattern: str, *, read_timeout: float) -> str:
        """Read until ``pattern`` matches the accumulated output.

        The matched terminator is retained in the returned text; any overflow
        after the match stays in the buffer for the next read.
        """
        output = ""
        start = time.monotonic()
        while (time.monotonic() - start) < read_timeout or not read_timeout:
            output += self._read_any()
            match = re.search(pattern, output)
            if match is not None:
                end = match.end()
                overflow = output[end:]
                self._buffer = overflow + self._buffer
                return output[:end]
        raise TransportError(
            f"pattern {pattern!r} not detected within {read_timeout}s"
        )

    def read_channel_timing(self, *, last_read: float, read_timeout: float) -> str:
        """Read until no new data arrives for ``last_read`` seconds."""
        channel_data = ""
        start = time.monotonic()
        while (time.monotonic() - start) < read_timeout or not read_timeout:
            new_data = self._read_any()
            if new_data:
                channel_data += new_data
            elif channel_data != "":
                time.sleep(last_read)
                new_data = self._read_any()
                if not new_data:
                    break
                channel_data += new_data
        return channel_data

    def write_channel(self, out_data: str) -> None:
        self._write_bytes(out_data.encode("utf-8", errors="replace"))

    def _write_bytes(self, data: bytes) -> None:
        raise NotImplementedError

    def find_prompt(self) -> str:
        """Send RETURN and return the last line of the terminal output."""
        self._buffer = ""
        self.write_channel("\n")
        time.sleep(self._initial_delay)
        prompt = self._read_any().strip()
        count = 0
        while count <= self._prompt_retries and not prompt:
            if not prompt:
                self.write_channel("\n")
                time.sleep(self._retry_delay)
                prompt = self._read_any().strip()
            count += 1
        lines = prompt.splitlines()
        prompt = lines[-1].strip() if lines else prompt
        self._buffer = ""
        if not prompt:
            raise TransportError("unable to find the device prompt")
        return prompt

    def send_command(
        self,
        command_string: str,
        *,
        expect_string: str | None = None,
        read_timeout: float,
        strip_prompt: bool = True,
        strip_command: bool = True,
        cmd_verify: bool = True,
    ) -> str:
        """Write a command and read until ``expect_string`` matches."""
        self.write_channel(command_string + "\n")
        pattern = expect_string or r"[#>$]\s*$"
        output = self.read_until_pattern(pattern, read_timeout=read_timeout)
        if strip_command and cmd_verify:
            output = _strip_command_echo(output, command_string)
        if strip_prompt:
            output = re.sub(r"[#>$]\s*$", "", output)
        return output


def _strip_command_echo(output: str, command: str) -> str:
    """Remove the echoed command line from the start of the output."""
    pattern = re.compile(rf"^\s*{re.escape(command)}\s*[\r\n]?")
    return pattern.sub("", output, count=1)


class SshTerminal(_BaseTerminal):
    """SSH terminal backed by a Paramiko interactive shell."""

    def __init__(self) -> None:
        super().__init__()
        self._client: paramiko.SSHClient | None = None
        self._channel: paramiko.Channel | None = None

    def connect(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str | None = None,
        key_file: str | None = None,
        passphrase: str | None = None,
        sock: object | None = None,
        disabled_algorithms: dict[str, list[str]] | None = None,
        known_hosts_file: str,
        conn_timeout: float,
        banner_timeout: float,
        auth_timeout: float,
    ) -> None:
        client = paramiko.SSHClient()
        client.load_host_keys(known_hosts_file)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        connect_kwargs: dict[str, Any] = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": conn_timeout,
            "banner_timeout": banner_timeout,
            "auth_timeout": auth_timeout,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if key_file is not None:
            connect_kwargs["key_filename"] = key_file
            if passphrase is not None:
                connect_kwargs["passphrase"] = passphrase
        elif password is not None:
            connect_kwargs["password"] = password
        if sock is not None:
            connect_kwargs["sock"] = sock
        if disabled_algorithms:
            connect_kwargs["disabled_algorithms"] = disabled_algorithms
        try:
            client.connect(**connect_kwargs)
            channel = client.invoke_shell()
            channel.settimeout(0.1)
        except Exception:
            client.close()
            raise
        self._client = client
        self._channel = channel

    def _read_channel(self) -> str:
        """Read all currently-available data from the channel.

        Drains until the socket reports no pending data so a single call returns
        everything the device has sent so far (Netmiko ``read_channel``
        semantics). With ``settimeout`` this blocks up to the timeout on the
        first empty read, then returns.
        """
        if self._channel is None:
            return ""
        chunks: list[bytes] = []
        while True:
            try:
                raw = self._channel.recv(_READ_CHUNK)
            except TimeoutError:
                break
            except (OSError, paramiko.SSHException):
                break
            if not raw:
                break
            chunks.append(raw)
        if not chunks:
            return ""
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _write_bytes(self, data: bytes) -> None:
        if self._channel is None or self._channel.closed:
            raise TransportError("SSH terminal is closed")
        try:
            self._channel.sendall(data)
        except (OSError, paramiko.SSHException) as exc:
            raise TransportError(f"SSH terminal write failed: {exc}") from exc

    def disconnect(self) -> None:
        if self._channel is not None and not self._channel.closed:
            try:
                self._channel.close()
            except Exception:
                pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._channel = None
        self._client = None


class _AsyncBridge:
    """Own an asyncio event loop running on a background daemon thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="telnet-async", daemon=True
        )
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro: Any, *, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)


class TelnetTerminal(_BaseTerminal):
    """Telnet or console terminal backed by telnetlib3 on a private loop."""

    def __init__(self) -> None:
        super().__init__()
        self._bridge = _AsyncBridge()
        self._reader: Any = None
        self._writer: Any = None
        self._closed = False

    def connect(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        conn_timeout: float,
    ) -> None:
        import telnetlib3  # imported lazily to keep SSH-only runs dependency-free

        async def _open() -> tuple[Any, Any]:
            reader, writer = await telnetlib3.open_connection(
                host=host,
                port=port,
                encoding="utf-8",
                connect_timeout=conn_timeout,
            )
            await self._telnet_login(reader, writer, username, password)
            return reader, writer

        try:
            reader, writer = self._bridge.run(_open(), timeout=conn_timeout)
        except Exception as exc:
            self.disconnect()
            raise TransportError(
                f"telnet connect to {host}:{port} failed: {exc}"
            ) from exc
        self._reader = reader
        self._writer = writer

    async def _telnet_login(
        self,
        reader: Any,
        writer: Any,
        username: str,
        password: str,
    ) -> None:
        login = re.compile(r"(?:[Ll]ogin\s*:|[Uu]ser(?:name)?\s*:)\s*$")
        await self._read_until_async(reader, login, timeout=10.0)
        writer.write(username.encode("utf-8") + b"\r")
        await writer.drain()
        await self._read_until_async(
            reader, re.compile(r"[Pp]assword\s*:\s*$"), timeout=10.0
        )
        writer.write(password.encode("utf-8") + b"\r")
        await writer.drain()

    async def _read_until_async(
        self, reader: Any, pattern: re.Pattern[str], *, timeout: float
    ) -> str:
        output = ""
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            chunk = await asyncio.wait_for(reader.read(1024), timeout=0.2)
            if not chunk:
                continue
            text = bytes(chunk).decode("utf-8", errors="replace")
            output += text.replace("\r\n", "\n").replace("\r", "\n")
            if pattern.search(output):
                return output
        raise TransportError("telnet login timed out")

    def _read_channel(self) -> str:
        if self._reader is None:
            return ""
        try:
            chunk = self._bridge.run(self._reader.read(1024), timeout=0.2)
        except Exception:
            return ""
        if not chunk:
            return ""
        return bytes(chunk).decode("utf-8", errors="replace")

    def _write_bytes(self, data: bytes) -> None:
        if self._writer is None:
            raise TransportError("telnet terminal is closed")
        try:
            self._bridge.run(self._writer.write(data), timeout=2.0)
            self._bridge.run(self._writer.drain(), timeout=2.0)
        except Exception as exc:
            raise TransportError(f"telnet terminal write failed: {exc}") from exc

    def disconnect(self) -> None:
        self._closed = True
        if self._writer is not None:
            try:
                self._bridge.run(self._writer.close(), timeout=2.0)
            except Exception:
                pass
        self._bridge.close()
        self._reader = None
        self._writer = None
