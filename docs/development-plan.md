# План разработки

## Принципы

- Сначала read-only диагностика, затем контролируемые изменения.
- Сначала прямой SSH на тестовом оборудовании, затем сложные маршруты.
- Не писать vendor-команды в транспортном слое.
- Каждый новый транспорт должен иметь воспроизводимый transcript и
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

Статус: выполнен для direct SSH. Реализованы session manager на Paramiko,
output buffer, idle/hard lifetime, JSONL audit, `known_hosts` и шесть stdio MCP
tools. Через зарегистрированные MCP tools проверены Cisco IOS (включая legacy
`group1`/`ssh-rsa`), SNR old/eNOS, D-Link, Huawei VRP и Juniper Junos. Первичная
регистрация host key требует явного `host_key_policy: accept_new`; последующие
соединения используют `strict`.

- Реализовать `pass` credential backend.
- Добавить session manager с timeout, lock и ограничением вывода.
- Реализовать `open_session`, `run_command`, `run_commands`, `session_status`,
  `read_output`, `close_session`.
- Подключить Cisco IOS, Huawei VRP и Juniper Junos.
- Проверить автоматическую подготовку сессии и обработку paging.

Критерий завершения: MCP tools выполняют несколько диагностических команд в
одной сессии на трех тестовых платформах, не получая пароль. Регистрация MCP в
OpenCode остается задачей Этапа 6.

## Этап 2. Исследование CLI

Статус: реализован, покрыт local scripted tests и проверен на оборудовании.
Добавлены `cli_help`, `send_control`, `respond`, состояния
`paging`/`awaiting_response` и bounded pager flow. SNR уже был подключён в
Этапе 1.

Hardware findings:
- `cli_help` работает на Cisco IOS, SNR eNOS и D-Link.
- SNR old возвращает настоящий help-pager; `space` листает, `q` возвращает к
  prompt, после чего Ctrl-C и Ctrl-U очищают неполную строку.
- На проверенных Junos/Huawei pager отключён. Их help заканчивается
  `prompt + набранная строка`; Ctrl-C и Ctrl-U очищают её без нового вывода,
  после чего следующая команда проходит в той же сессии.
- SNR old меняет host key при каждой загрузке; добавлена per-profile политика
  `host_key_policy: accept_changed` с аудитом old→new fingerprint.

- `cli_help` отправляет `<line>?` без Enter. После распознанного prompt с
  неполной строкой очищает её Ctrl-C и Ctrl-U; нераспознанный pager возврат
  остаётся fail-safe.
- Pager требует явного `send_control(space)`; доступны `q` для pager и `ctrl-c`
  для pager либо распознанного confirmation prompt.
- `respond` принимает только `y`/`n` либо `yes`/`no`, если они были явно
  распознаны в device prompt. Password/passphrase/secret prompts приводят к
  failed session, без отправки ответа.
- `ask` command policy по-прежнему не исполняется; `respond` не является
  подтверждением policy.

Критерий завершения: модель может найти неизвестную команду через `?`, очистить
строку и выполнить найденную команду без переподключения. Read-only hardware
validation выполнена; anonymized transcripts нужны для валидации новых
pager/prompt patterns.

## Этап 3. Маршруты доступа

Статус: реализован и проверен на реальном bastion. Один configured SSH-only
ProxyJump hop работает через Paramiko `direct-tcpip`, с независимыми host-key
checks и cleanup обоих hops. Поддерживаются password и explicit `ssh_key`
(включая encrypted key c passphrase из `pass`) credential profiles. Nested SSH
проверен на реальной паре intermediate host + SNR old target. Hardware
validation пройдена через OpenCode MCP для обоих маршрутов.

- Не добавлять literal `ProxyCommand`; произвольный shell process небезопасен
  для password-backed local profiles.
- Добавить console profiles и вложенный Telnet.
- Проверить очистку сессий при ошибках на любом hop.

Критерий завершения: один и тот же tool contract работает для direct, jump и
terminal-server целей.

## Этап 4. Legacy SSH и Telnet

Статус: реализован. Per-profile legacy SSH algorithm overrides применяются как
allowlist через Paramiko `disabled_algorithms` только к перечисленным
категориям. Прямой Telnet и `console` profiles поддерживают только явный
двойной gating (`allow_telnet: true` + `defaults.telnet: allow`); транспорт
Telnet/console — telnetlib3. Nested Telnet выполняется через `telnet` из shell
промежуточного хоста.
Telnet и console проверены unit-тестами; hardware-проверка Telnet на реальной
цели не выполнялась.

- Реализовать host-scoped SSH algorithm profiles.
- Проверить `ssh-rsa`, SHA1 KEX и CBC на лабораторной цели.
- Добавить прямой Telnet и обязательное security warning.
- Оценить отдельный OpenSSH PTY backend для случаев, которые не поддерживает
  Paramiko.
- Не реализовывать DSA-only compatibility до появления реального устройства.

Критерий завершения: слабые алгоритмы никогда не применяются вне явно
разрешенных целей, Telnet нельзя включить случайно.

## Этап 5. Нестандартные платформы

Отдельные драйверы не пишутся. Семейства устройств (BDCOM, EcoSGE, PON и т.п.)
определяет модель по баннеру и первичному выводу; механика терминала
обрабатывается универсальным транспортным слоем и state machine сервера.
Нестандартное поведение исследуется через `cli_help` и `send_control`, а при
необходимости обобщается в транспортном слое или в skill.

- Собрать безопасные login/session transcripts с BDCOM, EcoSGE и PON.
- Валидировать transcripts на универсальном транспорте.
- Добавлять варианты SNR только если различается механика терминала, а не команды.

Критерий завершения: каждая заявленная платформа проходит общий acceptance
suite на базе transcript-тестов без отдельного кода под вендора.

## Этап 6. Интеграция OpenCode

Статус: выполнен для текущего global OpenCode profile. Local stdio MCP
`network-terminal` зарегистрирован с рабочим каталогом проекта и timeout 65
секунд. Global skill направляет диагностику только через MCP, открывает
`open_session(host=...)` по IP/имени, определяет семейство устройства по
баннеру/выводу и требует read-only command и закрытие session.

`opencode mcp list` подтвердил подключение, а read-only round-trip
`open_session -> run_command -> close_session` выполнен через OpenCode.
Ограничения команд по-прежнему применяет policy самого MCP: `ask` не
исполняется, `respond` не подтверждает policy.

Критерий завершения: обычный запрос пользователя приводит к безопасной MCP-
сессии, а не к `sshpass` или временному Python-скрипту.

## Этап 7. Изменения конфигурации

Статус: реализован. Изменения отделены от диагностики отдельными инструментами
`plan_change`, `apply_change`, `abort_change` и `finalize_change`.
`apply_change` двухшаговый, хранит канонический список команд на сервере и
исполняет один раз после явного подтверждения человеком. Откат
(reload/commit confirmed) — рекомендация модели по собственному усмотрению, а
не серверная механика.

- Добавить отдельные `plan_change` и `apply_change`; не расширять
  `run_command` скрытым режимом записи.
- Сохранять конфигурацию и состояние до изменений.
- Добавить отдельное подтверждение пользователя перед исполнением.

Критерий завершения: модель не может применить изменение одним вызовом без
плана, подтверждения и аудита.
