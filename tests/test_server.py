"""MCP tool contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from network_terminal_mcp.server import create_server
from network_terminal_mcp.sessions import (
    CommandResult,
    OutputChunk,
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

    def read_output(self, session_id: str, *, offset: int, limit: int | None) -> OutputChunk:
        self.calls.append(("read_output", (session_id, offset, limit)))
        return OutputChunk(session_id=session_id, offset=offset, output="chunk")

    def session_status(self, session_id: str) -> SessionInfo:
        self.calls.append(("session_status", session_id))
        return self.info

    def close_session(self, session_id: str) -> SessionInfo:
        self.calls.append(("close_session", session_id))
        return self.info.model_copy(update={"state": SessionState.CLOSED})


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
            "platform": "cisco_ios",
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
                "platform": "cisco_ios",
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
async def test_output_and_close_tools() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    output = await server.call_tool(
        "read_output", {"session_id": "session-1", "offset": 3, "limit": 10}
    )
    closed = await server.call_tool("close_session", {"session_id": "session-1"})
    assert output.structured_content["output"] == "chunk"
    assert closed.structured_content["state"] == "closed"
