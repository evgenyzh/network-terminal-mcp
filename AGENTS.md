# Repository Guide

## Read First

- `README.md` defines the project scope.
- `docs/architecture.md` is the technical source of truth.
- `docs/development-plan.md` lists ordered milestones and exit criteria.
- `docs/security.md` contains mandatory safety requirements.
- `src/network_terminal_mcp/usage.md` is the model-facing manual shipped as MCP
  `instructions` + the `network-terminal://usage` resource; keep it in sync with
  `usage.py` and the OpenCode skill.
- There are no example config files: connections are model-described inline. Never commit real
  addresses, hostnames, or secrets anywhere in the repository.

## Safety Rules

- Never add plaintext passwords, TACACS secrets, enable secrets, API tokens, or real production
  addresses to this repository.
- Never run integration tests against real network devices without explicit user approval for the
  exact targets and commands.
- Sessions are raw interactive terminals: the model may type any command, including configuration
  entry. The safety boundary is the client permission gate on `open_session` (and optionally on
  `terminal_write`), the audited input, and the operator's AAA policy — not command allowlists.
- Secrets typed at a live prompt must go through `terminal_write_secret` (a `pass` entry reference);
  its value must never appear in tool arguments, results, or audit. Plain `terminal_write` is
  logged verbatim and must not be used for secrets.
- Keep legacy SSH algorithms scoped to explicit per-call host allowlists. Never weaken global
  SSH settings.
- Telnet, TCP console, and local serial must be explicitly enabled per call (`allow_telnet=true`,
  `allow_serial=true`) and clearly marked insecure; policy can hard-deny them.
- Audit failures are fail-closed: if an action cannot be recorded, it must not be executed.
- Secret retrieval is internal to the server. MCP tool arguments and results must not contain
  passwords, except an explicit plaintext opt-in behind `allow_plaintext_password`, which is never
  stored, never audited, and always marked insecure. References (`pass` entry, key file) are the
  default.

## Engineering Rules

- Use Python 3.12+ and `uv`.
- Terminal transport is implemented directly on Paramiko (SSH), telnetlib3
  (Telnet/TCP console), and pyserial (local serial) in
  `src/network_terminal_mcp/terminal.py`. Do not reintroduce a vendor-driver
  abstraction or nested connection routes; the model identifies the device type
  from output and performs further ssh/telnet hops itself with `terminal_write`.
- Keep command knowledge out of the transport layer. The transport handles
  writes, reads, prompt detection, and timeouts only.
- Validate the connection spec and policy with Pydantic before opening any
  network connection.
- Use structured errors and redact secrets before logging or returning failures.

## Planned Verification

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

Add these checks as the corresponding implementation appears. Do not add placeholder tests that
assert no behavior.
