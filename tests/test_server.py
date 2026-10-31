"""MCP tool contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from network_terminal_mcp.errors import NetworkMCPError, TransportError
from network_terminal_mcp.server import create_server
from network_terminal_mcp.sessions import (
    OutputChunk,
    SessionInfo,
    SessionState,
    TerminalOutput,
    TerminalSecretResult,
    TerminalWriteResult,
)


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.open_error: NetworkMCPError | None = None
        self.info = SessionInfo(
            session_id="session-1",
            command="ssh operator@192.0.2.1",
            state=SessionState.READY,
            created_at=datetime.now(UTC),
            last_used_at=datetime.now(UTC),
        )

    def open_session(self, command: str) -> SessionInfo:
        self.calls.append(("open_session", command))
        if self.open_error is not None:
            raise self.open_error
        return self.info

    def terminal_write(
        self, session_id: str, data: str, *, enter: bool = True
    ) -> TerminalWriteResult:
        self.calls.append(("terminal_write", (session_id, data, enter)))
        return TerminalWriteResult(
            session_id=session_id, bytes_sent=len(data), state=SessionState.READY
        )

    def terminal_read(
        self, session_id: str, *, timeout: float | None = None
    ) -> TerminalOutput:
        self.calls.append(("terminal_read", (session_id, timeout)))
        return TerminalOutput(session_id=session_id, output="output", output_offset=0)

    def terminal_write_secret(
        self,
        session_id: str,
        *,
        value: str | None = None,
        env: str | None = None,
        file: str | None = None,
    ) -> TerminalSecretResult:
        self.calls.append(
            ("terminal_write_secret", (session_id, value, env, file))
        )
        return TerminalSecretResult(
            session_id=session_id,
            source="value" if value is not None else "env" if env is not None else "file",
            bytes_sent=7,
            state=SessionState.READY,
        )

    def read_output(
        self, session_id: str, *, offset: int | None, limit: int | None
    ) -> OutputChunk:
        self.calls.append(("read_output", (session_id, offset, limit)))
        return OutputChunk(session_id=session_id, offset=offset or 0, output="chunk")

    def session_status(self, session_id: str) -> SessionInfo:
        self.calls.append(("session_status", session_id))
        return self.info

    def close_session(self, session_id: str) -> SessionInfo:
        self.calls.append(("close_session", session_id))
        return self.info.model_copy(update={"state": SessionState.CLOSED})


@pytest.mark.asyncio
async def test_open_session_tool_accepts_only_a_command() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    result = await server.call_tool(
        "open_session", {"command": "ssh operator@192.0.2.1"}
    )

    assert result.structured_content["session_id"] == "session-1"
    assert result.structured_content["command"] == "ssh operator@192.0.2.1"
    assert result.structured_content["last_output_at"] is None
    assert result.structured_content["idle_seconds"] == 0.0
    assert manager.calls == [("open_session", "ssh operator@192.0.2.1")]


@pytest.mark.asyncio
async def test_open_session_schema_has_only_command_and_rejects_removed_fields() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    tools = {tool.name: tool for tool in await server.list_tools()}
    schema = tools["open_session"].input_schema

    assert schema["properties"] == {
        "command": {"title": "Command", "type": "string"}
    }
    assert schema["additionalProperties"] is False

    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        await server.call_tool(
            "open_session",
            {"command": "ssh operator@192.0.2.1", "host": "192.0.2.1"},
        )
    assert manager.calls == []


@pytest.mark.asyncio
async def test_expected_errors_keep_the_real_message() -> None:
    manager = FakeManager()
    manager.open_error = TransportError(
        "ssh exited with code 255 during startup:\nHost key verification failed."
    )
    server = create_server(manager=manager)  # type: ignore[arg-type]

    with pytest.raises(ToolError) as error:
        await server.call_tool("open_session", {"command": "ssh operator@192.0.2.1"})

    message = str(error.value)
    assert "transport_error" in message
    assert "ssh exited with code 255" in message
    assert "Host key verification failed." in message
    assert "Error executing tool open_session" in message


@pytest.mark.asyncio
async def test_unexpected_internal_errors_do_not_leak_details() -> None:
    class CrashingManager(FakeManager):
        def open_session(self, command: str) -> SessionInfo:
            raise RuntimeError("internal file /secret/token failed")

    manager = CrashingManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    with pytest.raises(UnexpectedToolError) as error:
        await server.call_tool("open_session", {"command": "ssh operator@192.0.2.1"})

    message = str(error.value)
    assert "internal file" not in message
    assert "/secret/token" not in message
    assert "Error executing tool open_session" in message


@pytest.mark.asyncio
async def test_terminal_tools_dispatch() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    written = await server.call_tool(
        "terminal_write",
        {"session_id": "session-1", "data": "ssh operator@192.0.2.10"},
    )
    read = await server.call_tool("terminal_read", {"session_id": "session-1"})
    secret = await server.call_tool(
        "terminal_write_secret",
        {"session_id": "session-1", "env": "DEVICE_PASSWORD"},
    )

    assert written.structured_content["bytes_sent"] == len("ssh operator@192.0.2.10")
    assert "data" not in written.structured_content
    assert read.structured_content["output"] == "output"
    assert secret.structured_content["source"] == "env"
    assert manager.calls == [
        ("terminal_write", ("session-1", "ssh operator@192.0.2.10", True)),
        ("terminal_read", ("session-1", None)),
        ("terminal_write_secret", ("session-1", None, "DEVICE_PASSWORD", None)),
    ]


@pytest.mark.asyncio
async def test_terminal_write_enter_flag_is_forwarded() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    await server.call_tool(
        "terminal_write",
        {"session_id": "session-1", "data": "\u0003", "enter": False},
    )

    assert manager.calls == [("terminal_write", ("session-1", "\u0003", False))]


@pytest.mark.asyncio
async def test_output_and_status_tools() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    output = await server.call_tool(
        "read_output", {"session_id": "session-1", "offset": 3, "limit": 10}
    )
    unseen = await server.call_tool("read_output", {"session_id": "session-1"})
    status = await server.call_tool("session_status", {"session_id": "session-1"})
    closed = await server.call_tool("close_session", {"session_id": "session-1"})
    assert output.structured_content["output"] == "chunk"
    assert unseen.structured_content["output"] == "chunk"
    assert manager.calls[-4] == ("read_output", ("session-1", 3, 10))
    assert manager.calls[-3] == ("read_output", ("session-1", None, None))
    assert status.structured_content["state"] == "ready"
    assert closed.structured_content["state"] == "closed"


@pytest.mark.asyncio
async def test_server_ships_usage_instructions_and_resource() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    assert server.version == "0.2.1"
    instructions = server.instructions or ""
    assert "open_session(command)" in instructions
    assert "value, env, or file" in instructions
    assert "Do not re-read" in instructions

    resources = await server.list_resources()
    assert any(
        str(resource.uri) == "network-terminal://usage" for resource in resources
    )

    contents = await server.read_resource("network-terminal://usage")
    manual = "".join(
        item.content if isinstance(item.content, str) else item.content.decode()
        for item in contents
    )
    assert "terminal_write_secret" in manual
    assert "serial /dev/ttyUSB0 115200" in manual
    assert "ProxyJump" in manual
    assert "pass entry" not in manual
    assert "no mandatory configuration" in manual


@pytest.mark.asyncio
async def test_only_the_seven_raw_terminal_tools_exist() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    names = {tool.name for tool in await server.list_tools()}

    assert names == {
        "open_session",
        "terminal_write",
        "terminal_read",
        "terminal_write_secret",
        "read_output",
        "session_status",
        "close_session",
    }
    assert manager.calls == []
