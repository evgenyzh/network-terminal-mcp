# Архитектура

## Назначение

`network-terminal-mcp` предоставляет модели постоянный управляемый терминал, а
не набор заранее подготовленных vendor-команд. Модель сама исследует CLI через
`?`, читает ошибки синтаксиса и продолжает работу в той же сессии.

Netmiko используется как транспортный и терминальный двигатель. Он не переводит
запросы пользователя в команды и не является инвентарем или NMS.

## Компоненты

```text
OpenCode
  |
  | MCP stdio
  v
MCP tools
  |
  +-- target resolver -------- inventory or ad-hoc target
  +-- credential resolver ---- pass/GPG
  +-- policy engine ---------- allow / ask / deny
  +-- session manager -------- lifetime, locking, output limits
  +-- audit logger ----------- JSONL, fail-closed
  +-- host key store --------- strict known_hosts or explicit TOFU
  |
  v
connection backend (implemented)
  +-- Netmiko direct SSH
   +-- Paramiko one-hop proxyjump -> Netmiko target session
  |
  v
platform registry
  +-- stock Netmiko drivers and configured aliases
  +-- local BDCOM/EcoSGE/PON adapters (planned)
```

Реализованы direct SSH, один configured SSH-only ProxyJump hop, один configured
Nested hop (SSH или Telnet через `generic_termserver` + `redispatch`), прямой
Telnet и TCP console profiles. OpenSSH PTY fallback и local adapters остаются
отдельными transport backends следующих этапов.

## MCP-инструменты Этапов 1-2

### `open_session`

Открывает direct, configured one-hop ProxyJump, configured one-hop Nested,
Telnet или console-соединение и возвращает непрозрачный `session_id`, platform,
dialect, prompt и transport warnings. Перед соединением SSH-ключ проверяется
через локальный `known_hosts`; у ProxyJump отдельно проверяются ключи bastion и
final target, у Nested — ключ intermediate host. Telnet/console не проверяют
host key.

Цель задается именем из инвентаря или одноразовым описанием `host`, `platform`,
`credentials`, `connection`, `port`. Одноразовая цель не содержит пароль и
может ссылаться только на локальные profiles.

### `run_command`

Проверяет полную строку политикой, отправляет ее с Enter и читает до prompt или
таймаута. В этой версии исполняются только решения `allow`; `ask` возвращает
`confirmation_required`, а `deny` возвращает ошибку политики. При известном
pager command возвращает первый фрагмент в состоянии `paging`, а при
распознанном device confirmation - `response_required` и allowlist ответов.
MCP не изменяет синтаксис команды.

### `run_commands`

Последовательно исполняет массив команд под одним session lock и
останавливается на первой неисполненной команде, pager или device prompt.

### `cli_help`

Проверяет отдельную policy `defaults.cli_help`, отправляет `<line>?` без Enter и
читает completion output. Вход не может содержать `?`, control characters или
structural command hazards.

Если помощь попадает в pager (`--More--`, `---- More ----`,
`---(more N%)---`), `cli_help` возвращает первый экран с `pager_active: true`
и не отправляет control bytes. Модель продолжает страницами через
`send_control(space)` либо выходит `q`. После `q` сервер ждёт распознанный
`prompt + остаток строки`, затем посылает Ctrl-C и Ctrl-U, не нажимая Enter.
Если pager не вернулся к известному prompt, session переводится в `failed`.

После непостраничной помощи CLI может либо ждать Ctrl-C и вернуть чистый
prompt, либо уже показать `prompt + остаток строки` (Junos/Huawei). Во втором
случае сервер распознаёт этот хвост, посылает Ctrl-C и Ctrl-U без ожидания
нового вывода: эти платформы очищают line buffer молча. Для CLI, показывающих
помощь только после Enter (D-Link), используется `cli_help_requires_enter`
(`<line>?` + Enter).

### `send_control`

Не принимает произвольные bytes. В `paging` разрешены `space` (следующая
страница), `q` и `ctrl-c` (отмена); для `awaiting_response` разрешен только
`ctrl-c`. После `max_pager_pages` сервер вместо следующей `space` безопасно
отправляет `q` и ожидает prompt.

### `respond`

Доступен только в `awaiting_response`. Сервер распознает ограниченный набор
confirmation prompts с явным текстом действия и `[Y/N]`, `(Y/N)`, `[yes/no]`
или `(yes/no)`, затем принимает только соответствующий token. Password,
passphrase и secret prompts не получают автоматического ответа и переводят
session в `failed`.

### `read_output`, `session_status`, `close_session`

Возвращают накопленный вывод частями, состояние либо закрывают соединение.
Вывод хранится в ограниченном session buffer; при превышении лимита старые данные
вытесняются, а `oldest_offset` сообщает доступную начальную позицию.

### Инструменты Этапа 7: изменения конфигурации

`plan_change(session_id, title, commands, safety_net?, auto_approve?)`
регистрирует план без исполнения. Сервер хранит канонический список команд и
возвращает `change_id` и hash. Требуется `Device.allow_writes` и
`defaults.write_change != deny`. Команды проходят структурную безопасность, но
не keyword-deny (reload/save/commit-команды легитимны внутри плана).

`apply_change(change_id)` двухшаговый: первый вызов возвращает канонический
список с `confirmation_required` и не исполняет, второй исполняет ровно
сохранённые команды. Опциональная `safety_net` содержит opaque `save`/`arm`/
`cancel`: `save` выполняется до команд изменений, `arm` после, после чего план
помечается как требующий отмены и `close_session` блокируется.

`abort_change(change_id)` отменяет план до исполнения. `finalize_change`
отправляет `cancel` (reload cancel/undo schedule reboot/commit) и снимает блок;
если отмена не прошла, сервер докладывает, что устройство перезагрузится само.
`close_session(force=true)` — аварийный обход блока с аудитом
`reboot_not_cancelled`.

### Инструменты следующих этапов

`raw_input` остается отключенным по умолчанию. Policy-confirmation workflow
для команд с решением `ask` не реализован и не использует `respond`.

## Жизненный цикл сессии

```text
connecting -> ready -> paging ------------+
     |          |       |                 |
     |          |       +-> awaiting_response
     |          |                 |
     v          v                 v
   failed <-----+-----------------+-----> closing -> closed
```

Каждая сессия имеет reentrant lock: одновременно выполняется одна операция или
один пакет `run_commands`. Для сессий задаются idle timeout, hard lifetime и
максимальный объем буфера.

## Подготовка терминала и paging

После подключения Netmiko запускает `session_preparation` выбранного драйвера:

- Cisco IOS: terminal width и `terminal length 0`.
- Huawei: `screen-length 0 temporary`.
- Junos: `set cli screen-length 0` и screen width.
- Остальные штатные платформы используют собственную реализацию.

После вложенного SSH/Telnet сначала используется `generic_termserver`, затем
`redispatch(..., session_prep=True)`. Поэтому подготовка выполняется именно на
конечном устройстве.

Этап 2 добавляет консервативное распознавание распространенных pager markers
(включая Junos `---(more N%)---`). Pager не продолжается автоматически: tool
возвращает первый фрагмент и `paging`, после чего модель явно выбирает
`space`, `q` или `ctrl-c`. Каждая страница пишется в bounded output buffer;
page limit прерывается `q`. В help-flow совпадение prompt допускает хвост
(`prompt + остаток строки`) только для `cli_help`; этот хвост очищается
Ctrl-C и Ctrl-U без Enter. Вне help-flow совпадение prompt строгое.

## Платформы и диалекты

Платформа описывает механику соединения. Диалект дает модели контекст о CLI, но
не обязан иметь отдельный класс.

```text
snr_29xx -> CiscoIos transport + snr_29xx dialect
snr_52xx -> CiscoIos transport + snr_52xx dialect
```

Разница между `show mac address-table` и `show mac-address-table` не является
причиной писать новый Netmiko-драйвер. Модель определяет синтаксис через
`cli_help`.

BDCOM делится как минимум на `bdcom_huawei_like` и `bdcom_cisco_legacy`, потому
что там может различаться сама механика базового CLI.

## Транспортные маршруты

- `direct` SSH: реализован; `legacy_ssh` использует Paramiko/Netmiko.
  Per-profile allowlists (`host_key_algorithms`/`kex_algorithms`/`ciphers`)
  становятся Paramiko `disabled_algorithms` и не ослабляют host key checking.
- `proxyjump`: один configured SSH hop реализован через Paramiko `direct-tcpip`
  channel и Netmiko `sock`; arbitrary `ProxyCommand` не поддерживается.
- `nested`: один configured hop реализован через `generic_termserver` +
  `redispatch`; `next_protocol` поддерживает `ssh` и `telnet`. Inner SSH
  выполняется SSH-клиентом промежуточного хоста, поэтому host key final target
  проверяет именно он; inner Telnet host key не проверяет.
- `direct` Telnet: реализован с двойным gating
  (`allow_telnet` на устройстве и `defaults.telnet: allow`), использует
  `telnet_driver` платформы, host key не проверяется.
- `console`: реализован как TCP console port терминального сервера через
  Telnet-драйвер; требует заданный `port` и тот же gating.

Маршруты состоят только из заранее определенных connection profiles. Модель не
может передать произвольную shell-команду перехода.

## Расширение Netmiko

Netmiko распространяется под MIT. Проект использует его как зависимость и не
изменяет установленный пакет.

Собственный registry разрешает штатный `device_type` либо alias из
`connections.yml`. Локальные драйверы будут наследоваться от ближайшего
штатного класса; в Этапе 1 local adapters пока возвращают понятную ошибку.
Версия Netmiko ограничена major-версией 4, потому что драйверы используют часть
его protected API. Interactive flow использует только public `send_command`,
`write_channel`, `read_until_pattern` и `read_channel_timing`. Обновление
выполняется только после tests transcript replay.
