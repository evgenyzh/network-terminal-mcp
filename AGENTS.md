# Repository Guide

## Read First

- `README.md` gives the project overview and release status.
- `docs/README.md` is the single documentation index and explains document
  statuses.
- `docs/current/architecture.md` is the technical source of truth for current
  0.2.1 behavior.
- `docs/current/security.md` defines the current trust model and secret rules.
- `src/network_terminal_mcp/usage.md` is the model-facing manual shipped as MCP
  `instructions` + the `network-terminal://usage` resource; keep it in sync with
  `usage.py` and the OpenCode skill.

## Documentation Structure

- `docs/current/`: documentation for the released implementation.
- `docs/plans/`: only approved target specifications and the post-release
  backlog.
- `docs/history/`: historical stages and completed plans only, never current
  operating guidance.
- Keep one authoritative description per topic. Do not recreate overlapping
  configuration, adapter, or development-plan documents.
- On release, move the completed plan to `docs/history/` and describe only
  actual behavior in `docs/current/`.

## Safety Rules

- Never add plaintext passwords, TACACS secrets, enable secrets, API tokens,
  private keys, real production addresses, hostnames, usernames, fingerprints,
  or production transcripts to this repository. Examples use `192.0.2.0/24` and
  `example.net`.
- Never run integration tests against real network devices without explicit
  user approval for the exact targets and commands.
- Audit failures are fail-closed: if an action cannot be recorded, it must not
  be executed.
- Values sent through `terminal_write_secret` must never appear in tool results
  or audit. They may be visible in model context when the caller deliberately
  uses the new `value` source; this is documented behavior, not a hidden leak.
- Keep the output redactor effective for secrets already registered in a
  session.

## Engineering Rules

- Use Python 3.12+ and `uv`; target POSIX/Linux.
- `open_session` accepts only `command`. Do not retain or recreate structured
  connection fields, connection plans, inventory, policy gates, credential
  profiles, a custom host-key store, or nested route models.
- SSH and Telnet run as real clients in a POSIX PTY. Serial uses pyserial via
  `serial DEVICE [BAUDRATE]`. Do not reintroduce Paramiko/telnetlib3 or a
  vendor-driver abstraction.
- Sessions are persistent raw terminals. Transport code handles process/device
  I/O, lifecycle, and timeouts only; command knowledge belongs to the model.
- PTY writes must stay bounded. Keep `write_timeout` and the non-blocking write
  loop; do not reintroduce an unbounded `ptyprocess.write` that can hold the
  session lock forever.
- `SessionInfo.last_output_at` and `idle_seconds` describe output arrival, not
  read activity. Keep them observable and do not turn them into an automatic
  failure or idle-kill signal.
- Keep cursor-based output: a default read returns only unseen output and must
  not replay history into model context. The first read must include a startup
  banner stored by `open_session`.
- Convert expected `NetworkMCPError` failures to `ToolError` with actionable,
  redacted messages. Never replace them with a generic tool error.
- Generated tool argument schemas must reject unknown fields. The removed
  structured connection fields must not be silently accepted.
- There is no mandatory configuration, `pass` integration, or `open_session`
  permission gate. The server is a trusted local component, not a sandbox.
- Do not add a one-shot `run_command` gateway: ordinary shell SSH remains the
  simpler tool for non-interactive one-off commands.
- Do not add new restrictions unless a concrete operational need is documented.

## Verification

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

Add and update meaningful tests with implementation changes. Use fake terminal
clients for PTY tests; no placeholder tests and no real production identifiers.
