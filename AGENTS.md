# Repository Guide

## Read First

- `README.md` defines the project scope.
- `docs/architecture.md` is the technical source of truth.
- `docs/development-plan.md` lists ordered milestones and exit criteria.
- `docs/security.md` contains mandatory safety requirements.
- Files under `config/` are examples only and must never contain real addresses or secrets.

## Safety Rules

- Never add plaintext passwords, TACACS secrets, enable secrets, API tokens, or real production
  addresses to this repository.
- Never run integration tests against real network devices without explicit user approval for the
  exact targets and commands.
- Read-only diagnostics are the initial scope. Do not expose configuration writes through a generic
  command tool.
- Keep legacy SSH algorithms scoped to explicit hosts or connection profiles. Never weaken global
  SSH settings.
- Telnet must be explicitly enabled per device or connection profile and clearly marked insecure.
- Audit failures are fail-closed: if an action cannot be recorded, it must not be executed.
- Secret retrieval is internal to the server. MCP tool arguments and results must not contain
  passwords.

## Engineering Rules

- Use Python 3.12+ and `uv`.
- Terminal transport is implemented directly on Paramiko (SSH) and telnetlib3
  (Telnet/console) in `src/network_terminal_mcp/terminal.py`. Do not reintroduce
  a vendor-driver abstraction; the model identifies the device type from output.
- Keep command knowledge out of the transport layer. The transport handles
  writes, reads, prompt detection, and timeouts only.
- Validate configuration with Pydantic before opening any network connection.
- Use structured errors and redact secrets before logging or returning failures.

## Planned Verification

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

Add these checks as the corresponding implementation appears. Do not add placeholder tests that
assert no behavior.
