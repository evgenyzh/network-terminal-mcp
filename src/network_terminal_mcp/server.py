"""stdio MCP server for direct network-terminal sessions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Literal

import anyio
from mcp.server import MCPServer

from network_terminal_mcp.changes.models import ChangeResult
from network_terminal_mcp.config.loader import load_config
from network_terminal_mcp.errors import TargetError
from network_terminal_mcp.sessions import (
    CliHelpResult,
    CommandResult,
    ControlResult,
    OutputChunk,
    ResponseResult,
    SessionInfo,
    SessionManager,
)


def create_server(
    *,
    config_dir: Path | None = None,
    manager: SessionManager | None = None,
) -> MCPServer:
    """Build an MCP server, optionally with an injected manager for tests."""
    _configure_library_logging()
    session_manager = manager or SessionManager(load_config(config_dir))
    server = MCPServer("network-terminal")

    @server.tool()
    async def open_session(
        target: str | None = None,
        host: str | None = None,
        credentials: str | None = None,
        connection: str | None = None,
        port: int | None = None,
    ) -> SessionInfo:
        """Open an inventory target or an ad-hoc SSH/Telnet session.

        ``target`` is an inventory name (infrastructure hosts only). Without it,
        ``host`` is required; ``credentials`` and ``connection`` fall back to
        the configured inventory defaults. Passwords are never accepted as tool
        arguments.
        """
        if target is not None:
            if any(value is not None for value in (host, credentials, connection, port)):
                raise TargetError("target cannot be combined with ad-hoc target fields")
            return await _run_sync(session_manager.open_session, target)
        ad_hoc = {
            key: value
            for key, value in {
                "host": host,
                "credentials": credentials,
                "connection": connection,
                "port": port,
            }.items()
            if value is not None
        }
        return await _run_sync(session_manager.open_session, None, **ad_hoc)

    @server.tool()
    async def run_command(session_id: str, command: str) -> CommandResult:
        """Execute one policy-allowed read-only CLI command in a session."""
        return await _run_sync(session_manager.run_command, session_id, command)

    @server.tool()
    async def run_commands(session_id: str, commands: list[str]) -> list[CommandResult]:
        """Execute commands serially, stopping at the first unapproved command."""
        return await _run_sync(session_manager.run_commands, session_id, commands)

    @server.tool()
    async def cli_help(session_id: str, line: str) -> CliHelpResult:
        """Read CLI completion help without executing ``line`` or pressing Enter."""
        return await _run_sync(session_manager.cli_help, session_id, line)

    @server.tool()
    async def send_control(
        session_id: str, action: Literal["space", "q", "ctrl-c"]
    ) -> ControlResult:
        """Send a control key only when the session is in a compatible state."""
        return await _run_sync(session_manager.send_control, session_id, action)

    @server.tool()
    async def respond(session_id: str, response: str) -> ResponseResult:
        """Reply only with a token allowed by the current device confirmation prompt."""
        return await _run_sync(session_manager.respond, session_id, response)

    @server.tool()
    async def read_output(
        session_id: str, offset: int = 0, limit: int | None = None
    ) -> OutputChunk:
        """Read a bounded slice of accumulated session output."""
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

    @server.tool()
    async def run_change(session_id: str, commands: list[str]) -> ChangeResult:
        """Execute policy-allowed configuration change commands immediately.

        Commands are validated for structural safety and run in order. A
        device confirmation or pager aborts the run and reports it as failed.
        """
        return await _run_sync(session_manager.run_change, session_id, commands)

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
