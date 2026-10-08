# network-terminal: operating manual

This server gives you a raw, persistent terminal to network devices. There is no
command gateway, no allowlist, and no per-command approval inside a session: you
type into the device exactly as an engineer would, and you read back whatever
comes. One session is one continuous terminal stream.

## Mental model

- `open_session` opens ONE terminal (SSH, Telnet, TCP console, or local serial)
  and keeps it open until `close_session` or an idle/lifetime timeout.
- After that, everything is `terminal_write` (input) and `terminal_read`
  (output). You decide what to type, including `ssh`/`telnet` to further hosts.
- Several sessions can be open at once (up to `max_open_sessions`). Use one
  `session_id` per device instead of reconnecting.

## Connecting

`open_session` fields:

- `host`, `protocol` (`ssh`/`telnet`/`console`/`serial`), optional `port`.
- `credentials` (required except serial): `{backend: "pass", entry: <pass
  entry>, username: ...}` or `{backend: "ssh_key", key_file: <path>,
  key_passphrase_entry: ...}`. Plaintext (`backend: "plaintext"`) only with
  `allow_plaintext_password=true` and only if the user supplied it; prefer
  `pass` and never invent entries or key paths.
- `route` for the FIRST hop only: `{type: "socks", host, port}` (local SOCKS5,
  target must be an IP) or `{type: "proxyjump", host, port, credentials,
  host_key_policy}` (one SSH bastion). There is no multi-hop route: reach
  further hosts by typing `ssh`/`telnet` inside the session.
- `host_key_policy`: `strict` (default) requires the key in the server's
  `known_hosts`. Use `accept_new` only for the first contact with a host, then
  go back to `strict`. `accept_changed` is only for devices known to rotate
  keys.
- `allow_telnet=true` for Telnet/console; `allow_serial=true` plus an absolute
  `/dev/tty*` device for serial. They are insecure and always warn.
- `legacy` (per-call SSH algorithm allowlists) only if the device needs old
  algorithms; retry the same host with it.

Every `open_session` goes through the client's permission popup. A rejected or
pending popup and an authentication failure are different things: an auth error
means the popup was fine and the credential reference for the named hop is
wrong (see Errors below).

## Core loop

```text
terminal_write(session_id, "display version")   # enter=true appends \n
terminal_read(session_id)                       # output until quiet
terminal_write(session_id, "display interface brief")
terminal_read(session_id)
```

- `terminal_read` returns whatever arrived (banner, prompt, pager screen,
  error) after ~0.5 s of quiet or the `timeout`. It does NOT wait for a prompt
  shape. If a command starts a new connection (e.g. `ssh`), the first read may
  return only the echo; call `terminal_read` again until the expected output
  appears.
- `session_status().prompt` is only the initial best-effort prompt, not a live
  tracker. Inspect the live stream with `terminal_read`.
- `terminal_read` returns only output that arrived since your previous read; it
  never replays what you have already seen. When `truncated=true`, continue
  from `next_output_offset`: `read_output(session_id,
  offset=<next_output_offset>)`, or just call `read_output(session_id)` to read
  on from where you stopped.
- `read_output` without `offset` returns only unseen buffered output; an
  explicit `offset` is a deliberate look at older output. Never re-read history
  you already have: it inflates the context without adding information.
- `enter=false` sends raw bytes: use it for single keys and controls, e.g.
  `terminal_write(session_id, " ", enter=False)` for pager next page,
  `"q"` to quit a pager, `"\u0003"` for Ctrl-C, `"\u0015"` for Ctrl-U.

## Further hops (bastion → device)

```text
open_session(host="127.0.0.1", port=2224,
             route={type: "proxyjump", host="bastion.example.net",
                    credentials={backend: "ssh_key", key_file: "~/.ssh/id_ed25519",
                                 key_passphrase_entry: "net/key-pass", username: "operator"}},
             credentials={backend: "ssh_key", key_file: "~/.ssh/id_ed25519",
                          key_passphrase_entry: "net/key-pass", username: "operator"})
terminal_write(session_id, "ssh operator@192.0.2.10")
terminal_read(session_id)                                  # password prompt
terminal_write_secret(session_id, "net/device-password")   # value from pass
terminal_read(session_id)                                  # device CLI
```

The next hop's host key is checked by the intermediate host's ssh client, not
by this server.

## Secrets

- Every secret typed at a live prompt (device login, nested `ssh`, enable,
  TACACS) MUST go through `terminal_write_secret(session_id, entry)` with a
  `pass` entry name. The value is resolved inside the server, never returned,
  never audited (only the entry name and byte count are logged).
- `terminal_write` is audited verbatim. Never type a password, passphrase, or
  token with it.
- If the user has not named a `pass` entry for a prompt, ask. Do not guess
  entries and do not ask the user to paste the secret into chat.

## Configuration changes

- Configuration commands are ordinary input: `system-view`, `configure
  terminal`, `set ...`, `commit`.
- For changes that can affect reachability, prefer a rollback you can cancel
  (`commit confirmed`, `reload in 10`, `schedule reboot delay`) and verify
  before finalizing. This is your judgment, not a server guarantee.
- The operator may set OpenCode permission `ask` for
  `network-terminal_terminal_write`; if each write needs approval, do not try
  to bypass it.

## Errors and troubleshooting

- `jump host authentication failed ...` / `authentication failed for final
  target ...`: the credential reference for that hop is wrong. Ask the user for
  the correct `pass` entry or key file. The permission popup was not the
  problem.
- Unknown/changed host key: the key is not in the server `known_hosts`. For a
  genuinely new host use `host_key_policy="accept_new"` once, verify the
  fingerprint with the user, then use `strict`.
- `session ... is not ready: failed`: a transport error occurred. Close the
  session and open a new one; do not try to recover it with more writes.
- Read timeout / quiet read with no output: the device may still be working.
  Read again with a larger `timeout`, or page/scroll.
- `maximum number of open sessions reached`: close sessions you no longer need.

## Tool reference

- `open_session` — open one persistent terminal (direct/socks/proxyjump).
- `terminal_write(session_id, data, enter=True)` — send input; audited.
- `terminal_read(session_id, timeout=None)` — read available output.
- `terminal_write_secret(session_id, entry)` — send a `pass` secret; not logged.
- `read_output(session_id, offset=None, limit=None)` — unseen buffered output;
  pass an explicit `offset` only to revisit older output.
- `session_status(session_id)` — state, initial prompt, warnings.
- `close_session(session_id)` — disconnect and drop the session.

Always close sessions you are done with. Summaries must not include real
addresses, credentials, fingerprints, hostnames, or terminal output in source
control.
