"""Model-facing operating instructions shipped with the MCP server.

The concise contract goes into the MCP ``initialize`` result. The full manual
lives in ``usage.md`` inside this package and is exposed as the
``network-terminal://usage`` resource.
"""

from __future__ import annotations

from importlib.resources import files

USAGE_URI = "network-terminal://usage"

INSTRUCTIONS = """\
network-terminal gives you a persistent raw terminal opened by a real command.

- open_session(command) accepts exactly one command: `ssh ...`, `telnet ...`,
  or `serial DEVICE [BAUDRATE]`. SSH uses the local OpenSSH client, your config,
  agent, keys, ProxyJump, and any other ssh options. No shell is involved.
- Keep using the same session for commands, `?` help, pagers, and further
  `ssh`/`telnet` hops. terminal_write sends input; terminal_read returns only
  NEW output. No prompt shape is required.
- If terminal_read is truncated, continue with read_output(session_id,
  offset=next_output_offset) or just read_output(session_id). Do not re-read
  old output: it wastes context.
- Use terminal_write(..., enter=false) for a single key or control byte, e.g.
  " " for the next pager page, "q" to quit, "\\u0003" for Ctrl-C.
- At any live password/passphrase prompt use terminal_write_secret with exactly
  one of value, env, or file. Never put a password in terminal_write or in the
  connection command. Prefer env or file; a plaintext value stays in the model
  and client history.
- open_session returns ready when the terminal process is running, not when
  authentication or a CLI prompt has completed. Read the live stream.
- If the client process exits during startup, open_session raises the real exit
  code and output. If it exits later, session_status reports closed/failed and
  the exit code. Always close_session when done.
- Do not use this MCP for a simple non-interactive `ssh host 'command'`; the
  ordinary shell is simpler for one-shot commands.

Full manual: read the MCP resource network-terminal://usage before complex
work (nested hops, secrets, serial, troubleshooting).
"""
# NOTE: keep this contract in sync with the matching rules in usage.md.


def usage_text() -> str:
    """Return the packaged full manual."""
    return files("network_terminal_mcp").joinpath("usage.md").read_text(encoding="utf-8")
