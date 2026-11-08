# Тестирование 0.3.1

## Обязательные проверки

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

## Unit

- разбор SSH/Telnet/Serial, пустые и неизвестные команды;
- отсутствие executable;
- serial defaults, baudrate и ошибки устройства;
- строго один источник секрета: `value`, `env`, `file`;
- пустой секрет, отсутствующая переменная, нечитаемый файл;
- redaction секрета во вводе, выводе и ошибках;
- `NetworkMCPError -> ToolError` с сохранением текста;
- сокрытие неожиданных внутренних исключений;
- cursor/truncation/bounded buffer;
- audit offsets и fail-closed logger;
- переходы `ready/failed/closed`, exit code и shutdown;
- лимит одновременных сессий;
- `last_output_at`/`idle_seconds`: стартовый вывод, новый вывод, пустое чтение и
  сессия без вывода;
- переход сессии в `failed` после ошибки записи без потери `close_session`.

## PTY integration без реальных устройств

Тесты создают временные исполняемые `ssh` и `telnet` со shebang Python и
подставляют их в `PATH`. Проверяются:

- запуск через `open_session(command)`;
- стартовый banner и первый `terminal_read`;
- несколько команд в одном процессе без переподключения;
- password prompt и `terminal_write_secret`;
- ранний exit с кодом и stderr;
- естественный exit после успешной работы;
- timeout записи при клиенте, который перестал читать stdin;
- закрытие и освобождение процесса;
- отсутствие секрета в audit.

Эти тесты используют настоящий POSIX PTY, но не сеть и не оборудование.

## MCP contract

- только семь инструментов;
- `open_session` имеет единственное поле `command`;
- неизвестные structured-поля отклоняются;
- ожидаемая ошибка видна модели как текст с кодом;
- внутренний crash не раскрывает детали;
- usage instructions и ресурс содержат новый workflow;
- компактные ответы, диагностические поля и продолжение вывода на границе
  65 536 байт;
- stdio black-box вызов возвращает `session_error: unknown session ...`, а не
  общий `Error executing tool ...`.

## Wheel smoke

Релизная проверка включает сборку wheel и запуск `network-terminal-mcp check`
из чистой установки. Проверяются зависимости `ptyprocess` и `pyserial`, команды
`ssh`/`telnet` и audit directory.

## Живые проверки

Автоматические тесты не подключаются к реальным устройствам. Живые сценарии
выполняются только с отдельного разрешения пользователя для конкретных целей и
команд:

- SSH с config/agent;
- SSH с password/passphrase;
- Telnet/TCP console;
- Serial при наличии hardware;
- host key failure, неверные credentials, недоступный target.

Результаты без реальных адресов, hostnames, usernames и секретов фиксируются в
[validation.md](validation.md).
