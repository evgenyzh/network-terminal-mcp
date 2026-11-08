"""Model-facing operating instructions shipped with the MCP server.

The concise contract goes into the MCP ``initialize`` result. The full manual
lives in ``usage.md`` inside this package and is exposed as the
``network-terminal://usage`` resource.
"""

from __future__ import annotations

from importlib.resources import files

USAGE_URI = "network-terminal://usage"

INSTRUCTIONS = """\
Persistent raw terminal: open_session(command) takes `ssh ...`, `telnet ...`, or
`serial DEVICE [BAUDRATE]`; no shell. Use the same session for commands,
pagers, and nested hops. Read startup output; ready only means the local client
is running, not that authentication or the network is healthy.

Reads return only new output. If truncated, continue with read_output without
an offset. A reader_id on read_output has an independent cursor for already
buffered output; it does not collect the transport. Do not re-read history.
Use enter=false for single keys. Send secrets only with terminal_write_secret
(exactly one of value, env, or file); prefer env or file because value remains
in client history.

After sleep or network changes, reopen suspect sessions. A timed-out write may
be partial: verify device state before retrying. Close sessions when done. For
one-shot SSH use the shell. Before complex work, read network-terminal://usage.
"""
# NOTE: keep this contract in sync with the matching rules in usage.md.


def usage_text() -> str:
    """Return the packaged full manual."""
    return files("network_terminal_mcp").joinpath("usage.md").read_text(encoding="utf-8")
