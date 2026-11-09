"""Black-box stdio smoke test for real model-visible error messages."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _message(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def test_expected_error_is_visible_over_stdio(tmp_path: Path) -> None:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "session_status",
                "arguments": {"session_id": "missing-session"},
            },
        },
    ]
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    process = subprocess.Popen(
        [sys.executable, "-m", "network_terminal_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write("\n".join(_message(item) for item in messages) + "\n")
    process.stdin.flush()

    responses: list[dict[str, object]] = []
    while not any(item.get("id") == 2 for item in responses):
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "MCP server did not answer the tool call"
        line = process.stdout.readline()
        assert line, "MCP server closed stdout before answering"
        payload = json.loads(line)
        if isinstance(payload, dict):
            responses.append(payload)

    process.stdin.close()
    process.wait(timeout=10)
    assert process.returncode == 0, process.stderr.read()
    result = next(item for item in responses if item.get("id") == 2)["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert text == (
        "Error executing tool session_status: "
        "session_error: unknown session missing-session"
    )
    assert "Error executing tool session_status" in text
    assert "unknown session missing-session" in text


@pytest.mark.asyncio
async def test_compact_responses_over_stdio(tmp_path: Path) -> None:
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('banner', flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('response:' + line.strip(), flush=True)\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "network_terminal_mcp"], env=env
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            opened = await session.call_tool("open_session", {"command": "ssh example.net"})
            assert opened.structured_content is not None
            session_id = opened.structured_content["session_id"]
            assert set(opened.structured_content) == {"session_id", "state"}
            first = await session.call_tool("terminal_read", {"session_id": session_id})
            assert first.structured_content == {"output": "banner\n"}
            assert json.loads(first.content[0].text) == {"output": "banner\n"}
            independent = await session.call_tool(
                "read_output", {"session_id": session_id, "reader_id": "observer"}
            )
            assert independent.structured_content == {"output": "banner\n"}
            written = await session.call_tool(
                "terminal_write", {"session_id": session_id, "data": "hello"}
            )
            assert written.structured_content == {"ok": True}
            read = await session.call_tool("terminal_read", {"session_id": session_id})
            assert read.structured_content == {"output": "response:hello\n"}
            empty = await session.call_tool("read_output", {"session_id": session_id})
            assert empty.structured_content == {"output": ""}
            closed = await session.call_tool("close_session", {"session_id": session_id})
            assert closed.structured_content["state"] == "closed"


@pytest.mark.asyncio
async def test_dsr_reports_over_stdio(tmp_path: Path) -> None:
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'\\x1b[6n')\n"
        "reply = b''\n"
        "while not reply.endswith(b'R'):\n"
        "    reply += os.read(0, 1)\n"
        "if reply != b'\\x1b[1;1R': sys.exit(1)\n"
        "os.write(1, b'READY')\n"
        "line = b''\n"
        "while not line.endswith(b'\\r'):\n"
        "    line += os.read(0, 1)\n"
        "os.write(1, b'RESULT')\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    env.pop("NETWORK_TERMINAL_DSR", None)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "network_terminal_mcp"], env=env
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            opened = await session.call_tool("open_session", {"command": "ssh example.net"})
            assert opened.structured_content is not None
            session_id = opened.structured_content["session_id"]
            first = await session.call_tool("terminal_read", {"session_id": session_id})
            assert first.structured_content == {"output": "READY"}
            await session.call_tool("terminal_write", {"session_id": session_id, "data": "show"})
            result = await session.call_tool("terminal_read", {"session_id": session_id})
            assert result.structured_content is not None
            assert result.structured_content["output"] == "RESULT"
            await session.call_tool("close_session", {"session_id": session_id})
