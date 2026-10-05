"""Model-facing operating instructions shipped with the MCP server.

The concise contract goes into the MCP ``initialize`` result (OpenCode places
it in the model context). The full manual lives in ``usage.md`` inside this
package and is exposed as the ``network-terminal://usage`` resource.
"""

from __future__ import annotations

from importlib.resources import files

USAGE_URI = "network-terminal://usage"

INSTRUCTIONS = """\
network-terminal gives you a raw persistent terminal to network devices.

- open_session opens ONE terminal (ssh/telnet/console/serial) and keeps it
  open. route (socks/proxyjump) covers only the first hop; reach further hosts
  by typing `ssh`/`telnet` inside the session. Several sessions can stay open.
- Everything after that is terminal_write (input) and terminal_read (output).
  No command allowlists, no per-command server approval. terminal_read returns
  output after a brief quiet period; right after a command that starts a new
  connection (e.g. ssh) it may return only the echo - read again.
- Secrets typed at any live prompt MUST go through
  terminal_write_secret(session_id, <pass entry>). terminal_write is audited
  verbatim; never type passwords, passphrases, or tokens with it, and never
  guess pass entries or key paths - ask the user.
- Single keys and controls: terminal_write(..., enter=false), e.g. " " (pager
  next page), "q", "\\u0003" (Ctrl-C).
- An authentication error is NOT a permission popup problem. Read the message:
  it names the jump host or the final target whose credential is wrong.
- Use host_key_policy=strict (default); accept_new only for the first contact
  with a host. Telnet/console require allow_telnet=true; serial requires
  allow_serial=true and an absolute /dev/tty* path.
- Always close_session when done, including after errors.

Full manual: read the MCP resource network-terminal://usage before complex
work (nested hops, secrets, troubleshooting).
"""
# NOTE: keep this contract in sync with the matching rules in usage.md.


def usage_text() -> str:
    """Return the packaged full manual."""
    return files("network_terminal_mcp").joinpath("usage.md").read_text(encoding="utf-8")
