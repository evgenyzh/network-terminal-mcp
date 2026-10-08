# Эксплуатация

## Границы текущей версии

Сервер поддерживает постоянные direct SSH/Telnet/console/serial-сессии,
локальный SOCKS5-маршрут и один SSH ProxyJump hop для первого подключения.
Дальше модель работает в сыром терминале: печатает команды, `?`-подсказки,
второй `ssh`/`telnet` и одиночные клавиши. Маршрут и credentials модель
описывает прямо в `open_session`; локальный inventory/connection/credential
YAML не нужен.

Рабочая инструкция для модели едет внутри MCP: краткий контракт в
`instructions` и полный мануал в ресурсе `network-terminal://usage` (исходник —
`src/network_terminal_mcp/usage.md`).

Все risky-опции явные: `allow_telnet=true` для Telnet/console,
`allow_serial=true` для `/dev/tty*`, флаг `allow_plaintext_password=true` для
plaintext-пароля, `legacy` для старых SSH-алгоритмов. Policy может hard-deny
каждую из них.

Каждый вызов `open_session` проходит через нативный permission-попап OpenCode
(`network-terminal_open_session: ask`): пользователь видит host, route и
credential-ссылки до подключения. Ввод в уже открытой сессии подтверждений не
требует; при желании оператор может добавить
`"network-terminal_terminal_write": "ask"` в permission-политику клиента.

## Рабочий цикл

```text
open_session(host="192.0.2.1", credentials={backend: "pass", entry: "net/sw", username: "operator"},
             host_key_policy="accept_new")
terminal_write(session_id, "display version")     # определяем вендора по выводу
terminal_read(session_id)                          # читаем баннер/версию/pager
terminal_write(session_id, "display interface brief")
terminal_read(session_id)
close_session(session_id)
```

Pager листается обычным вводом: `terminal_write(session_id, " ", enter=False)`
или `terminal_write(session_id, "q", enter=False)`. Подсказки CLI — печатайте
`display ?` как есть и читайте вывод. `Ctrl-C` — `terminal_write(session_id,
"\u0003", enter=False)`. Никаких специальных режимов у сервера нет.

## Вложенные переходы

`proxyjump` нужен только чтобы попасть на bastion. Дальше переход выполняется
обычным вводом в той же сессии:

```text
open_session(host="localhost", port=2224,
             route={type: "proxyjump", host="bastion.example.net",
                    credentials={backend: "ssh_key", key_file: "~/.ssh/id_ed25519",
                                 key_passphrase_entry: "net/key-pass", username: "operator"}},
             credentials={backend: "ssh_key", key_file: "~/.ssh/id_ed25519",
                          key_passphrase_entry: "net/key-pass", username: "operator"})
terminal_write(session_id, "ssh operator@192.0.2.40")  # с shell bastion
terminal_read(session_id)                              # "operator@192.0.2.40's password:"
terminal_write_secret(session_id, "net/device-pass")   # значение из pass, не в audit
terminal_read(session_id)                           # CLI устройства
close_session(session_id)
```

Host key следующего hop проверяет SSH-клиент промежуточного хоста, а не
локальный `known_hosts` этого сервера.

## Маршруты первого подключения

```text
# локальный SOCKS5, обычно ssh -D; target должен быть IP
open_session(host="192.0.2.1", route={type: "socks", host: "127.0.0.1", port: 1080},
             credentials={backend: "pass", entry: "network/net", username: "operator"})

# SSH jump host; host key jump и target проверяются независимо
open_session(host="192.0.2.1", route={type: "proxyjump", host: "jump.example.net",
             credentials={backend: "pass", entry: "network/jump", username: "operator"}},
             credentials={backend: "pass", entry: "network/net", username: "operator"})
```

Для первого подключения используйте `host_key_policy="accept_new"`, затем
`strict`. Доверие к SOCKS-прокси — на операторе, сервер возвращает warning.

## Telnet, console и serial

Прямой Telnet, `console` (TCP console port терминального сервера) и serial
включаются только явными флагами (`allow_telnet=true` / `allow_serial=true`);
policy может запретить их полностью. Telnet/console передают учётные данные и
трафик открытым текстом; serial не имеет ни аутентификации, ни защиты канала.
Для serial `host` — абсолютный `/dev/tty*`, который должен быть символьным
устройством.

## Несколько устройств одновременно

Один процесс держит до `max_open_sessions` (по умолчанию 10) независимых
сессий. `session_status` показывает состояние, prompt и warnings каждой.
Сессии закрываются по idle timeout (по умолчанию 1 час), hard lifetime
(24 часа) или `close_session`. Значения настраиваются в `policy.yml`.

Реализованные MCP tools:

- `open_session`
- `terminal_write`
- `terminal_read`
- `terminal_write_secret`
- `read_output`
- `session_status`
- `close_session`

## Локальная конфигурация

Обязательных файлов нет. Единственный необязательный файл —
`~/.config/network-terminal-mcp/policy.yml` (каталог 0700, файл 0600): posture
и runtime-лимиты. Без него действуют встроенные defaults. Формат описан в
[документе конфигурации](configuration.md).

Проверка без сетевых соединений:

```bash
uv sync
uv run python -m network_terminal_mcp check
```

Старые `inventory.yml`, `connections.yml` и `credentials.yml` больше не
читаются и могут быть удалены.

## Credentials

Credentials передаются в вызове как ссылки:

```text
{ backend: "pass", entry: "network/credentials/net", username: "operator" }
{ backend: "ssh_key", key_file: "~/.ssh/id_ed25519",
  key_passphrase_entry: "network/credentials/key-pass", username: "operator" }
```

Первая строка password-store entry — пароль. Пароль не передаётся в другие MCP
tools, audit или shell history. Секреты, запрашиваемые уже внутри сессии,
вводятся через `terminal_write_secret` и тоже не попадают в audit.
Plaintext-пароль допустим только при явном `allow_plaintext_password=true`.

## Первое SSH-подключение

По умолчанию `host_key_policy: strict`: ключ target обязан уже присутствовать в
`~/.local/state/network-terminal-mcp/known_hosts`. Для первичной регистрации
передайте в конкретном вызове `host_key_policy="accept_new"` (TOFU). Первое
успешное `open_session` сохраняет ключ с правами 0600, пишет fingerprint в audit
и возвращает предупреждение. Сверьте fingerprint по независимому каналу, после
чего используйте `strict`.

Ключ никогда не заменяется автоматически. Несовпадение ключа прерывает
подключение. Слабый `accept_changed` — только для платформ с заведомо
меняющимся при загрузке ключом (некоторые SNR); он явный, всегда пишет в audit
старый и новый fingerprint.

## Audit и вывод

- Audit: `~/.local/state/network-terminal-mcp/audit.jsonl`.
- Known hosts: `~/.local/state/network-terminal-mcp/known_hosts`.
- Обе директории создаются с 0700, файлы с 0600.
- Если audit недоступен, новая операция не исполняется.
- `terminal_write` пишет полный ввод (кроме известных серверу секретов),
  `terminal_write_secret` — только entry и размер.
- `read_output` без offset продолжает с курсора чтения (только непрочитанный
  вывод) и аудируется с offset/bytes/cursor; явный offset — осознанное
  обращение к bounded session buffer. При переполнении буфера старый вывод
  недоступен, а `oldest_offset` сообщает границу.

## Запуск MCP

MCP-сервер ставится как инструмент и запускается OpenCode через stdio:

```bash
uv tool install network-terminal-mcp     # команда в ~/.local/bin
uv tool upgrade network-terminal-mcp     # обновление
```

Не запускать его вручную в обычном терминале для диагностики: stdout
зарезервирован исключительно для MCP protocol. `uvx network-terminal-mcp@latest`
на каждый старт не рекомендуется: сетевой резолвинг `@latest` и общий кэш uv
дают задержки в несколько секунд, а при нескольких окнах — таймауты
подключения. В OpenCode сервер регистрируется как local MCP с полным путём к
бинарю и timeout не меньше `runtime.io_timeout`:

```json
{
  "mcp": {
    "network-terminal": {
      "type": "local",
      "command": ["/home/USER/.local/bin/network-terminal-mcp"],
      "enabled": true,
      "timeout": 65000
    }
  },
  "permission": {
    "network-terminal_open_session": "ask"
  }
}
```

Если `~/.local/bin` в `PATH`, достаточно `["network-terminal-mcp"]`.

После изменения global OpenCode config или skill перезапустите OpenCode:
конфигурация и MCP tools загружаются только при старте.

### Дублирование MCP-процессов

OpenCode может держать **пару** процессов одного local MCP-сервера: один
поднимается при старте сессии, второй — при активации возобновлённой сессии
(известный баг OpenCode: #43845, смежные #46035, #46174). Это не ошибка этого
проекта. OpenCode сам маршрутизирует tool-вызовы на нужный процесс, поэтому
модель и оператор **не выбирают процесс и не убивают его вручную**. Сессии
живут в памяти обслуживающего процесса и закрываются по idle-таймауту или
`close_session`; при перезапуске процесса всё fail-closed — новая сессия
создаётся заново.
