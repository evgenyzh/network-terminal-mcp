# Конфигурация

Сервер работает zero-config: после установки не нужно писать YAML, чтобы
подключиться. Каждое соединение модель описывает прямо в вызове `open_session`
(host, protocol, credentials, route, host key policy, serial-параметры,
опционально legacy алгоритмы). Pydantic валидирует описание до открытия любого
сетевого соединения, а секреты остаются ссылками.

Единственный локальный файл — **необязательный** `policy.yml`: posture и
runtime-лимиты. Без него действуют встроенные значения. Инвентаря, connection и
credential profiles больше нет.

## `open_session`

```text
open_session(
  host: str,
  credentials: CredentialSpec | None = None,
  port: int | None = None,          # порт final target
  protocol: "ssh"|"telnet"|"console"|"serial" = "ssh",
  route: SocksRoute | ProxyJumpRoute | None = None,
  host_key_policy: "strict"|"accept_new"|"accept_changed" = "strict",
  legacy: LegacyAlgorithms | None = None,
  allow_telnet: bool = False,
  allow_plaintext_password: bool = False,
  allow_serial: bool = False,
  serial: SerialParams | None = None,
)
```

`protocol` и `port` всегда описывают final target. Тип оборудования не
передаётся: модель определяет его по баннеру и выводу после подключения.
Для `serial` credentials не нужны (устройство аутентифицирует само консольный
вход), host — абсолютный путь к `/dev/tty*`.

### Credentials

```text
# пароль из pass
credentials = { backend: "pass", entry: "network/credentials/net", username: "operator" }

# явный локальный SSH key
credentials = { backend: "ssh_key", key_file: "~/.ssh/id_ed25519",
                key_passphrase_entry: "network/credentials/key-pass", username: "operator" }

# plaintext — только с allow_plaintext_password=true, всегда insecure warning
credentials = { backend: "plaintext", username: "operator", password: "..." }
```

`entry` и `key_passphrase_entry` — относительные имена записей `pass` (без `..`
и ведущего `/`). Сервер вызывает `pass show` без shell. Пароль из `pass`
читается только в память сессии, не попадает в аргументы, results и audit и
используется redaction. Plaintext-пароль принимается только при явном
`allow_plaintext_password=true`, не сохраняется сервером, маскируется в аудите и
всегда возвращается с insecure warning; policy может hard-deny этот режим.

### Route

`route` описывает один hop до final target; без него соединение прямое. Дальше
модель сама печатает `ssh`/`telnet` в открытой сессии, если нужен следующий
переход.

```text
# локальный SOCKS5 (обычно ssh -D); target должен быть IP
route = { type: "socks", host: "127.0.0.1", port: 1080 }

# один SSH jump host
route = { type: "proxyjump", host: "jump.example.net", port: 22,
          credentials: { backend: "pass", entry: "network/jump", username: "operator" },
          host_key_policy: "strict" }
```

- `socks` и `proxyjump` поддерживают только SSH final target.
- Host key jump проверяется локально по `host_key_policy` route.
- Произвольный `ProxyCommand`, shell-строки и больше одного hop не
  поддерживаются. Второй hop выполняется вручную: `terminal_write("ssh ...")`.

### Serial

```text
open_session(host="/dev/ttyUSB0", protocol="serial", allow_serial=True,
             serial={ baudrate: 115200, bytesize: 8, parity: "N", stopbits: 1 })
```

Путь должен существовать и быть символьным устройством. Serial не проверяет
host key и не аутентифицируется; policy может hard-deny через
`defaults.allow_serial: false`. По умолчанию `terminal_write(enter=True)`
отправляет `\r`, как ожидает большинство консольных портов.

### Legacy SSH

```text
legacy = { host_key_algorithms: ["ssh-rsa"],
           kex_algorithms: ["diffie-hellman-group1-sha1"],
           ciphers: ["aes128-cbc"] }
```

Списки — allowlist для указанных категорий: они превращаются в Paramiko
`disabled_algorithms`, остальные категории сохраняют defaults, host key checking
не отключается. Legacy применяется только к локальному SSH-соединению (direct,
socks, proxyjump) и всегда возвращает warning. Штатный сценарий: модель сначала
пробует обычный SSH; если устройство требует старые алгоритмы, повторяет вызов с
`legacy` для этого явного host. Policy может hard-deny.

### Host key

По умолчанию `strict`: host key обязан быть в локальном
`~/.local/state/network-terminal-mcp/known_hosts`. Для первого подключения
передайте `host_key_policy: "accept_new"` (TOFU, явная регистрация), сверьте
fingerprint вне канала и вернитесь к `strict`.

`accept_changed` — слабый режим для устройств, у которых ключ заведомо меняется
при каждой загрузке (например, некоторые SNR). Он явный, всегда пишет в audit
старый и новый fingerprint и не должен применяться к устройствам со стабильными
ключами.

### Telnet и console

`protocol: "telnet"` или `"console"` требуют явного `allow_telnet=true` в
вызове. `console` дополнительно требует `port`. Telnet/console передают учётные
данные и трафик открытым текстом, не проверяют host key и всегда возвращают
warning. Policy может hard-deny через `defaults.allow_telnet: false`.

## Секреты в живой сессии

Пароль, который устройство запрашивает уже внутри сессии (второй `ssh`,
`enable`, TACACS), вводится только инструментом
`terminal_write_secret(session_id, entry)`: значение берётся из `pass` внутри
сервера, не возвращается модели и не пишется в audit (только имя записи и
размер). Обычный `terminal_write` предназначен для команд и клавиш; его ввод
логируется целиком.

## policy.yml (необязательно)

`~/.config/network-terminal-mcp/policy.yml`, каталог 0700, файл 0600. Все поля
опциональны; приведены значения по умолчанию:

```yaml
defaults:
  allow_telnet: true                 # false = hard-deny Telnet/console
  allow_plaintext_password: true     # false = hard-deny plaintext-пароль
  allow_legacy_algorithms: true      # false = hard-deny legacy SSH
  allow_serial: true                 # false = hard-deny /dev/tty*
runtime:
  session_idle_timeout: 3600
  session_max_lifetime: 86400
  io_timeout: 60                     # connect/auth и default read timeout
  max_read_timeout: 300              # верхняя граница terminal_read(timeout)
  max_write_bytes: 65536
  max_inline_output_bytes: 65536
  max_session_buffer_bytes: 1048576
  max_open_sessions: 10
  audit_file: ~/.local/state/network-terminal-mcp/audit.jsonl
  output_dir: ~/.local/state/network-terminal-mcp/outputs
  known_hosts_file: ~/.local/state/network-terminal-mcp/known_hosts
  transcripts_enabled: false
```

Долгие idle/lifetime позволяют держать несколько устройств открытыми
одновременно и переключаться между ними без переподключения.

Проверка конфигурации без сетевых соединений:

```bash
uv sync
uv run python -m network_terminal_mcp check
```

Каталог конфигурации переопределяется `--config-dir` или
`NETWORK_MCP_CONFIG_DIR`. Старые файлы `inventory.yml`, `connections.yml` и
`credentials.yml` больше не читаются и могут быть удалены.
