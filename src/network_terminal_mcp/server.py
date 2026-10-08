"""stdio MCP server for raw network-terminal sessions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path

import anyio
from mcp.server import MCPServer

from network_terminal_mcp.config.loader import load_config
from network_terminal_mcp.config.models import (
    CredentialSpec,
    HostKeyPolicy,
    LegacyAlgorithms,
    OpenSpec,
    Protocol,
    RouteSpec,
    SerialParams,
)
from network_terminal_mcp.sessions import (
    OutputChunk,
    SessionInfo,
    SessionManager,
    TerminalOutput,
    TerminalSecretResult,
    TerminalWriteResult,
)
from network_terminal_mcp.usage import INSTRUCTIONS, USAGE_URI, usage_text


def create_server(
    *,
    config_dir: Path | None = None,
    manager: SessionManager | None = None,
) -> MCPServer:
    """Build an MCP server, optionally with an injected manager for tests."""
    _configure_library_logging()
    session_manager = manager or SessionManager(load_config(config_dir))
    server = MCPServer("network-terminal", instructions=INSTRUCTIONS)

    @server.resource(
        USAGE_URI,
        name="usage",
        title="network-terminal operating manual",
        description=(
            "Full operating manual: raw terminal workflow, nested ssh/telnet "
            "hops, secrets, configuration changes, and troubleshooting."
        ),
        mime_type="text/markdown",
    )
    def usage() -> str:
        return usage_text()

    @server.tool()
    async def open_session(
        host: str,
        credentials: CredentialSpec | None = None,
        port: int | None = None,
        protocol: Protocol = "ssh",
        route: RouteSpec | None = None,
        host_key_policy: HostKeyPolicy = "strict",
        legacy: LegacyAlgorithms | None = None,
        allow_telnet: bool = False,
        allow_plaintext_password: bool = False,
        allow_serial: bool = False,
        serial: SerialParams | None = None,
    ) -> SessionInfo:
        """Open a raw interactive terminal session and keep it open.

        Describe the target inline: ``protocol`` and ``port`` describe the
        final target, ``credentials`` are references (a ``pass`` entry, an
        explicit SSH key file) for ssh/telnet/console. The optional single-hop
        route is ``socks`` (local SOCKS5 proxy) or ``proxyjump`` (SSH jump
        host); use proxyjump to reach a bastion quickly and run further ssh or
        telnet hops yourself with terminal_write inside the same session.
        For local console cables use ``protocol="serial"`` with an absolute
        ``/dev/tty*`` path in ``host``, serial parameters and
        ``allow_serial=true``. Every call goes through the client's permission
        approval gate.
        """
        spec = OpenSpec(
            host=host,
            port=port,
            protocol=protocol,
            credentials=credentials,
            route=route,
            host_key_policy=host_key_policy,
            legacy=legacy,
            allow_telnet=allow_telnet,
            allow_plaintext_password=allow_plaintext_password,
            allow_serial=allow_serial,
            serial=serial or SerialParams(),
        )
        return await _run_sync(session_manager.open_session, spec)

    @server.tool()
    async def terminal_write(
        session_id: str, data: str, enter: bool = True
    ) -> TerminalWriteResult:
        """Write exact input to the session's terminal stream.

        Use this for commands, interactive keystrokes and nested hops such as
        ``ssh user@host``. ``enter`` appends the transport line terminator
        (newline, or carriage return on serial). The full ``data`` string is
        recorded in the audit; never type passwords, passphrases or other
        secrets here — use terminal_write_secret.
        """
        return await _run_sync(session_manager.terminal_write, session_id, data, enter=enter)

    @server.tool()
    async def terminal_read(
        session_id: str, timeout: float | None = None
    ) -> TerminalOutput:
        """Read new terminal output until the stream is quiet or timeout expires.

        Returns only output that arrived since your previous read, including
        pager screens, password prompts, banners and shell output; no prompt
        shape is required. If ``truncated=true``, continue from
        ``next_output_offset`` with read_output.
        """
        return await _run_sync(session_manager.terminal_read, session_id, timeout=timeout)

    @server.tool()
    async def terminal_write_secret(session_id: str, entry: str) -> TerminalSecretResult:
        """Send a ``pass`` entry value at a live password or passphrase prompt.

        Use this for every secret typed interactively (device logins, ssh,
        enable, TACACS). The value is resolved inside the server and never
        appears in tool arguments, results or audit; only the entry name and
        byte count are logged.
        """
        return await _run_sync(session_manager.terminal_write_secret, session_id, entry)

    @server.tool()
    async def read_output(
        session_id: str, offset: int | None = None, limit: int | None = None
    ) -> OutputChunk:
        """Read buffered session output that you have not seen yet.

        Without ``offset`` it continues from your read cursor, so repeated
        calls never re-inject old output. Pass an explicit ``offset`` only to
        deliberately revisit older buffered output (for example the
        ``next_output_offset`` of a truncated read).
        """
        return await _run_sync(
            session_manager.read_output,
            session_id,
            offset=offset,
            limit=limit,
        )

    @server.tool()
    async def session_status(session_id: str) -> SessionInfo:
        """Return state and prompt metadata for an active session."""
        return await _run_sync(session_manager.session_status, session_id)

    @server.tool()
    async def close_session(session_id: str) -> SessionInfo:
        """Close a session and disconnect from the device."""
        return await _run_sync(session_manager.close_session, session_id)

    return server


def _configure_library_logging() -> None:
    """Keep SSH library diagnostics out of the MCP stdio transport."""
    logging.getLogger("paramiko").setLevel(logging.WARNING)


async def _run_sync[**P, T](
    function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run a blocking manager operation outside the async MCP event loop."""
    return await anyio.to_thread.run_sync(partial(function, *args, **kwargs))


def main() -> None:
    """Run the stdio server. stdout is reserved exclusively for MCP traffic."""
    create_server().run(transport="stdio")
