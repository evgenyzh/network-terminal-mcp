"""MCP tool contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from network_terminal_mcp.changes.models import ChangeResult
from network_terminal_mcp.server import create_server
from network_terminal_mcp.sessions import (
    CliHelpResult,
    CommandResult,
    ControlResult,
    OutputChunk,
    ResponseResult,
    SessionInfo,
    SessionState,
)


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.info = SessionInfo(
            session_id="session-1",
            target="sw1",
            host="192.0.2.1",
            platform="cisco_ios",
            dialect="cisco_ios",
            prompt="sw1#",
            state=SessionState.READY,
            created_at=datetime.now(UTC),
            last_used_at=datetime.now(UTC),
        )

    def open_session(self, name: str | None = None, **ad_hoc: object) -> SessionInfo:
        self.calls.append(("open_session", name if name is not None else ad_hoc))
        return self.info

    def run_command(self, session_id: str, command: str) -> CommandResult:
        self.calls.append(("run_command", (session_id, command)))
        return CommandResult(
            session_id=session_id,
            command=command,
            policy="allow",
            executed=True,
            output="ok",
        )

    def run_commands(self, session_id: str, commands: list[str]) -> list[CommandResult]:
        self.calls.append(("run_commands", (session_id, commands)))
        return [self.run_command(session_id, command) for command in commands]

    def cli_help(self, session_id: str, line: str) -> CliHelpResult:
        self.calls.append(("cli_help", (session_id, line)))
        return CliHelpResult(
            session_id=session_id,
            line=line,
            policy="allow",
            executed=True,
            output="completion",
        )

    def send_control(self, session_id: str, action: str) -> ControlResult:
        self.calls.append(("send_control", (session_id, action)))
        return ControlResult(session_id=session_id, action="space", output="next page")

    def respond(self, session_id: str, response: str) -> ResponseResult:
        self.calls.append(("respond", (session_id, response)))
        return ResponseResult(session_id=session_id, response=response, output="continued")

    def read_output(self, session_id: str, *, offset: int, limit: int | None) -> OutputChunk:
        self.calls.append(("read_output", (session_id, offset, limit)))
        return OutputChunk(session_id=session_id, offset=offset, output="chunk")

    def session_status(self, session_id: str) -> SessionInfo:
        self.calls.append(("session_status", session_id))
        return self.info

    def close_session(self, session_id: str, *, force: bool = False) -> SessionInfo:
        self.calls.append(("close_session", (session_id, force)))
        return self.info.model_copy(update={"state": SessionState.CLOSED})

    def plan_change(
        self,
        session_id: str,
        title: str,
        commands: list[str],
        *,
        safety_net: object | None = None,
        auto_approve: bool = False,
    ) -> ChangeResult:
        self.calls.append(("plan_change", (session_id, title, commands, safety_net, auto_approve)))
        return ChangeResult(
            change_id="chg-1",
            session_id=session_id,
            target="sw1",
            platform="cisco_ios",
            title=title,
            state="proposed",
            hash="abc",
            commands=[{"command": command} for command in commands],
        )

    def apply_change(self, change_id: str) -> ChangeResult:
        self.calls.append(("apply_change", change_id))
        return ChangeResult(
            change_id=change_id,
            session_id="session-1",
            target="sw1",
            platform="cisco_ios",
            title="t",
            state="applied",
            hash="abc",
            commands=[],
        )

    def abort_change(self, change_id: str) -> ChangeResult:
        self.calls.append(("abort_change", change_id))
        return ChangeResult(
            change_id=change_id,
            session_id="session-1",
            target="sw1",
            platform="cisco_ios",
            title="t",
            state="aborted",
            hash="abc",
            commands=[],
        )

    def finalize_change(self, change_id: str) -> ChangeResult:
        self.calls.append(("finalize_change", change_id))
        return ChangeResult(
            change_id=change_id,
            session_id="session-1",
            target="sw1",
            platform="cisco_ios",
            title="t",
            state="finalized",
            hash="abc",
            commands=[],
        )


@pytest.mark.asyncio
async def test_named_open_session_tool() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    result = await server.call_tool("open_session", {"target": "sw1"})
    assert result.structured_content["session_id"] == "session-1"
    assert manager.calls == [("open_session", "sw1")]


@pytest.mark.asyncio
async def test_ad_hoc_open_session_tool() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    result = await server.call_tool(
        "open_session",
        {
            "host": "192.0.2.2",
            "credentials": "net",
            "connection": "direct",
        },
    )
    assert result.structured_content["host"] == "192.0.2.1"
    assert manager.calls == [
        (
            "open_session",
            {
                "host": "192.0.2.2",
                "credentials": "net",
                "connection": "direct",
            },
        )
    ]


@pytest.mark.asyncio
async def test_command_and_status_tools() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    command = await server.call_tool(
        "run_command", {"session_id": "session-1", "command": "show version"}
    )
    status = await server.call_tool("session_status", {"session_id": "session-1"})
    assert command.structured_content["executed"] is True
    assert status.structured_content["state"] == "ready"


@pytest.mark.asyncio
async def test_interactive_tools() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    help_result = await server.call_tool(
        "cli_help", {"session_id": "session-1", "line": "show "}
    )
    control = await server.call_tool(
        "send_control", {"session_id": "session-1", "action": "space"}
    )
    response = await server.call_tool(
        "respond", {"session_id": "session-1", "response": "y"}
    )

    assert help_result.structured_content["output"] == "completion"
    assert control.structured_content["action"] == "space"
    assert response.structured_content["output"] == "continued"
    assert manager.calls == [
        ("cli_help", ("session-1", "show ")),
        ("send_control", ("session-1", "space")),
        ("respond", ("session-1", "y")),
    ]


@pytest.mark.asyncio
async def test_send_control_rejects_unknown_action_before_manager_dispatch() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    with pytest.raises(ToolError, match="Input should be 'space', 'q' or 'ctrl-c'"):
        await server.call_tool(
            "send_control", {"session_id": "session-1", "action": "ctrl-u"}
        )

    assert manager.calls == []


@pytest.mark.asyncio
async def test_output_and_close_tools() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    output = await server.call_tool(
        "read_output", {"session_id": "session-1", "offset": 3, "limit": 10}
    )
    closed = await server.call_tool("close_session", {"session_id": "session-1"})
    assert output.structured_content["output"] == "chunk"
    assert closed.structured_content["state"] == "closed"


@pytest.mark.asyncio
async def test_change_tools() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    planned = await server.call_tool(
        "plan_change",
        {
            "session_id": "session-1",
            "title": "vlan",
            "commands": ["vlan 100"],
            "safety_net": {
                "save": "copy running-config startup-config",
                "arm": "reload in 10",
                "cancel": "reload cancel",
            },
        },
    )
    applied = await server.call_tool("apply_change", {"change_id": "chg-1"})
    aborted = await server.call_tool("abort_change", {"change_id": "chg-2"})
    finalized = await server.call_tool("finalize_change", {"change_id": "chg-3"})
    forced = await server.call_tool(
        "close_session", {"session_id": "session-1", "force": True}
    )

    assert planned.structured_content["state"] == "proposed"
    assert applied.structured_content["state"] == "applied"
    assert aborted.structured_content["state"] == "aborted"
    assert finalized.structured_content["state"] == "finalized"
    assert forced.structured_content["state"] == "closed"
    assert manager.calls == [
        (
            "plan_change",
            (
                "session-1",
                "vlan",
                ["vlan 100"],
                {
                    "save": "copy running-config startup-config",
                    "arm": "reload in 10",
                    "cancel": "reload cancel",
                },
                False,
            ),
        ),
        ("apply_change", "chg-1"),
        ("abort_change", "chg-2"),
        ("finalize_change", "chg-3"),
        ("close_session", ("session-1", True)),
    ]
