# Эксплуатация Этапов 1-2

## Границы текущей версии

Сервер поддерживает постоянные direct SSH-сессии через Netmiko. Он не
поддерживает ProxyJump, terminal server, nested SSH/Telnet, console ports,
Telnet, `raw_input` или запись конфигурации.

Реализованные MCP tools:

- `open_session`
- `run_command`
- `run_commands`
- `cli_help`
- `send_control`
- `respond`
- `read_output`
- `session_status`
- `close_session`

## Локальная конфигурация

Конфигурация хранится вне git в `~/.config/network-terminal-mcp/`:

```text
inventory.yml
connections.yml
credentials.yml
policy.yml
```

Права: каталог 0700, файлы 0600. Схемы и примеры находятся в `config/`, а
проверка выполняется без сетевых соединений:

```bash
uv sync
uv run python -m network_terminal_mcp check
uv run python -m network_terminal_mcp check --device <inventory-name>
```

## Credentials

`credentials.yml` содержит имя записи `pass` и не секретный AAA username:

```yaml
credentials:
  network-tacacs:
    backend: pass
    entry: network/credentials/network-tacacs
    username: operator
```

Первая строка password-store entry — пароль. Не передавать пароль в MCP tool,
inventory, audit или shell history.

## Первое SSH-подключение

По умолчанию `host_key_policy: strict`: ключ target обязан уже присутствовать в
`~/.local/state/network-terminal-mcp/known_hosts`.

Для первичной регистрации создается отдельный профиль с явным TOFU:

```yaml
connections:
  direct-enroll:
    type: direct
    protocol: ssh
    host_key_policy: accept_new
```

Используйте его только для конкретной новой цели. Первое успешное
`open_session` сохраняет ключ с правами 0600, пишет fingerprint в audit и
возвращает предупреждение. Сверьте fingerprint по независимому каналу, затем
переключите устройство обратно на профиль `direct` со `strict`.

Ключ никогда не заменяется автоматически. Несовпадение ключа прерывает
подключение.

## Read-only политика

Исполняются только команды с решением `allow`. Типовая policy:

```yaml
rules:
  - id: read-only
    action: allow
    command_patterns: ["show *", "display *"]
```

Неизвестная команда возвращает `confirmation_required`; она не отправляется на
оборудование. Destructive operations (`reload`, `erase`, `delete` и подобные),
переносы строк, chaining и shell metacharacters запрещены.

## Интерактивный read-only CLI

`cli_help(session_id, line)` отправляет `<line>?` без Enter, читает completion
и отменяет незавершенную строку Ctrl-C перед возвратом. Не передавайте в `line`
символ `?`, перевод строки или control bytes.

При pager `run_command` возвращает `pager_active: true` и состояние `paging`.
Продолжайте только одной страницей: `send_control(session_id, "space")`.
`q` или `ctrl-c` возвращают terminal к prompt; после `max_pager_pages` сервер
сам отправляет `q` вместо новой страницы.

При `response_required: true` вызывайте `respond` только одним token из
`allowed_responses`. Это device confirmation, а не подтверждение policy: команда
с `confirmation_required` по-прежнему не была отправлена. Password/passphrase/
secret prompts не получают ответа; session становится `failed` и требует
`close_session` с последующим новым подключением.

## Audit и вывод

- Audit: `~/.local/state/network-terminal-mcp/audit.jsonl`.
- Known hosts: `~/.local/state/network-terminal-mcp/known_hosts`.
- Обе директории создаются с 0700, файлы с 0600.
- Если audit недоступен, новая команда не исполняется.
- `read_output` читает bounded session buffer по offset. При переполнении buffer
  старый вывод недоступен, а `oldest_offset` сообщает границу.

## Запуск MCP

После регистрации в конфигурации OpenCode MCP запускается через stdio:

```bash
uv run network-terminal-mcp
```

Не запускать его вручную в обычном терминале для диагностики: stdout зарезервирован
исключительно для MCP protocol. Интеграция с OpenCode еще не выполнена; она
запланирована в Этапе 6.
