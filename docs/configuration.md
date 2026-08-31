# Конфигурация

Формат ниже реализован Pydantic-схемой Этапов 1-3. Неизвестные поля отклоняются,
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
    allow_writes: false
```

`allow_writes: true` разрешает применение изменений конфигурации на этом
устройстве (через `plan_change`/`apply_change`); без него изменения всегда
отклоняются, даже если policy разрешает `write_change`. По умолчанию `false`.

Инвентарь предназначен для инфраструктуры, а не для каждого устройства.
Сетевые устройства (коммутаторы и т.п.) обычно достигаются ad-hoc через
`open_session(host=..., platform=...)` без записи в `devices`. Для ad-hoc
подключений `credentials` и `connection` берутся из дефолтов:

```yaml
default_credentials: network-tacacs
default_connection: through-jump
```

`default_credentials` и `default_connection` — опциональные имена профилей,
которые подставляются в ad-hoc `open_session`, когда модель не передала их
явно. Оба должны существовать среди профилей. Одноразовый target может
передать те же несекретные поля в `open_session`. Разрешенные credential и
connection profiles по-прежнему берутся из локальной конфигурации.

Тип оборудования ad-hoc может быть неверен с первого раза: модель открывает
сессию с предполагаемым `platform`, читает вывод и при необходимости меняет
драйвер инструментом `set_platform` (см. операции), не закрывая сессию.

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
    protocol: ssh
    jump_host: jump.example.net
    jump_port: 22
    jump_credentials: terminal-tacacs
    jump_host_key_policy: strict
    host_key_policy: strict
    # Optional fallback for the final target. Device/ad-hoc port wins.
    port: 22

  through-terminal:
    type: nested
    host: terminal.example.net
    protocol: ssh
    credentials: terminal-tacacs
    next_protocol: ssh
    host_key_policy: strict

  old-switch:
    type: direct
    protocol: legacy_ssh
    host_key_algorithms: [ssh-rsa]
    kex_algorithms: [diffie-hellman-group14-sha1]
    ciphers: [aes128-cbc]
```

Поддерживаются `direct` с protocol `ssh`, `legacy_ssh` или `telnet`, один
SSH-only `proxyjump` hop, один `nested` hop (`next_protocol: ssh` или
`telnet`) и TCP `console` profile. ProxyJump аутентифицируется на
`jump_host`, открывает `direct-tcpip` channel к final target и передаёт его
Netmiko как socket. `jump_port` относится к bastion; final target использует
`Device.port` или ad-hoc `port`, затем fallback profile `port`, затем 22.

Nested подключается к `host` через `generic_termserver`, затем из shell
промежуточного хоста выполняет `ssh` (или `telnet`, если
`next_protocol: telnet`) до final target с credentials целевого устройства и
переключает драйвер `redispatch` на платформу цели. Inner SSH
использует SSH-клиент промежуточного хоста: host key цели проверяется им, а не
локальным `known_hosts_file`. Inner Telnet вообще не проверяет host key цели.

`console` profile подключается к TCP console port терминального сервера
(`port` обязателен) через Telnet-драйвер платформы. Поле `connect_command`
намеренно не поддерживается: произвольная shell-строка противоречит модели
безопасности. Console требует того же gating, что и Telnet.

Ключи jump host и final target проверяются раздельно в одном dedicated
`known_hosts_file`: `jump_host_key_policy` относится к bastion, а
`host_key_policy` — к final target (для `nested` — к intermediate host).
Literal `ProxyCommand` и несколько hops не поддерживаются. Сложные nested-профили
в дальнейшем будут описываться массивом typed hops, а не shell-строкой.

`proxyjump` с полем `socks: {host, port}` вместо `jump_host` маршрутизирует
final target через локальный SOCKS5-прокси — обычно это локальный SSH dynamic
forward (`ssh -D 1080 jump.example.net`). `socks` и `jump_host` взаимоисключающие.
Прокси не резолвит имена, поэтому target должен быть IP-адресом. Для первого
подключения цели, видимой только через туннель, используйте
`host_key_policy: accept_new` (TOFU через SOCKS), затем переключите на `strict`.
Пример:

```yaml
connections:
  through-socks:
    type: proxyjump
    protocol: ssh
    socks: { host: 127.0.0.1, port: 1080 }
    host_key_policy: accept_new
```

`legacy_ssh` и явные списки KEX/ciphers/key types выполняют per-profile
algorithm override: категории, перечисленные в профиле, ограничиваются
allowlist, остальные сохраняют значения Paramiko по умолчанию. Списки не
применяются глобально и не отключают host key checking.

Telnet и console требуют двойного gating: `allow_telnet: true` на устройстве
**и** `defaults.telnet: allow` в policy. По умолчанию `telnet: deny`, поэтому
случайно включить Telnet нельзя. Telnet не проверяет host key и передаёт
трафик и учётные данные открытым текстом; сессия всегда возвращает warning.

Изменения конфигурации тоже требуют двойного gating: `Device.allow_writes:
true` **и** `defaults.write_change: allow` в policy (по умолчанию `deny`).
Инструменты `plan_change`, `apply_change`, `abort_change` и `finalize_change`
применяют изменения отдельно от `run_command`; команды пишет модель, а сервер
хранит их канонический список и исполняет один раз. Подробнее в разделе
[Запись конфигурации](security.md#запись-конфигурации).

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

  jump-key:
    backend: ssh_key
    key_file: ~/.ssh/id_ed25519
    key_passphrase_entry: network/credentials/jump-key-passphrase
    username: operator
```

Поля `entry` и `username` не принимаются из MCP-вызова. Пароль всегда берется
из `pass`; username не считается секретом и задается отдельно, чтобы не
дублировать его в password store. Profile с `backend: ssh_key` хранит только
явный локальный путь `key_file` и username; automatic ssh-agent/key discovery
не включается. Key file не передается в MCP arguments. Для password-backed
target используйте отдельный `pass` profile, даже если jump host использует key.
Для зашифрованного private key добавьте `key_passphrase_entry`: его первая строка
также читается из `pass`, redacted и не передается в MCP arguments.

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
    telnet_driver: dlink_ds_telnet

  bdcom-new:
    driver: local:bdcom_huawei_like
    dialect: bdcom_huawei_like
```

`cli_help_requires_enter` включает режим подсказки, в котором `cli_help`
отправляет `<line>?` и Enter. Нужен для CLI (например, D-Link), где список
подсказки показывается только после Enter; `?` в конце не позволяет выполнить
строку. По умолчанию `false`: обычные CLI (Cisco, SNR) показывают помощь без
Enter, и сервер отменяет строку Ctrl-C.

`telnet_driver` задаёт Netmiko-драйвер для Telnet/console соединений платформы.
По умолчанию используется `<driver>_telnet` (например, `cisco_ios_telnet`);
для платформ, где такого класса нет, задайте его явно или оставьте `null`,
тогда Telnet/console для платформы будут недоступны.

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
