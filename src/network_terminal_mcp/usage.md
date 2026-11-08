# network-terminal: operating manual

This server gives you a persistent raw terminal. You open it with the same
connection command an engineer would type, then read and write the live stream.
There is no command gateway, no inventory, and no mandatory configuration file.

## Mental model

- `open_session(command)` opens one terminal and keeps it open.
- `terminal_write` sends input; `terminal_read` returns new output.
- A session is one continuous terminal stream. The server does not parse device
  commands and does not wait for a prompt shape.
- Several sessions can be open at once (up to 10). Close sessions you no longer
  need.

## Opening a session

The only argument is `command`:

```text
ssh operator@192.0.2.10
ssh -J operator@bastion.example.net operator@192.0.2.10
ssh -i ~/.ssh/id_ed25519 -p 2222 operator@192.0.2.20
telnet 192.0.2.30 23
serial /dev/ttyUSB0 115200
```

- The command is split like a POSIX shell command line, but no shell executes
  it. Shell operators such as `|`, `&&`, redirection, and substitutions are not
  interpreted by this server.
- SSH and Telnet run as normal system clients in a PTY. SSH therefore uses your
  `~/.ssh/config`, aliases, agent, keys, `ProxyJump`, `ProxyCommand`, algorithm
  options, and host-key rules. Arguments are passed to the client unchanged.
- Telnet and TCP console use the system `telnet` client. A console server on a
  non-standard port is just `telnet HOST PORT`.
- Serial uses `serial DEVICE [BAUDRATE]`, defaults to 9600 baud, and runs 8N1
  through pyserial. The device must be an accessible character device.
- `ready` means the process was started and the terminal can accept input. It
  does not mean authentication finished or a CLI prompt exists. Read the
  startup banner and prompts with `terminal_read`.
- If the client exits during the short startup window, `open_session` returns
  an error containing its exit code and output. If it stays alive, output is
  buffered for your first read.

For a simple one-shot `ssh host 'command'`, use the ordinary shell instead of
this MCP. This server is for interactive state that must survive between calls.

## Core loop

```text
terminal_write(session_id, "show version")   # enter=true appends \n
terminal_read(session_id)                    # output until quiet or timeout
terminal_write(session_id, "show interface brief")
terminal_read(session_id)
```

- `terminal_read` returns whatever arrived after the previous read: banner,
  password prompt, pager screen, echo, or command output. A command that starts
  another connection may produce only the echo on the first read; read again.
- If no output arrives, `terminal_read` can wait until `timeout` expires. Pass a
  larger timeout for slow commands, or read again later.
- `terminal_write(..., enter=false)` sends exact text without a line terminator.
  Use it for single keys and controls: `" "` for the next pager page, `"q"` to
  quit, `"\u0003"` for Ctrl-C, `"\u0015"` for Ctrl-U.
- The line terminator is `\n` for SSH/Telnet and `\r` for serial.
- Successful writes return `ok=true`; a `state` field is included if the client
  is no longer ready after the write. This confirms local delivery only, not
  execution by the device. Check `session_status` for diagnostic details.

## Unstable connections

- `ready` describes the local client process, not the remote side. A local
  `ssh` or `telnet` process can stay alive while the network path is gone.
- OpenSSH keepalive is disabled by default. For links that may go silent, add
  explicit liveness options when opening the session:

  ```text
  ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 operator@192.0.2.10
  ```

  The client then exits after roughly 45 seconds without a response, and the
  next `session_status` or `terminal_read` reports `failed`.
- Telnet has no MCP-level keepalive. If a Telnet session may have lost its
  connection, close it and open a new one.
- After laptop sleep, a Wi-Fi/VPN change, or a long pause, treat every open
  session as suspect. A write into a half-open TCP connection can appear to
  succeed while the data never reaches the device. Close and reopen, then run a
  harmless verification command.
- `terminal_read` is bounded by `timeout` (default 60 seconds, maximum 300).
  Empty output means no output arrived, not that the peer is healthy. Use a
  short timeout such as 5 seconds for an interactive liveness check.
- `terminal_write` is bounded to 10 seconds. If the client stops reading input,
  the call returns `transport_error: terminal write timed out...` and the
  session becomes `failed`; `close_session` still works. Part of the input may
  already have been sent, so verify device state before retrying a command.
- `session_status` includes `last_output_at` and `idle_seconds`. A large idle
  time is normal for an idle device; it is a hint, not proof of failure.
- There is no automatic idle timeout. Close sessions you no longer use; stale
  `ready` sessions count toward the limit of 10.

## Reading without repeating history

- `terminal_read` returns only output that arrived since the previous read.
- When `truncated=true`, the model has seen only the returned prefix. Continue
  from `next_output_offset` with `read_output(session_id,
  offset=next_output_offset)` or from the read cursor with
  `read_output(session_id)`.
- Normal replies omit `truncated` and continuation offsets. A `read_output`
  reply includes `next_offset` when more buffered output remains; its
  `truncated` and `oldest_offset` indicate that earlier history was evicted.
  `terminal_read` still returns up to 65,536 bytes per call.
- `read_output` without `offset` returns only unseen buffered output. An explicit
  `offset` is a deliberate random access into older output.
- Pass a stable `reader_id` to `read_output` for an independent named cursor;
  this reads only output already collected by `terminal_read`, not directly
  from the transport. The session retains up to 32 named readers. If a reader
  falls behind buffer eviction, the error reports the oldest available offset;
  explicitly pass an available offset to resume after acknowledging the gap.
- Never re-read history you already have: it wastes context without adding
  information.

## Further hops

A session can contain another SSH or Telnet connection:

```text
open_session("ssh operator@bastion.example.net")
terminal_write(session_id, "ssh operator@192.0.2.10")
terminal_read(session_id)                                  # password prompt
terminal_write_secret(session_id, env="DEVICE_PASSWORD")
terminal_read(session_id)                                  # device CLI
```

The next hop is handled by the client running on the intermediate host. Its
host-key and authentication rules are that host's rules, not this server's.

## Secrets

`terminal_write_secret` accepts exactly one source:

```json
{"session_id": "...", "value": "secret"}
```

```json
{"session_id": "...", "env": "DEVICE_PASSWORD"}
```

```json
{"session_id": "...", "file": "/run/secrets/device-password"}
```

- `value` uses the string directly. The string is already in the tool call,
  model context, and client history; the server does not return it, but it
  cannot erase that history.
- `env` reads a variable from the MCP server process environment.
- `file` reads a local file and removes one final CR/LF.
- Empty values, missing variables, unreadable files, and multiple sources are
  errors.
- The value is added to the session redactor. It is never returned in the tool
  result or written to audit; only the source kind and byte count are logged.
- Prefer `env` or `file`. Use `value` only as the simple fallback when the user
  has supplied a secret and no safer source is available.
- A secret typed with ordinary `terminal_write`, or embedded in `command`, can
  remain in client history and audit. Do not do that.

## Sessions and errors

- `session_status` returns the original command, state, timestamps,
  `last_output_at`, `idle_seconds`, warnings, exit code, and saved error.
- `open_session` returns the session ID and state, plus any warnings or error;
  `close_session` returns the state, plus a nonzero exit code or saved error.
  Use `session_status` when the full metadata is needed.
- `close_session` terminates the client and child process group and retains the
  final status for inspection.
- A natural process exit sets `closed` for code 0 and `failed` with the exit
  code otherwise. Remaining output can still be read with `terminal_read` or
  `read_output`.
- Writing to a closed or failed session returns a specific state error.
- `executable_not_found` means the system client is not installed. Install
  OpenSSH or Telnet, or use another connection command.
- `transport_error` includes the real client output and exit code where
  available. Read the message instead of assuming a permission popup failed.
- Unknown session IDs and invalid offsets return `session_error`.
- `maximum number of open sessions reached` means close a session first.
- `serial device ... is not accessible` / `not a character device` describes a
  local device problem; check the path and permissions.

## Configuration changes

Configuration commands are ordinary terminal input (`configure terminal`,
`system-view`, `set ...`, `commit`). The server does not filter them. For
changes that can affect reachability, prefer a rollback you can cancel
(`commit confirmed`, `reload in 10`, `schedule reboot delay`) and verify before
finalizing; that judgment belongs to you, not to the transport.

## Tool reference

- `open_session(command)` — start one persistent terminal.
- `terminal_write(session_id, data, enter=True)` — send input; audited after
  redaction.
- `terminal_read(session_id, timeout=None)` — read new output until quiet.
- `terminal_write_secret(session_id, value|env|file)` — send one secret from
  exactly one source.
- `read_output(session_id, offset=None, limit=None)` — unseen buffered output;
  use an explicit offset only to revisit old output.
- `session_status(session_id)` — state, command, timestamps, last output, idle
  seconds, warnings, exit code, error.
- `close_session(session_id)` — close the terminal and child process.

Always close sessions you are done with. Do not include real addresses,
credentials, fingerprints, hostnames, or terminal transcripts in source
control.
