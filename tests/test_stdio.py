"""Black-box stdio smoke test for real model-visible error messages."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path


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
