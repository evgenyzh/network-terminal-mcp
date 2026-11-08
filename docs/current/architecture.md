# Архитектура 0.3.0

`network-terminal-mcp` даёт модели один постоянный сырой терминал, открытый
обычной командой. Сервер не знает vendor-команд, не ищет prompt и не заменяет
интерактивный CLI набором заранее подготовленных действий.

## Поток данных

```text
MCP client (stdio)
  |
  v
server.py
  |-- ToolError boundary для ожидаемых NetworkMCPError
  |-- MCP tools + usage instructions/resource
  v
SessionManager
  |-- command parser
  |-- lifecycle, per-session lock
  |-- bounded output, read cursor
  |-- secret resolution, redaction
  |-- fail-closed audit
  |
  +-- PtyTerminal (ssh / telnet) --- ptyprocess
  |
  +-- SerialTerminal ---------------- pyserial
```

Конфигурационных файлов, inventory, connection plans, credential resolver,
policy gates, SOCKS-клиента и собственного `known_hosts` нет.

## Команда подключения

`open_session` принимает только `command`. `commands.py` разбирает строку через
`shlex.split(..., posix=True)` и никогда не запускает shell.

- `ssh ...` — аргументы без изменений передаются системному OpenSSH;
- `telnet ...` — аргументы без изменений передаются системному Telnet;
- `serial DEVICE [BAUDRATE]` — разбирается в устройство и скорость, по умолчанию
  9600 и 8N1.

Отсутствующий executable возвращает `executable_not_found`. Неизвестная или
пустая команда возвращает `command_error`. SSH options, aliases, config,
agent, ProxyJump и legacy algorithms обрабатывает сам OpenSSH, а не сервер.

## Терминалы

`PtyTerminal` запускает SSH/Telnet в отдельном POSIX PTY. Он читает доступный
вывод, ждёт короткой тишины, отслеживает завершение процесса и возвращает exit
code. `SerialTerminal` открывает character device через pyserial.

PTY-запись неблокирующая и ограничена `write_timeout` (10 с): если клиент
перестал читать stdin, запись завершается `transport_error`, а сессия — `failed`.
Это не даёт одному зависшему вызову удерживать lock сессии бесконечно.
Serial-запись использует `write_timeout=10` самого pyserial.

Транспорт отвечает только за:

- запуск и остановку процесса/устройства;
- запись текста с верхней границей времени;
- чтение до тишины или timeout;
- liveness и exit code.

Транспорт не интерпретирует вывод, не ждёт prompt и не выполняет команды
устройства.

## Жизненный цикл

```text
ready -> closing -> closed
   |
   v
 failed
```

- `open_session` делает короткое стартовое чтение.
- Если процесс завершился в стартовом окне, вызов возвращает `transport_error`
  с exit code и хвостом вывода.
- Если процесс жив, сессия получает `ready`; начальный вывод остаётся в буфере
  и возвращается первым `terminal_read`.
- Естественное завершение с кодом 0 переводит сессию в `closed`; ненулевой код
  — в `failed` с сохранённым `error` и `exit_code`.
- `close_session` завершает процесс и сохраняет финальный статус. Закрытая
  сессия остаётся доступной для `session_status`.
- `SessionInfo` содержит `last_output_at` (время последнего полученного вывода)
  и `idle_seconds` (секунды с этого момента или с создания сессии, если вывода
  ещё не было). Это наблюдаемость, а не автоматическое определение обрыва.
- Idle/lifetime timeout отсутствует: сессия живёт до закрытия, выхода клиента
  или остановки MCP. Liveness проверяется только по локальному процессу и
  обновляется лениво, при вызове инструмента.
- При остановке MCP все дочерние терминалы закрываются через lifespan.

Одновременно разрешено до 10 сессий; лимит встроен в код.

Ответы MCP компактны: открытие возвращает ID и состояние, запись — `ok`, чтение
— вывод, закрытие — состояние. Предупреждения, ошибки, необычные состояния и
данные для продолжения возвращаются при необходимости. `ok` подтверждает запись
локальному клиенту, а не выполнение команды устройством. Полные метаданные
доступны через `session_status`; схемы ответов изменены относительно 0.2.1.

## Чтение и буфер

- `terminal_read` возвращает только непрочитанный вывод.
- Непрочитанный стартовый banner или хвост усечённого чтения возвращается сразу,
  без ожидания нового вывода.
- Иначе чтение идёт до 0.5 с тишины или `timeout`.
- Вывод ограничен `max_inline_output_bytes` (по умолчанию 65 536 байт); продолжение доступно через
  `next_output_offset`.
- Общий буфер ограничен; старые символы вытесняются, `oldest_offset` показывает
  начало доступного диапазона.
- `read_output` без `offset` продолжает с курсора и не повторяет историю.
- Явный `offset` — осознанное чтение старого буфера.
- `last_output_at` обновляется при поступлении нового вывода, а не при его
  чтении из буфера; повторное чтение старого вывода не делает сессию «свежей».

## Секреты

`terminal_write_secret` принимает ровно один источник: `value`, `env` или
`file`. Значение:

- записывается в терминал с переводом строки транспорта;
- добавляется в redactor текущей сессии;
- не возвращается в результате;
- не попадает в audit (там только тип источника и число байт).

`value` остаётся в аргументах вызова, контексте модели и истории клиента; это
документированный компромисс. `env` и `file` предпочтительнее.

## Ошибки

Ожидаемые исключения наследуются от `NetworkMCPError`:

```text
command_error
executable_not_found
credential_error
session_error
audit_error
transport_error
```

На границе MCP они преобразуются в `ToolError`, поэтому модель видит настоящий
код и текст, например:

```text
transport_error: ssh exited with code 255 during startup:
Host key verification failed.
```

Неожиданные внутренние исключения остаются crash-ошибкой SDK и не раскрывают
traceback или внутренние пути модели. Генератор аргументов инструментов
настроен на `additionalProperties: false`, поэтому удалённые structured-поля
отклоняются, а не игнорируются.

## Аудит

JSONL-аудит пишется в `$XDG_STATE_HOME/network-terminal-mcp/audit.jsonl` или
`~/.local/state/network-terminal-mcp/audit.jsonl`.

Записываются:

- `open_session`: команда, protocol, outcome, ошибка, session ID;
- `terminal_write`: ввод после redaction, enter, bytes;
- `terminal_write_secret`: только source и bytes;
- чтения: offsets, bytes, cursor;
- статус и закрытие: state, exit code, error.

Pre-action audit остаётся fail-closed: если запись о начале действия не удалась,
действие не выполняется. Каталог создаётся с `0700`, файл — с `0600`.

## Основные файлы

- `src/network_terminal_mcp/server.py` — MCP contract и ToolError boundary.
- `src/network_terminal_mcp/commands.py` — разбор команд.
- `src/network_terminal_mcp/terminal.py` — PTY и serial transports.
- `src/network_terminal_mcp/sessions/manager.py` — lifecycle, buffer, cursor,
  secrets, audit.
- `src/network_terminal_mcp/sessions/models.py` — MCP result models.
- `src/network_terminal_mcp/usage.md` — полный мануал для модели.
