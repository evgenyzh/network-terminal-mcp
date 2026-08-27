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
  |
  v
connection backends
  +-- Netmiko SSH/Telnet
  +-- OpenSSH PTY fallback for legacy cases
  +-- terminal-server hops and redispatch
  +-- console-port sessions
  |
  v
platform adapters
  +-- stock Netmiko drivers
  +-- local BDCOM/EcoSGE/PON adapters
```

## MCP-инструменты

### `open_session`

Открывает соединение и возвращает непрозрачный `session_id`, platform, prompt,
режим и предупреждения транспорта. При необходимости выполняет несколько
переходов через терминальные серверы.

Цель задается именем из инвентаря или одноразовым описанием `host`, `platform`,
`credential_profile`, `connection_profile`. Одноразовая цель не содержит пароль.

### `run_command`

Проверяет полную строку политикой, отправляет ее с Enter и читает до prompt,
интерактивного вопроса или таймаута. MCP не изменяет синтаксис команды.

### `run_commands`

Последовательно исполняет массив команд в одной сессии. Останавливается на
первой ошибке политики, неожиданном интерактивном prompt или потере сессии.

### `cli_help`

Отправляет префикс команды и `?` без Enter, читает подсказку, затем очищает
незавершенную строку через подходящий для платформы control sequence. Этот
инструмент позволяет исследовать неизвестный CLI без исполнения префикса.

### `respond`

Отвечает только на уже распознанный интерактивный prompt. Сессия хранит тип
ожидаемого ответа и допустимые значения. Произвольный raw-ввод через этот
инструмент запрещен.

### `send_control`

Поддерживает ограниченный набор: `ctrl-c`, `ctrl-u`, `ctrl-z`, `space`, `q`.
Политика зависит от состояния сессии. Например, `space` автоматически допустим
в pager, а `ctrl-z` может потребовать подтверждения.

### `read_output`, `session_status`, `close_session`

Возвращают накопленный вывод, состояние либо закрывают соединение. Большой
вывод сохраняется в защищенный runtime-каталог и читается частями.

### `raw_input`

Резервный инструмент для неизвестных интерактивных протоколов. По умолчанию
отключен. Включается только отдельной политикой и всегда требует подтверждения.

## Жизненный цикл сессии

```text
created -> connecting -> preparing -> ready
                                  |      |
                                  |      +-> waiting_for_response
                                  |      +-> paging
                                  |      +-> busy
                                  v
                               failed

ready/busy/waiting -> closing -> closed
```

Каждая сессия имеет lock: одновременно выполняется только одна операция. Для
сессий задаются idle timeout, hard lifetime и максимальный объем буфера.

## Подготовка терминала и paging

После подключения Netmiko запускает `session_preparation` выбранного драйвера:

- Cisco IOS: terminal width и `terminal length 0`.
- Huawei: `screen-length 0 temporary`.
- Junos: `set cli screen-length 0` и screen width.
- Остальные штатные платформы используют собственную реализацию.

После вложенного SSH/Telnet сначала используется `generic_termserver`, затем
`redispatch(..., session_prep=True)`. Поэтому подготовка выполняется именно на
конечном устройстве.

Адаптер также задает pager patterns. Если отключение paging не удалось, session
manager распознает pager и отправляет `space`; при превышении лимита пытается
корректно выйти через `q` или `ctrl-c`.

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

- `direct`: прямой SSH или Telnet.
- `proxyjump`: TCP-переход через OpenSSH ProxyJump/ProxyCommand.
- `nested`: shell терминального сервера, затем SSH/Telnet внутри него.
- `console`: соединение с выделенным TCP-портом либо команда выбора консоли.

Маршруты состоят только из заранее определенных connection profiles. Модель не
может передать произвольную shell-команду перехода.

## Расширение Netmiko

Netmiko распространяется под MIT. Проект использует его как зависимость и не
изменяет установленный пакет.

Локальные драйверы наследуются от ближайшего штатного класса. Собственный
registry создает стандартный `ConnectHandler` либо локальный класс напрямую.
Версия Netmiko ограничена major-версией 4, потому что драйверы используют часть
его protected API. Обновление выполняется только после тестов transcript replay.
