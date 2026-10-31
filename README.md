# network-terminal-mcp

[![PyPI](https://img.shields.io/pypi/v/network-terminal-mcp)](https://pypi.org/project/network-terminal-mcp/)
[![CI](https://github.com/evgenyzh/network-terminal-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/evgenyzh/network-terminal-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Локальный MCP-сервер для постоянных интерактивных терминалов. Модель открывает
сессию привычной командой `ssh`, `telnet` или `serial`, затем пишет в живой
поток команды, `?`-подсказки, pager keys и вложенные `ssh`, а читает только
новый вывод.

Статус: **0.2.1** — command-only API, настоящий OpenSSH/Telnet в POSIX PTY,
локальный serial, конкретные ошибки, ограниченная по времени запись, метрики
простоя и секреты без обязательного `pass`.

## Открытие сессии

Единственный аргумент:

```json
{"command": "ssh -J operator@bastion.example.net operator@192.0.2.10"}
```

```json
{"command": "telnet 192.0.2.30 23"}
```

```json
{"command": "serial /dev/ttyUSB0 115200"}
```

SSH использует локальные `~/.ssh/config`, agent, ключи, ProxyJump, ProxyCommand
и любые опции OpenSSH. Аргументы передаются клиенту без фильтрации; shell не
используется. `ready` означает, что процесс запущен, а не что логин завершён:
banner и prompt читаются через `terminal_read`.

## Основной цикл

```text
open_session("ssh operator@192.0.2.10")
terminal_read(session_id)                    # banner / login prompt
terminal_write(session_id, "show version")
terminal_read(session_id)
terminal_write(session_id, "show interface brief")
terminal_read(session_id)
close_session(session_id)
```

- `terminal_read` возвращает только новый вывод и не повторяет историю.
- При `truncated=true` продолжайте с `next_output_offset` через `read_output`.
- `terminal_write(..., enter=false)` отправляет одиночные клавиши и control
  bytes.
- Пароли и passphrase вводятся только через `terminal_write_secret` с одним
  источником: `value`, `env` или `file`.

## Нестабильные соединения

`ready` означает, что жив локальный процесс клиента, а не что удалённая
сторона отвечает. Для SSH добавляйте контроль связи:

```text
ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 operator@192.0.2.10
```

После сна ноутбука, смены Wi-Fi/VPN или долгой паузы закройте и переоткройте
подозрительные сессии: запись в полуразрушенное TCP-соединение может выглядеть
успешной. `terminal_read` ограничен timeout, `terminal_write` — 10 секундами;
`session_status` показывает `last_output_at` и `idle_seconds`. Подробнее:
[эксплуатация](docs/current/operations.md#нестабильные-соединения).

Для одноразового `ssh host 'command'` обычный shell остаётся проще. MCP нужен
для живого терминала, pager, нескольких команд с сохранением состояния,
password prompts, вложенных переходов и Serial.

## Установка

```bash
uv tool install network-terminal-mcp
network-terminal-mcp check
```

Обновление:

```bash
uv tool upgrade network-terminal-mcp
```

Проверка показывает версию, наличие `ssh`/`telnet`, pyserial и доступность
audit-каталога. Обязательных конфигурационных файлов нет.

## OpenCode V2

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "network-terminal": {
        "type": "local",
        "command": ["/home/USER/.local/bin/network-terminal-mcp"],
        "codemode": false,
        "disabled": false
      }
    }
  }
}
```

`codemode: false` оставляет все инструменты, включая `open_session`, в native
tool list. Если `~/.local/bin` уже в `PATH`, достаточно
`["network-terminal-mcp"]`.

## Документация

- [Текущая документация 0.2.1](docs/current/) — архитектура, эксплуатация,
  безопасность, тесты и результаты проверок.
- [Инструкция для модели](src/network_terminal_mcp/usage.md) — MCP instructions
  и ресурс `network-terminal://usage`.
- [Backlog](docs/plans/backlog.md) — только необязательные направления после
  реальной эксплуатации.
- [История](docs/history/) — завершённые планы и реализация 0.1.x, не
  руководство.

## Разработка

Требуются Python 3.12+, `uv` и POSIX/Linux.

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

Не добавляйте в репозиторий реальные адреса, hostnames, usernames,
fingerprints, секреты или production transcripts.
