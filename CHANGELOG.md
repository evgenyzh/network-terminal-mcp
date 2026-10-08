# Changelog

Все значимые изменения проекта фиксируются в этом файле. Формат основан на
[Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии следуют
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Рекомендуемый способ запуска MCP-клиентом — `uv tool install
  network-terminal-mcp` и полный путь к бинарю вместо `uvx @latest`: при
  старте исчезают сетевой резолвинг и блокировка общего кэша uv, которые давали
  задержку «connecting» и таймауты при нескольких окнах.

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
