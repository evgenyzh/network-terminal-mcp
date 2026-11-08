"""stdio MCP server for persistent local terminals."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import NotRequired, TypedDict

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from network_terminal_mcp import __version__
from network_terminal_mcp.errors import NetworkMCPError
from network_terminal_mcp.sessions import (
    SessionInfo,
    SessionManager,
)
from network_terminal_mcp.sessions.models import SessionState
from network_terminal_mcp.usage import INSTRUCTIONS, USAGE_URI, usage_text


class OpenReply(TypedDict):
    session_id: str
    state: SessionState
    warnings: NotRequired[list[str]]
    error: NotRequired[str]


class WriteReply(TypedDict):
    ok: bool
    state: NotRequired[SessionState]


class ReadReply(TypedDict):
    output: str
    truncated: NotRequired[bool]
    next_output_offset: NotRequired[int]


class BufferReply(TypedDict):
    output: str
    offset: NotRequired[int]
    next_offset: NotRequired[int]
    oldest_offset: NotRequired[int]
    truncated: NotRequired[bool]


class CloseReply(TypedDict):
    state: SessionState
    exit_code: NotRequired[int]
    error: NotRequired[str]


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
    async def open_session(command: str) -> OpenReply:
        """Open a persistent terminal: ssh, telnet, or serial DEVICE [BAUDRATE].

        No shell is involved. SSH uses the local client, config, agent and keys.
        """
        info = await _run_tool(session_manager.open_session, command)
        reply: OpenReply = {"session_id": info.session_id, "state": info.state}
        if info.warnings:
            reply["warnings"] = info.warnings
        if info.error:
            reply["error"] = info.error
        return reply

    @server.tool()
    async def terminal_write(
        session_id: str, data: str, enter: bool = True
    ) -> WriteReply:
        """Write input, appending a line terminator unless enter=false.

        Input is audited after known-secret redaction; use terminal_write_secret
        for passwords. Timeout fails the session and may mean a partial write.
        """
        result = await _run_tool(
            session_manager.terminal_write, session_id, data, enter=enter
        )
        reply: WriteReply = {"ok": True}
        if result.state != SessionState.READY:
            reply["state"] = result.state
        return reply

    @server.tool()
    async def terminal_read(
        session_id: str, timeout: float | None = None
    ) -> ReadReply:
        """Read only new output until quiet or timeout; no prompt shape required.

        If truncated, continue with read_output or next_output_offset.
        """
        result = await _run_tool(
            session_manager.terminal_read, session_id, timeout=timeout
        )
        reply: ReadReply = {"output": result.output}
        if result.truncated:
            reply["truncated"] = True
        if result.next_output_offset is not None:
            reply["next_output_offset"] = result.next_output_offset
        return reply

    @server.tool()
    async def terminal_write_secret(
        session_id: str,
        value: str | None = None,
        env: str | None = None,
        file: str | None = None,
    ) -> WriteReply:
        """Send a secret from exactly one source: value, env, or file.

        Prefer env or file; plaintext value remains in client history. The server
        redacts known secrets and never returns or audits the secret value.
        """
        result = await _run_tool(
            session_manager.terminal_write_secret,
            session_id,
            value=value,
            env=env,
            file=file,
        )
        reply: WriteReply = {"ok": True}
        if result.state != SessionState.READY:
            reply["state"] = result.state
        return reply

    @server.tool()
    async def read_output(
        session_id: str, offset: int | None = None, limit: int | None = None
    ) -> BufferReply:
        """Read buffered output from the cursor, or a specified historical offset."""
        result = await _run_tool(
            session_manager.read_output,
            session_id,
            offset=offset,
            limit=limit,
        )
        reply: BufferReply = {"output": result.output}
        if offset is not None:
            reply["offset"] = result.offset
        if result.next_offset is not None:
            reply["next_offset"] = result.next_offset
        if result.truncated:
            reply["truncated"] = True
            reply["oldest_offset"] = result.oldest_offset
        return reply

    @server.tool()
    async def session_status(session_id: str) -> SessionInfo:
        """Return state, command, timestamps, idle time, warnings, and exit status."""
        return await _run_tool(session_manager.session_status, session_id)

    @server.tool()
    async def close_session(session_id: str) -> CloseReply:
        """Close the terminal and child process, retaining its final status."""
        info = await _run_tool(session_manager.close_session, session_id)
        reply: CloseReply = {"state": info.state}
        if info.exit_code not in (None, 0):
            reply["exit_code"] = info.exit_code
        if info.error:
            reply["error"] = info.error
        return reply

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
