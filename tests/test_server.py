"""MCP tool contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from network_terminal_mcp.config.models import OpenSpec
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
        self.info = SessionInfo(
            session_id="session-1",
            route="direct",
            host="192.0.2.1",
            prompt="sw1#",
            state=SessionState.READY,
            created_at=datetime.now(UTC),
            last_used_at=datetime.now(UTC),
        )

    def open_session(self, spec: OpenSpec) -> SessionInfo:
        self.calls.append(("open_session", spec))
        return self.info

    def terminal_write(
        self, session_id: str, data: str, *, enter: bool = True
    ) -> TerminalWriteResult:
        self.calls.append(("terminal_write", (session_id, data, enter)))
        return TerminalWriteResult(
            session_id=session_id, bytes_sent=len(data), state=SessionState.READY
        )

    def terminal_read(self, session_id: str, *, timeout: float | None = None) -> TerminalOutput:
        self.calls.append(("terminal_read", (session_id, timeout)))
        return TerminalOutput(session_id=session_id, output="output", output_offset=0)

    def terminal_write_secret(self, session_id: str, entry: str) -> TerminalSecretResult:
        self.calls.append(("terminal_write_secret", (session_id, entry)))
        return TerminalSecretResult(
            session_id=session_id, entry=entry, bytes_sent=7, state=SessionState.READY
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
async def test_open_session_tool_accepts_an_inline_spec() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    result = await server.call_tool(
        "open_session",
        {
            "host": "192.0.2.2",
            "credentials": {"entry": "network/net", "username": "operator"},
            "route": {"type": "socks", "host": "127.0.0.1", "port": 1080},
        },
    )
    assert result.structured_content["session_id"] == "session-1"
    assert result.structured_content["route"] == "direct"
    call_name, spec = manager.calls[0]
    assert call_name == "open_session"
    assert isinstance(spec, OpenSpec)
    assert spec.host == "192.0.2.2"
    assert spec.route is not None
    assert spec.route.type == "socks"


@pytest.mark.asyncio
async def test_open_session_tool_accepts_a_serial_spec() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    await server.call_tool(
        "open_session",
        {
            "host": "/dev/ttyUSB0",
            "protocol": "serial",
            "allow_serial": True,
            "serial": {"baudrate": 115200},
        },
    )
    _, spec = manager.calls[0]
    assert isinstance(spec, OpenSpec)
    assert spec.protocol == "serial"
    assert spec.credentials is None
    assert spec.serial.baudrate == 115200


@pytest.mark.asyncio
async def test_open_session_tool_rejects_plaintext_without_the_flag() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    with pytest.raises(ToolError):
        await server.call_tool(
            "open_session",
            {
                "host": "192.0.2.2",
                "credentials": {
                    "backend": "plaintext",
                    "username": "operator",
                    "password": "hunter2",
                },
            },
        )
    assert manager.calls == []


@pytest.mark.asyncio
async def test_open_session_tool_rejects_telnet_without_the_flag() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    with pytest.raises(ToolError):
        await server.call_tool(
            "open_session",
            {
                "host": "192.0.2.2",
                "protocol": "telnet",
                "credentials": {"entry": "network/net", "username": "operator"},
            },
        )
    assert manager.calls == []


@pytest.mark.asyncio
async def test_terminal_tools_dispatch() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    written = await server.call_tool(
        "terminal_write", {"session_id": "session-1", "data": "ssh operator@192.0.2.10"}
    )
    read = await server.call_tool("terminal_read", {"session_id": "session-1"})
    secret = await server.call_tool(
        "terminal_write_secret", {"session_id": "session-1", "entry": "net/device-pass"}
    )

    assert written.structured_content["bytes_sent"] == len("ssh operator@192.0.2.10")
    assert "data" not in written.structured_content
    assert read.structured_content["output"] == "output"
    assert secret.structured_content["entry"] == "net/device-pass"
    assert manager.calls == [
        ("terminal_write", ("session-1", "ssh operator@192.0.2.10", True)),
        ("terminal_read", ("session-1", None)),
        ("terminal_write_secret", ("session-1", "net/device-pass")),
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
    assert manager.calls[-3] == ("read_output", ("session-1", None, None))
    assert status.structured_content["state"] == "ready"
    assert closed.structured_content["state"] == "closed"


@pytest.mark.asyncio
async def test_server_ships_usage_instructions_and_resource() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]

    instructions = server.instructions or ""
    assert "terminal_write_secret" in instructions
    assert "authentication error is NOT a permission popup" in instructions
    assert "Do not re-read" in instructions

    resources = await server.list_resources()
    assert any(str(resource.uri) == "network-terminal://usage" for resource in resources)

    contents = await server.read_resource("network-terminal://usage")
    manual = "".join(
        item.content if isinstance(item.content, str) else item.content.decode()
        for item in contents
    )
    assert "terminal_write_secret" in manual
    assert "proxyjump" in manual
    assert "close_session" in manual


@pytest.mark.asyncio
async def test_command_and_change_tools_are_gone() -> None:
    manager = FakeManager()
    server = create_server(manager=manager)  # type: ignore[arg-type]
    gone = (
        "run_command",
        "run_commands",
        "cli_help",
        "send_control",
        "respond",
        "run_change",
    )
    for tool in gone:
        with pytest.raises(ToolError):
            await server.call_tool(tool, {"session_id": "session-1"})
    assert manager.calls == []
