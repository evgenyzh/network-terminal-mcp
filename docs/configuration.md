# Конфигурация

Формат ниже является черновиком v0. Он уточняется до начала реализации, после
чего фиксируется Pydantic-схемой.

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

Сложные nested-профили в дальнейшем будут описываться массивом typed hops, а не
shell-строкой.

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

  bdcom-new:
    driver: local:bdcom_huawei_like
    dialect: bdcom_huawei_like
```

## Runtime defaults

```yaml
runtime:
  session_idle_timeout: 300
  session_max_lifetime: 1800
  command_timeout: 60
  max_inline_output_bytes: 65536
  max_session_buffer_bytes: 1048576
  max_open_sessions: 10
  audit_file: ~/.local/state/network-terminal-mcp/audit.jsonl
  output_dir: ~/.local/state/network-terminal-mcp/outputs
  known_hosts_file: ~/.local/state/network-terminal-mcp/known_hosts
  transcripts_enabled: false
```

`host_key_policy` по умолчанию равен `strict`: target подключается только при
совпадении ключа с `known_hosts_file`. Значение `accept_new` допускается только
для явной первичной регистрации ключа (TOFU); после нее профиль следует вернуть
в `strict`.
