# Эксплуатация Этапов 1-4

## Границы текущей версии

Сервер поддерживает постоянные direct SSH-сессии, один configured SSH-only
ProxyJump hop, один configured Nested hop (SSH или Telnet) и TCP console
profiles. Он не поддерживает `raw_input` или запись конфигурации.

Для ProxyJump нужны локальные connection и credential profiles для final target
и bastion. Обе host key проверяются независимо; bastion должен разрешать
`direct-tcpip` forwarding. При ошибке любого hop обе SSH-сессии закрываются.

Для Nested нужны локальные connection и credential profiles для intermediate
host и final target. Сервер подключается к intermediate host через
`generic_termserver`, выполняет из его shell `ssh` до final target и
`redispatch` на платформу цели. Host key intermediate host проверяется локально;
inner SSH использует SSH-клиент intermediate host, поэтому host key final target
проверяется им. При ошибке connection закрывается.

## Telnet и console

Прямой Telnet, Nested Telnet (`next_protocol: telnet`) и `console` profiles
(ТСP console port терминального сервера) включаются только двойным gating:
`allow_telnet: true` на устройстве **и** `defaults.telnet: allow` в policy.
По умолчанию `telnet: deny`, поэтому Telnet выключен, пока не разрешён явно.
Telnet передаёт учётные данные и трафик открытым текстом и не проверяет host
key; каждая сессия возвращает warning. Nested Telnet выполняет `telnet` из
shell промежуточного хоста, поэтому host key цели также не проверяется.
`console` требует заданный `port`; `connect_command` не поддерживается.

Telnet-драйвер платформы берётся из `telnet_driver` в platform alias, иначе
выводится как `<driver>_telnet`; если такого класса нет, Telnet/console для
платформы недоступны.

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

`cli_help(session_id, line)` отправляет `<line>?` без Enter и читает completion.
Не передавайте в `line` символ `?`, перевод строки или control bytes. Если CLI
уже вернул `prompt + остаток строки`, сервер очищает line buffer Ctrl-C и
Ctrl-U, не нажимая Enter. Для остальных непостраничных случаев ожидание prompt
после Ctrl-C ограничено `runtime.cli_help_timeout` (по умолчанию 5 секунд), а
не общим таймаутом команды.

Проверено на оборудовании: `cli_help` работает на Cisco IOS и SNR eNOS
(Cisco-подобный CLI). Для CLI, где помощь показывается только после Enter
(например, D-Link), задайте платформе `cli_help_requires_enter: true` в
`connections.yml`; тогда сервер отправит `<line>?` и Enter, `?` в конце не даст
строке выполниться.

На SNR old help идёт через pager: `cli_help` возвращает первый экран с
`pager_active: true`, дальше листайте `send_control("space")` и выходите `q`.
После выхода сервер ждёт prompt с остатком строки и очищает её Ctrl-C и Ctrl-U.
Надёжная клавиша выхода из help-pager — `q`; при нераспознанном возврате к
prompt сессия безопасно переводится в `failed`.

Netmiko `session_preparation` отключает pager на проверенных Junos и Huawei
VRP. Их help возвращается одним ответом, но оставляет набранную строку; сервер
очищает её последовательностью Ctrl-C, Ctrl-U перед следующей командой.

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
исключительно для MCP protocol. Для OpenCode server регистрируется как local
MCP с абсолютным `cwd` проекта и timeout не меньше `runtime.command_timeout`:

```json
{
  "mcp": {
    "network-terminal": {
      "type": "local",
      "command": ["uv", "run", "network-terminal-mcp"],
      "cwd": "/absolute/path/to/network-terminal-mcp",
      "enabled": true,
      "timeout": 65000
    }
  }
}
```

После изменения global OpenCode config или skill перезапустите OpenCode:
конфигурация и MCP tools загружаются только при старте.
