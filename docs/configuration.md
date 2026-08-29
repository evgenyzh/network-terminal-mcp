# Конфигурация

Формат ниже реализован Pydantic-схемой Этапов 1-2. Неизвестные поля отклоняются,
а ссылки устройства на credential/connection profiles проверяются при загрузке.

## Разделение файлов

- `inventory.yml`: устройства, группы и ссылки на профили.
- `connections.yml`: маршруты, hops и legacy SSH параметры.
- `credentials.yml`: ссылки на `pass`, но не сами секреты.
- `policy.yml`: решения allow/ask/deny.

Runtime-файлы располагаются в `~/.config/network-terminal-mcp/`, а примеры в
репозитории не содержат реальных данных.

## Target

```yaml
devices:
  access-snr-01:
    host: 192.0.2.10
    platform: snr_29xx
    credentials: network-tacacs
    connection: direct
    tags: [access, lab]
    allow_telnet: false
```

Одноразовый target может передать те же несекретные поля в `open_session`.
Разрешенные credential и connection profiles по-прежнему берутся из локальной
конфигурации.

## Connection profile

```yaml
connections:
  direct:
    type: direct
    protocol: ssh
    host_key_policy: strict

  direct-enroll:
    type: direct
    protocol: ssh
    host_key_policy: accept_new

  direct-snr:
    type: direct
    protocol: ssh
    host_key_policy: accept_changed

  through-jump:
    type: proxyjump
    jump_host: jump.example.net
    jump_credentials: terminal-tacacs

  through-terminal:
    type: nested
    host: terminal.example.net
    protocol: ssh
    credentials: terminal-tacacs
    next_protocol: ssh

  old-switch:
    type: direct
    protocol: legacy_ssh
    host_key_algorithms: [ssh-rsa]
    kex_algorithms: [diffie-hellman-group14-sha1]
    ciphers: [aes128-cbc]
```

В Этапе 1 поддерживается только `direct` с protocol `ssh` или `legacy_ssh`.
`proxyjump`, `nested`, `console` и Telnet уже описаны схемой, но сервер их
отвергает до реализации соответствующего transport backend. Сложные nested-
профили в дальнейшем будут описываться массивом typed hops, а не shell-строкой.

`legacy_ssh` сейчас передается Paramiko/Netmiko. Явные списки KEX/ciphers/key
types сохраняются в профиле для будущего per-device override, но еще не меняют
настройки Paramiko автоматически.

## Credential profile

```yaml
credentials:
  network-tacacs:
    backend: pass
    entry: network/credentials/network-tacacs
    username: operator

  terminal-tacacs:
    backend: pass
    entry: network/credentials/terminal-server
    username: operator
```

Поля `entry` и `username` не принимаются из MCP-вызова. Пароль всегда берется
из `pass`; username не считается секретом и задается отдельно, чтобы не
дублировать его в password store.

## Platform aliases

```yaml
platforms:
  snr_29xx:
    driver: cisco_ios
    dialect: snr_29xx

  snr_52xx:
    driver: cisco_ios
    dialect: snr_52xx

  dlink_ds:
    driver: dlink_ds
    dialect: dlink_ds
    cli_help_requires_enter: true

  bdcom-new:
    driver: local:bdcom_huawei_like
    dialect: bdcom_huawei_like
```

`cli_help_requires_enter` включает режим подсказки, в котором `cli_help`
отправляет `<line>?` и Enter. Нужен для CLI (например, D-Link), где список
подсказки показывается только после Enter; `?` в конце не позволяет выполнить
строку. По умолчанию `false`: обычные CLI (Cisco, SNR) показывают помощь без
Enter, и сервер отменяет строку Ctrl-C.

## Runtime defaults

```yaml
runtime:
  session_idle_timeout: 300
  session_max_lifetime: 1800
  command_timeout: 60
  cli_help_timeout: 5
  max_inline_output_bytes: 65536
  max_session_buffer_bytes: 1048576
  max_open_sessions: 10
  max_pager_pages: 32
  audit_file: ~/.local/state/network-terminal-mcp/audit.jsonl
  output_dir: ~/.local/state/network-terminal-mcp/outputs
  known_hosts_file: ~/.local/state/network-terminal-mcp/known_hosts
  transcripts_enabled: false
```

`host_key_policy` по умолчанию равен `strict`: target подключается только при
совпадении ключа с `known_hosts_file`. Значение `accept_new` допускается только
для явной первичной регистрации ключа (TOFU); после нее профиль следует вернуть
в `strict`.

`accept_changed` — слабый доверительный режим, opt-in для конкретного профиля.
Используется только для платформ, у которых host key заведомо меняется при
каждой загрузке (например, некоторые SNR). При каждом подключении сервер
сверяет живой ключ с сохраненным: совпадает — оставляет без изменений,
изменился или отсутствует — заменяет и пишет в audit предупреждение со
старым и новым fingerprint. Для устройств со стабильными ключами этот режим
недопустим: он снимает защиту от MITM.

`transcripts_enabled` зарезервирован для следующего этапа и пока не включает
сохранение full transcript.

`max_pager_pages` ограничивает количество страниц, которые можно запросить
через `send_control(..., action="space")` в одной операции. После лимита сервер
отправляет `q`, ожидает prompt и переводит session в `ready`.

`cli_help_timeout` ограничивает ожидание подсказки отдельно от обычной команды.
После любого результата или таймаута сервер отправляет Ctrl-C и ожидает prompt;
оба ожидания ограничены этим значением. Невернувшаяся подсказка переводит
session в `failed`.

## Проверка конфигурации

```bash
uv run python -m network_terminal_mcp check
uv run python -m network_terminal_mcp check --device access-snr-01
```

Эти команды валидируют YAML и ссылки между профилями, не открывая сетевое
соединение и не читая password entry из `pass`.
