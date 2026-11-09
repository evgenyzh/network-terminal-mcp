"""End-to-end PTY tests using fake ssh/telnet executables."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from network_terminal_mcp.errors import AuditError, TransportError
from network_terminal_mcp.sessions import SessionManager, SessionState
from network_terminal_mcp.sessions.manager import RuntimeLimits
from network_terminal_mcp.terminal import PtyTerminal

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

_DSR_CLIENT = """\
#!{python}
import os
import select
import sys
import tty

tty.setraw(sys.stdin.fileno())
os.write(1, b"prompt: \\x1b[4;7H\\x1b[6n")
def read_until(suffix):
    result = b""
    while not result.endswith(suffix):
        if not select.select([0], [], [], 1.5)[0]:
            os.write(1, b"TIMEOUT")
            sys.exit(1)
        result += os.read(0, 1)
    return result

if read_until(b"R") != b"\\x1b[4;7R":
    os.write(1, b"BAD FIRST REPORT")
    sys.exit(1)
os.write(1, b"\\x1b[s\\x1b[?25l\\x1b[9999;9999H\\x1b[6n")
if read_until(b"R") != b"\\x1b[24;120R":
    os.write(1, b"BAD SIZE REPORT")
    sys.exit(1)
os.write(1, b"\\x1b[uREADY\\r\\n")
if read_until(b"\\n") == b"show version\\n":
    os.write(1, b"VERSION OK\\r\\n")
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


def test_fake_ssh_waits_for_cursor_reports_before_accepting_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_client(tmp_path, "ssh", _DSR_CLIENT)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NETWORK_TERMINAL_DSR", "1")
    audit = FakeAudit()
    manager = _manager(audit)
    try:
        info = manager.open_session("ssh operator@192.0.2.10")
        output = manager.terminal_read(info.session_id).output
        assert "READY" in output
        manager.terminal_write(info.session_id, "show version")
        assert "VERSION OK" in manager.terminal_read(info.session_id).output
        assert [record["outcome"] for record in audit.records
                if record["event"] == "terminal_dsr_reply"] == [
                    "started", "completed", "started", "completed"
                ]
    finally:
        manager.shutdown()


def test_dsr_is_opt_in_and_diagnostics_show_escape_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_client(tmp_path, "ssh", _DSR_CLIENT)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("NETWORK_TERMINAL_DSR", raising=False)
    monkeypatch.setenv("NETWORK_TERMINAL_SHOW_CONTROLS", "1")
    audit = FakeAudit()
    manager = _manager(audit)
    try:
        info = manager.open_session("ssh operator@192.0.2.10")
        output = manager.terminal_read(info.session_id).output
        assert "\\x1b[6n" in output
        assert "READY" not in output
        assert not any(record["event"] == "terminal_dsr_reply" for record in audit.records)
    finally:
        manager.shutdown()


def test_dsr_reply_is_not_sent_if_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_client(tmp_path, "ssh", _DSR_CLIENT)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NETWORK_TERMINAL_DSR", "1")

    class FailingAudit(FakeAudit):
        def write(self, record: dict[str, object]) -> None:
            if record["event"] == "terminal_dsr_reply":
                raise AuditError("test audit failure")
            super().write(record)

    writes: list[str] = []
    monkeypatch.setattr(PtyTerminal, "write_channel", lambda _self, data: writes.append(data))
    manager = _manager(FailingAudit())
    try:
        with pytest.raises(AuditError, match="test audit failure"):
            manager.open_session("ssh operator@192.0.2.10")
        assert writes == []
    finally:
        manager.shutdown()
