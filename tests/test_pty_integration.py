"""End-to-end PTY tests using fake ssh/telnet executables."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from network_terminal_mcp.errors import TransportError
from network_terminal_mcp.sessions import SessionManager, SessionState
from network_terminal_mcp.sessions.manager import RuntimeLimits

_RESIDENT_CLIENT = """\
#!{python}
import sys

print("banner", flush=True)
for line in sys.stdin:
    line = line.rstrip("\\r\\n")
    if line == "quit":
        break
    print("got:" + line, flush=True)
print("bye", flush=True)
"""

_PASSWORD_CLIENT = """\
#!{python}
import sys

print("Password: ", end="", flush=True)
line = sys.stdin.readline().rstrip("\\r\\n")
if line != "s3cr3t":
    print("denied", flush=True)
    sys.exit(1)
print("welcome", flush=True)
for line in sys.stdin:
    line = line.rstrip("\\r\\n")
    if line == "quit":
        break
    print("got:" + line, flush=True)
"""

_EARLY_EXIT = """\
#!{python}
import sys

print("Host key verification failed.", file=sys.stderr, flush=True)
sys.exit(255)
"""


class FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


def _install_client(tmp_path: Path, name: str, source: str) -> None:
    path = tmp_path / name
    path.write_text(source.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)


def _manager(audit: FakeAudit | None = None) -> SessionManager:
    return SessionManager(
        limits=RuntimeLimits(
            io_timeout=2.0,
            max_read_timeout=10.0,
            max_write_bytes=4096,
            max_inline_output_bytes=4096,
            max_session_buffer_bytes=8192,
            max_open_sessions=10,
            read_quiet_seconds=0.05,
            startup_timeout=2.0,
            startup_quiet_seconds=0.05,
            startup_output_tail_bytes=4096,
        ),
        audit_logger=audit or FakeAudit(),
    )


@pytest.mark.parametrize(
    ("client", "command"),
    [
        ("ssh", "ssh operator@192.0.2.10"),
        ("telnet", "telnet 192.0.2.30 23"),
    ],
)
def test_fake_client_supports_persistent_terminal_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: str,
    command: str,
) -> None:
    _install_client(tmp_path, client, _RESIDENT_CLIENT)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    manager = _manager()
    try:
        info = manager.open_session(command)
        assert info.state is SessionState.READY
        assert info.command == command
        assert manager.terminal_read(info.session_id).output == "banner\n"

        manager.terminal_write(info.session_id, "show version")
        assert manager.terminal_read(info.session_id).output == "got:show version\n"

        manager.terminal_write(info.session_id, "show clock")
        assert manager.terminal_read(info.session_id).output == "got:show clock\n"

        manager.terminal_write(info.session_id, "quit")
        assert manager.terminal_read(info.session_id).output == "bye\n"
        status = manager.session_status(info.session_id)
        assert status.state is SessionState.CLOSED
        assert status.exit_code == 0
    finally:
        manager.shutdown()


def test_fake_client_reports_early_exit_with_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(tmp_path, "ssh", _EARLY_EXIT)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    manager = _manager()
    try:
        with pytest.raises(TransportError) as error:
            manager.open_session("ssh operator@192.0.2.10")
        assert "exited with code 255 during startup" in str(error.value)
        assert "Host key verification failed." in str(error.value)
    finally:
        manager.shutdown()


def test_secret_tool_handles_a_live_password_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(tmp_path, "ssh", _PASSWORD_CLIENT)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    audit = FakeAudit()
    manager = _manager(audit)
    try:
        info = manager.open_session("ssh operator@192.0.2.10")
        assert manager.terminal_read(info.session_id).output == "Password: "

        result = manager.terminal_write_secret(info.session_id, value="s3cr3t")
        assert result.source == "value"
        assert manager.terminal_read(info.session_id).output == "welcome\n"
        assert "s3cr3t" not in json.dumps(audit.records)

        manager.terminal_write(info.session_id, "quit")
        manager.terminal_read(info.session_id)
    finally:
        manager.shutdown()
