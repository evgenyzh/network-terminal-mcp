# План разработки

## Принципы

- Сначала read-only диагностика, затем контролируемые изменения.
- Сначала прямой SSH на тестовом оборудовании, затем сложные маршруты.
- Не писать vendor-команды в транспортном слое.
- Каждый новый транспорт или адаптер должен иметь воспроизводимый transcript и
  негативные тесты.

## Этап 0. Каркас проекта

Статус: выполнен. Добавлены strict Pydantic-схемы, loader четырех YAML-файлов,
pass credential backend, policy, audit/redaction, target resolver и unit tests.

- Уточнить схемы конфигурации и MCP tool contracts.
- Добавить Pydantic-модели, загрузчик YAML и структурированные ошибки.
- Настроить Ruff, mypy и pytest.
- Реализовать JSONL-аудит и redaction до первого сетевого подключения.

Критерий завершения: конфигурация валидируется, ошибки не содержат секретов,
а тесты запускаются одной командой.

## Этап 1. Прямой SSH и постоянные сессии

Статус: выполнен для direct SSH. Реализованы Netmiko session manager, output
buffer, idle/hard lifetime, JSONL audit, `known_hosts` и шесть stdio MCP tools.
Через зарегистрированные MCP tools проверены Cisco IOS (включая legacy
`group1`/`ssh-rsa`), SNR old/eNOS через `cisco_ios`, D-Link через `dlink_ds`,
Huawei VRP и Juniper Junos. Первичная регистрация host key требует явного
`host_key_policy: accept_new`; последующие соединения используют `strict`.

- Реализовать `pass` credential backend.
- Добавить session manager с timeout, lock и ограничением вывода.
- Реализовать `open_session`, `run_command`, `run_commands`, `session_status`,
  `read_output`, `close_session`.
- Подключить Cisco IOS, Huawei VRP и Juniper Junos через Netmiko.
- Проверить автоматический session preparation и paging.

Критерий завершения: MCP tools выполняют несколько диагностических команд в
одной сессии на трех тестовых платформах, не получая пароль. Регистрация MCP в
OpenCode остается задачей Этапа 6.

## Этап 2. Исследование CLI

Статус: реализован, покрыт local scripted tests и частично проверен на
оборудовании. Добавлены `cli_help`, `send_control`, `respond`, состояния
`paging`/`awaiting_response` и bounded pager flow. SNR dialect metadata уже была
добавлена в Этапе 1.

Hardware findings:
- `cli_help` работает на Cisco IOS и SNR eNOS; D-Link поддержан через
  `cli_help_requires_enter: true` (`<line>?` + Enter).
- На SNR old, Junos и Huawei VRP помощь не возвращает prompt после Ctrl-C за
  `cli_help_timeout`, и сессия безопасно падает. Нужны anonymized transcripts и
  platform-specific cleanup (например, `q` для pager-подобного help).
- SNR old меняет host key при каждой загрузке; добавлена per-profile политика
  `host_key_policy: accept_changed` с аудитом old→new fingerprint.

- `cli_help` отправляет `<line>?` без Enter, затем отменяет незавершенную строку
  через Ctrl-C и проверяет возврат prompt.
- Pager требует явного `send_control(space)`; доступны `q` для pager и `ctrl-c`
  для pager либо распознанного confirmation prompt.
- `respond` принимает только `y`/`n` либо `yes`/`no`, если они были явно
  распознаны в device prompt. Password/passphrase/secret prompts приводят к
  failed session, без отправки ответа.
- `ask` command policy по-прежнему не исполняется; `respond` не является
  подтверждением policy.

Критерий завершения: модель может найти неизвестную команду через `?`, очистить
строку и выполнить найденную команду без переподключения. Для полного закрытия
этапа еще нужны anonymized transcripts и read-only hardware validation pager/
`cli_help` на согласованной цели.

## Этап 3. Маршруты доступа

- Добавить ProxyJump/ProxyCommand.
- Добавить nested SSH через `generic_termserver` и `redispatch`.
- Добавить console profiles и вложенный Telnet.
- Проверить очистку сессий при ошибках на любом hop.

Критерий завершения: один и тот же tool contract работает для direct, jump и
terminal-server целей.

## Этап 4. Legacy SSH и Telnet

Наблюдение Этапа 1: проверенный Cisco IOS использует только
`diffie-hellman-group1-sha1`, `ssh-rsa`, `3des-cbc` и `hmac-sha1`. Paramiko 4.0
поддержал их без отдельного oldssh. Per-profile algorithm overrides и Telnet
по-прежнему не реализованы.

- Реализовать host-scoped SSH algorithm profiles.
- Проверить `ssh-rsa`, SHA1 KEX и CBC на лабораторной цели.
- Добавить прямые Telnet-драйверы и обязательное security warning.
- Оценить отдельный OpenSSH PTY backend для случаев, которые не поддерживает
  Paramiko.
- Не реализовывать DSA-only compatibility до появления реального устройства.

Критерий завершения: слабые алгоритмы никогда не применяются вне явно
разрешенных целей, Telnet нельзя включить случайно.

## Этап 5. Нестандартные платформы

- Собрать безопасные login/session transcripts с BDCOM, EcoSGE и PON.
- Реализовать только необходимые adapters: prompt, paging, modes, line ending.
- Разделить BDCOM Huawei-like и Cisco legacy.
- Добавлять варианты SNR только если различается механика терминала, а не команды.

Критерий завершения: каждая заявленная платформа проходит общий acceptance
suite и собственные transcript-тесты.

## Этап 6. Интеграция OpenCode

- Добавить глобальную MCP-конфигурацию OpenCode.
- Создать network skill с правилами диагностики и подтверждения изменений.
- Настроить permissions: read-only tools доступны, опасные требуют `ask`.
- Проверить перезапуск OpenCode и обнаружение MCP tools.

Критерий завершения: обычный запрос пользователя приводит к безопасной MCP-
сессии, а не к `sshpass` или временному Python-скрипту.

## Этап 7. Изменения конфигурации

Этот этап начинается только после стабильной диагностики.

- Добавить отдельные `plan_change` и `apply_change`; не расширять
  `run_command` скрытым режимом записи.
- Сохранять конфигурацию и состояние до изменений.
- Использовать нативный rollback: Junos commit confirmed, RouterOS Safe Mode и
  доступные vendor-механизмы.
- Для платформ без надежного rollback требовать рабочий console path.
- Добавить отдельное подтверждение пользователя и per-device `allow_writes`.

Критерий завершения: модель не может применить изменение одним вызовом без
плана, подтверждения и аудита.
