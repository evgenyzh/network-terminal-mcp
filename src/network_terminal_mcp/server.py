"""stdio MCP server for persistent local terminals."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from network_terminal_mcp import __version__
from network_terminal_mcp.errors import NetworkMCPError
from network_terminal_mcp.sessions import (
    OutputChunk,
    SessionInfo,
    SessionManager,
    TerminalOutput,
    TerminalSecretResult,
    TerminalWriteResult,
)
from network_terminal_mcp.usage import INSTRUCTIONS, USAGE_URI, usage_text


def create_server(*, manager: SessionManager | None = None) -> MCPServer:
    """Build an MCP server, optionally with an injected manager for tests."""
    session_manager = manager or SessionManager()

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(session_manager.shutdown)

    server = MCPServer(
        "network-terminal",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.resource(
        USAGE_URI,
        name="usage",
        title="network-terminal operating manual",
        description=(
            "Full operating manual: command-based connections, persistent "
            "terminal workflow, secrets, and troubleshooting."
        ),
        mime_type="text/markdown",
    )
    def usage() -> str:
        return usage_text()

    @server.tool()
    async def open_session(command: str) -> SessionInfo:
        """Open one persistent terminal from a connection command.

        Use a real client command. SSH examples:
        ``ssh operator@192.0.2.10``,
        ``ssh -J operator@bastion.example.net operator@192.0.2.10``,
        ``ssh -i ~/.ssh/id_ed25519 -p 2222 operator@192.0.2.20``.
        Telnet: ``telnet 192.0.2.30 23``. Local serial:
        ``serial /dev/ttyUSB0 115200``. The command is not run through a
        shell. SSH uses your existing config, agent, keys, and options.
        """
        return await _run_tool(session_manager.open_session, command)

    @server.tool()
    async def terminal_write(
        session_id: str, data: str, enter: bool = True
    ) -> TerminalWriteResult:
        """Write exact input to the session's terminal stream.

        Use this for commands, interactive keystrokes, and further hops such as
        ``ssh user@host``. ``enter`` appends the transport line terminator
        (newline, or carriage return on serial). A client that stops reading
        input causes a bounded write timeout and marks the session failed. The
        data is audited after known-secret redaction; never type a password
        here. Use terminal_write_secret for secrets.
        """
        return await _run_tool(
            session_manager.terminal_write, session_id, data, enter=enter
        )

    @server.tool()
    async def terminal_read(
        session_id: str, timeout: float | None = None
    ) -> TerminalOutput:
        """Read new terminal output until the stream is quiet or timeout expires.

        Returns only output that arrived since your previous read, including
        banners, password prompts, pagers, and command output; no prompt shape
        is required. If ``truncated=true``, continue from
        ``next_output_offset`` with read_output.
        """
        return await _run_tool(
            session_manager.terminal_read, session_id, timeout=timeout
        )

    @server.tool()
    async def terminal_write_secret(
        session_id: str,
        value: str | None = None,
        env: str | None = None,
        file: str | None = None,
    ) -> TerminalSecretResult:
        """Send a secret at a live prompt from exactly one source.

        Choose one: ``value`` carries a plaintext string, ``env`` names a
        variable in the MCP process environment, and ``file`` names a local
        file whose final newline is stripped. The value is never returned or
        written to audit; only the source kind and byte count are logged.
        A plaintext ``value`` remains in the model/client history, so prefer
        ``env`` or ``file`` when available.
        """
        return await _run_tool(
            session_manager.terminal_write_secret,
            session_id,
            value=value,
            env=env,
            file=file,
        )

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
        return await _run_tool(
            session_manager.read_output,
            session_id,
            offset=offset,
            limit=limit,
        )

    @server.tool()
    async def session_status(session_id: str) -> SessionInfo:
        """Return state, command, timestamps, idle time, warnings, and exit status."""
        return await _run_tool(session_manager.session_status, session_id)

    @server.tool()
    async def close_session(session_id: str) -> SessionInfo:
        """Close the terminal and child process, retaining its final status."""
        return await _run_tool(session_manager.close_session, session_id)

    _forbid_unknown_arguments(server)
    return server


def _forbid_unknown_arguments(server: MCPServer) -> None:
    """Make generated argument models strict about unknown fields.

    The SDK's generated argument models ignore extra keys by default, which
    would silently accept the removed structured connection fields. Rebuild
    each model with ``extra='forbid'`` and advertise the same JSON Schema.
    """
    for tool in server._tool_manager._tools.values():  # noqa: SLF001 - no public hook
        model = tool.fn_metadata.arg_model
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
        tool.parameters["additionalProperties"] = False


async def _run_sync[**P, T](
    function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run a blocking manager operation outside the async MCP event loop."""
    return await anyio.to_thread.run_sync(partial(function, *args, **kwargs))


async def _run_tool[**P, T](
    function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Convert expected project errors into model-readable MCP ToolError."""
    try:
        return await _run_sync(function, *args, **kwargs)
    except NetworkMCPError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    """Run the stdio server. stdout is reserved exclusively for MCP traffic."""
    create_server().run(transport="stdio")
