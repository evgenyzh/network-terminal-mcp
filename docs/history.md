# История: этапы 0-8

Историческая справка. Этапы 0-8 развивали командно-ориентированный gateway
(`run_command`, `run_commands`, `cli_help`, `send_control`, `respond`,
`run_change`) и `nested`-маршрут. На Этапе 9 они удалены и заменены raw terminal
API. Актуальное состояние — [план разработки](development-plan.md) и
[результаты проверок](validation.md).

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

Детали:

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

Статус: реализован. Изменения отделены от диагностики отдельным инструментом
`run_change`. Команды проходят структурную безопасность и исполняются в порядке
перечисления; pager или device confirmation прерывают исполнение (fail-closed).
Подтверждение вынесено на уровень клиента: `network-terminal_run_change`
настроен на `permission: ask` в `opencode.json`, и opencode показывает нативное
всплывающее окно (once/always/reject) перед исполнением. Серверного механизма
подтверждения нет. Сессии живут в памяти и не переживают рестарт сервера —
после рестарта `run_change` для отсутствующей сессии возвращает ошибку
(изменение не выполняется). Откат (reload/commit confirmed) — рекомендация
модели по собственному усмотрению, а не серверная механика.

- Добавить отдельный `run_change`; не расширять `run_command` скрытым
  режимом записи.
- Убрать отложенные планы: подтверждение даёт permission-политика клиента,
  а не двухшаговый вызов.
- Сохранять конфигурацию и состояние до изменений.
- Подтверждение пользователя перед исполнением — нативный permission-попап
  OpenCode.

Критерий завершения: модель не может замаскировать запись под диагностическую
команду (`run_command` read-only), а изменение исполняется только после
подтверждения permission-политикой клиента и фиксируется в аудите.

## Этап 8. Zero-config соединения, описываемые моделью

Статус: реализован. Inventory, connections.yml и credentials.yml удалены:
модель описывает соединение в `open_session` (host, protocol, credentials-ссылки,
route, host key policy, legacy-алгоритмы). Остаётся только необязательный
`policy.yml`. Risky-опции (Telnet/console, plaintext-пароль, legacy) требуют
явных флагов и warning'ов; policy может запретить их полностью. Каждый
`open_session` проходит клиентский permission-попап.

- Убрать profile-конфиги и target resolver; оставить typed `OpenSpec` и
  `build_plan` с валидацией до сетевого вызова.
- Добавить per-call legacy-алгоритмы, plaintext-режим за флагом и policy
  hard-deny для insecure-опций.
- Сохранить fail-closed audit, redaction и host key store.
- Обновить unit-тесты и документацию.

Критерий завершения: на чистой установке без единого YAML `open_session` +
read-only диагностика работают, а insecure-опции не включаются без явного
флага/подтверждения.

# Исторические проверки

## MCP acceptance (Этапы 1-2)

На каждом представителе проверен поток:

```text
open_session -> run_command -> read_output -> close_session
```

Первое подключение через явный `accept_new` сохраняет ключ в локальный
`known_hosts`. Последующие соединения выполняются с `host_key_policy: strict` и
передают этот файл `SshTerminal` как `known_hosts_file`.

## Hardware validation Этапа 2

Read-only проверки через `MCPServer.call_tool` на реальном оборудовании.

| Устройство | `cli_help` | `run_command` | Примечание |
| --- | --- | --- | --- |
| Cisco IOS | работает | работает | `show ?` возвращает подсказку, Ctrl-C восстанавливает prompt |
| SNR eNOS | работает | работает | корректная форма `show interface ?` / `show interface brief` |
| D-Link DES | работает | работает | через `cli_help` без Enter — подсказка не возвращается; модель повторяет запрос с Enter (`show ?` + Enter), завершающий `?` не даёт команде выполниться |
| SNR old | работает | работает | help-pager поддерживает `space`/`q`; host key меняется при каждой загрузке, профиль `direct-snr` использует `accept_changed` |
| Huawei VRP | работает | работает | help возвращает `prompt + display `, Ctrl-C + Ctrl-U очищают строку |
| Juniper Junos | работает | работает | help возвращает `prompt + show ` с backspace bytes, Ctrl-C + Ctrl-U очищают строку |

На SNR old проверены flows `cli_help -> space -> q -> run_command`,
`show interface -> space -> q -> show version` и естественное завершение
`show interface` после 29 страниц с последующим `show version` в одной сессии.
На Junos/Huawei проверен `cli_help -> run_command` в одной сессии после silent
Ctrl-C/Ctrl-U cleanup. Если help-pager не возвращает распознанный prompt,
сессия всё ещё безопасно переводится в `failed`.

Pager на проверенных Junos и Huawei не отключается автоматически: большие
выводы возвращаются с `pager_active: true`, и модель листает их
`send_control("space")` или завершает `q`. SNR old сохраняет pager для help
и больших команд (`show interface`).

Проверка `respond` на реальном оборудовании не проводилась: confirmation
prompts обычно предшествуют write/destructive действиям.

## Local validation Этапа 2

Scripted tests покрывают `MCPServer.call_tool` contracts, `cli_help` без Enter,
prompt-tail cleanup Ctrl-C/Ctrl-U, pager continuation/abort/page limit,
confirmation allowlist, отказ от password prompt и state-bound control actions.
Отдельные тесты фиксируют короткий `cli_help_timeout` и гарантированный Ctrl-C
cleanup даже при ошибке чтения.

## OpenCode integration (Этапы 1-8)

Текущий global OpenCode profile регистрирует `network-terminal` как local stdio
MCP с рабочим каталогом проекта и timeout 65 секунд. `opencode mcp list`
подтвердил подключение сервера. На Этапах 1-8 через этот MCP выполнялся
read-only round-trip `open_session -> run_command -> close_session`; на Этапе 9
его заменил `open_session -> terminal_write -> terminal_read -> close_session`.
Device identifiers и terminal output в репозиторий не записываются.

## One-hop ProxyJump validation (прежний gateway)

Проверен реальный маршрут local -> SSH jump host (explicit `ssh_key` profile с
encrypted key и passphrase из `pass`) -> SNR old target. Bastion auth по
explicit local key file, final target по password profile. Flow
`open_session -> show version -> close_session` выполнен в одной session;
jump host key после TOFU enrollment проверяется в `strict`, target — в
`accept_changed` из-за rotating keys. В репозиторий записаны только обезличенные
выводы и hostkey не входят.

## Nested SSH validation (удалено на Этапе 9)

Проверялся реальный маршрут local -> intermediate host (password profile) -> SNR
old target: из shell промежуточного хоста выполнялся `ssh` до final target.
Маршрут `nested` удалён: теперь тот же переход делается в raw сессии через
`terminal_write("ssh ...")` + `terminal_write_secret` + `terminal_read`.

## Этап 7 validation (удалено на Этапе 9)

`run_change` удалён на Этапе 9 вместе с command policy и command gateway. На
Cisco IOS (Cisco Catalyst 2950, IOS 12.1(22)EA13) исторически проверялось
применение и откат `description` через `run_change`; в raw терминале то же
действие выполняется `terminal_write` и подтверждается клиентским permission
для `terminal_write` (если оператор его настроил).
