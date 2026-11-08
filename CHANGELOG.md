# Changelog

Все значимые изменения проекта фиксируются в этом файле. Формат основан на
[Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии следуют
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-11-08

### Fixed

- Corrected the current documentation link label in the PyPI README.

## [0.3.0] - 2026-11-08

Breaking release: MCP output schemas changed. Clients that use fields removed
from 0.2.1 responses must be updated; use `session_status` for full metadata.

### Changed

- MCP tool responses are compact: opening and closing return essential state,
  writes return `ok`, and reads omit redundant fields. Warnings, abnormal
  states, errors, and continuation or buffer-eviction information remain visible;
  full session metadata is available via `session_status`. This changes the
  MCP output schemas and may require updates to clients relying on 0.2.1 fields.
- Shortened the always-present MCP instructions; the full manual remains a
  resource. The 65,536-byte per-read limit and cursor behavior are unchanged.

## [0.2.1] - 2026-10-31

### Added

- `SessionInfo.last_output_at` and `SessionInfo.idle_seconds`, so a client can
  distinguish a quiet device from a session that has never produced output.
- Bounded PTY writes with a 10-second `write_timeout`; a blocked client now
  returns `transport_error` instead of holding the session lock indefinitely.

### Changed

- MCP instructions, the packaged manual, the OpenCode skill, and current docs
  describe SSH keepalive options and the post-suspend/reconnect workflow.
- A write timeout marks the session `failed`, but `close_session` remains
  available and closes the child process.

### Fixed

- A PTY write to a client that stopped reading can no longer block the session
  forever.

## [0.2.0] - 2026-10-31

Breaking release: structured connection descriptions, local policy, `pass`
integration, and the old transport stack are removed. `open_session` now takes
one real connection command.

### Added

- Command-only `open_session(command)` for `ssh`, `telnet`, and
  `serial DEVICE [BAUDRATE]`.
- `PtyTerminal` running the real OpenSSH/Telnet client in a POSIX PTY, including
  user config, agent, keys, ProxyJump, ProxyCommand, and arbitrary client
  options.
- Short local serial form with pyserial 8N1 and configurable baudrate.
- `terminal_write_secret` sources `value`, `env`, and `file`, with exactly one
  source per call.
- Startup output in the cursor buffer, so the first `terminal_read` returns the
  banner or password prompt.
- Process exit tracking with `exit_code`, saved `error`, and `closed`/`failed`
  states.
- `network-terminal-mcp check` validates the real runtime prerequisites.
- Strict generated tool schemas reject removed structured fields.

### Changed

- Expected `NetworkMCPError` failures cross the MCP boundary as `ToolError`;
  the model sees the real code and message instead of a generic tool error.
- Sessions live until `close_session`, natural process exit, or server
  shutdown; automatic idle/lifetime expiry is removed.
- Closed sessions remain available through `session_status`.
- Documentation is split into `docs/current/` and `docs/history/`; the completed
  redesign plan is historical.
- OpenCode documentation recommends `codemode: false` and no mandatory
  permission popup.
- CLI startup remains via an installed binary (`uv tool install
  network-terminal-mcp`) instead of `uvx @latest`, avoiding startup network
  resolution and uv cache contention.

### Removed

- Structured `open_session` fields: `host`, `port`, `protocol`, `credentials`,
  `route`, `host_key_policy`, `legacy`, `allow_telnet`,
  `allow_plaintext_password`, `allow_serial`, and `serial`.
- Paramiko, telnetlib3, custom SOCKS5 routing, ProxyJump implementation,
  connection plans, credential resolver, host-key store, inventory, and
  `policy.yml`.
- Mandatory `pass` integration and the `terminal_write_secret(entry=...)` form.
- Mandatory client permission gate for `open_session`.
- Idle/lifetime session limits and per-call insecure-mode flags.
- YAML, pydantic-settings, PyYAML, Paramiko, and telnetlib3 dependencies.

### Fixed

- Real connection and I/O errors are no longer replaced by
  `Error executing tool ...` without a cause.
- Early client exit during startup now reports its exit code and stderr/stdout.
- Re-reading old output cannot duplicate history in model context.

## [0.1.2] - 2026-10-08

### Fixed

- `read_output` без `offset` больше не перечитывает весь буфер с начала:
  он продолжает с курсора чтения и возвращает только непрочитанный вывод.
  Повторные вызовы не могут продублировать историю в контексте модели.
- При `truncated=true` у `terminal_read` поле `next_output_offset` теперь
  указывает на первый непрочитанный символ, а не на конец всего вывода; добор
  хвоста больше не требует повторного чтения с нуля.

### Changed

- `terminal_write` больше не возвращает отправленный текст в результате
  (эхо ввода убрано из контекста; в audit ввод по-прежнему записывается).
- `read_output` теперь аудируется (offset, bytes, cursor) для диагностики
  «перечитывания истории».
- Мануал и MCP instructions явно запрещают повторное чтение уже виденного
  вывода.
- В README и примере конфигурации OpenCode рекомендован запуск через
  `uvx network-terminal-mcp@latest`, чтобы клиент подхватывал новые релизы.

## [0.1.1] - 2026-10-05

### Fixed

- Консольный скрипт `network-terminal-mcp` теперь выполняет подкоманду
  `check` (`uvx network-terminal-mcp check`); раньше аргумент игнорировался и
  молча запускался stdio-сервер. Без подкоманды, как и прежде, запускается
  MCP-сервер.

## [0.1.0] - 2026-10-05

Первый публичный релиз. Сессия — один постоянный терминальный stream; модель
пишет в него точный ввод, включая переходы `ssh`/`telnet`, и читает вывод без
требования определённой формы prompt.

### Added

- Сырые терминальные инструменты: `open_session`, `terminal_write`,
  `terminal_read`, `terminal_write_secret`, `read_output`, `session_status`,
  `close_session`.
- `terminal_write_secret` вводит значение `pass` в живой prompt, не раскрывая
  его в аргументах, результатах и audit (логируются только entry и размер).
- Транспорты SSH (Paramiko), Telnet/TCP console (telnetlib3) и локальный
  serial `/dev/tty*` (pyserial); Telnet/console/serial включаются явными
  per-call флагами `allow_telnet` / `allow_serial`.
- Маршруты первого подключения: direct, локальный SOCKS5 (`socks`) и SSH
  ProxyJump (`proxyjump`).
- Модель-ориентированная инструкция внутри MCP: `instructions` в initialize и
  ресурс `network-terminal://usage` (исходник
  `src/network_terminal_mcp/usage.md`).
- Необязательный `policy.yml` (posture и лимиты), per-call allowlist legacy
  SSH-алгоритмов, JSONL-audit и mutable redaction.
- Лимиты runtime: idle/lifetime сессий, размеры чтения/записи/буфера вывода,
  максимальное число одновременных сессий.

### Changed

- Сессии: несколько независимых сессий в одном процессе, вывод отдаётся без
  требования prompt, prompt detection — best-effort.
- Секреты: ссылки `pass`/key file по умолчанию; plaintext-пароль — только за
  явным `allow_plaintext_password` и помечается как insecure.
- Документация переписана под raw terminal; история этапов 0-8 вынесена в
  `docs/history.md`.

### Removed

- Командно-ориентированные инструменты `run_command`, `run_commands`,
  `cli_help`, `send_control`, `respond`, `run_change` и command policy.
- `nested`-маршрут: вложенные переходы выполняются вводом в сессии.
- Инвентарь и файлы `connections.yml`, `credentials.yml`, `inventory.yml`.
