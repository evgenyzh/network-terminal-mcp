"""stdio MCP server for direct network-terminal sessions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Literal

import anyio
from mcp.server import MCPServer

from network_terminal_mcp.changes.models import ChangeResult, SafetyNet
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
        platform: str | None = None,
        credentials: str | None = None,
        connection: str | None = None,
        port: int | None = None,
    ) -> SessionInfo:
        """Open an inventory target or an ad-hoc SSH session.

        ``target`` is an inventory name (infrastructure hosts only). Without it,
        ``host`` (and usually ``platform``) are required; ``credentials`` and
        ``connection`` fall back to the configured inventory defaults. Passwords
        are never accepted as tool arguments.
        """
        if target is not None:
            if any(value is not None for value in (host, platform, credentials, connection, port)):
                raise TargetError("target cannot be combined with ad-hoc target fields")
            return await _run_sync(session_manager.open_session, target)
        ad_hoc = {
            key: value
            for key, value in {
                "host": host,
                "platform": platform,
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
    async def set_platform(session_id: str, platform: str) -> SessionInfo:
        """Switch an active session to another device platform/driver.

        Use after the device output shows the real vendor when an ad-hoc
        ``open_session`` was opened with a best-guess ``platform``. The SSH
        connection is kept; only the Netmiko driver and session metadata change.
        """
        return await _run_sync(session_manager.set_platform, session_id, platform)

    @server.tool()
    async def close_session(session_id: str, force: bool = False) -> SessionInfo:
        """Close a session and disconnect from the device.

        ``force=true`` closes even when a scheduled reboot from an applied
        change plan is still pending cancellation (emergency use).
        """
        return await _run_sync(session_manager.close_session, session_id, force=force)

    @server.tool()
    async def plan_change(
        session_id: str,
        title: str,
        commands: list[str],
        safety_net: SafetyNet | None = None,
        auto_approve: bool = False,
    ) -> ChangeResult:
        """Register a configuration change plan without executing anything.

        The device must have ``allow_writes: true`` and the policy default
        ``write_change`` must not be ``deny``. Commands are validated for
        structural safety. Optionally declare a reload/commit safety net with
        opaque ``save``, ``arm`` and ``cancel`` commands.
        """
        return await _run_sync(
            session_manager.plan_change,
            session_id,
            title,
            commands,
            safety_net=(
                safety_net.model_dump() if safety_net is not None else None
            ),
            auto_approve=auto_approve,
        )

    @server.tool()
    async def apply_change(change_id: str) -> ChangeResult:
        """Confirm and then execute a change plan.

        The first call returns the canonical command list with
        ``confirmation_required`` and executes nothing; the second call
        executes the plan.
        """
        return await _run_sync(session_manager.apply_change, change_id)

    @server.tool()
    async def abort_change(change_id: str) -> ChangeResult:
        """Cancel a change plan before anything is executed."""
        return await _run_sync(session_manager.abort_change, change_id)

    @server.tool()
    async def finalize_change(change_id: str) -> ChangeResult:
        """Cancel the scheduled reboot or commit the safety net after apply."""
        return await _run_sync(session_manager.finalize_change, change_id)

    return server


def _configure_library_logging() -> None:
    """Keep SSH library diagnostics out of the MCP stdio transport."""
    logging.getLogger("netmiko").setLevel(logging.WARNING)
    logging.getLogger("paramiko").setLevel(logging.WARNING)


async def _run_sync[**P, T](
    function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run a blocking manager operation outside the async MCP event loop."""
    return await anyio.to_thread.run_sync(partial(function, *args, **kwargs))


def main() -> None:
    """Run the stdio server. stdout is reserved exclusively for MCP traffic."""
    create_server().run(transport="stdio")
