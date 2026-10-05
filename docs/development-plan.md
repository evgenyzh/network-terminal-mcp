# План разработки

## Принципы

- Сначала read-only диагностика, затем контролируемые изменения.
- Сначала прямой SSH на тестовом оборудовании, затем сложные маршруты.
- Не писать vendor-команды в транспортном слое.
- Каждый новый транспорт должен иметь воспроизводимый transcript и
  негативные тесты.

История этапов 0-8 (прежний командно-ориентированный gateway и `nested`)
вынесена в [docs/history.md](history.md). Актуальная модель — Этап 9.

## Этап 9. Сырой терминал

Статус: реализован и проверен на живом оборудовании. Командно-ориентированные
инструменты (`run_command`, `run_commands`, `cli_help`, `send_control`,
`respond`, `run_change`) удалены вместе с command policy и nested-маршрутом.
Сессия — один постоянный stream: `terminal_write` отправляет точный ввод
(команды, клавиши, `ssh`/`telnet`), `terminal_read` возвращает вывод без
требования prompt. Первый доступ использует direct/socks/proxyjump; дальше
модель переходит между хостами сама. `terminal_write_secret` вводит значение
`pass` в живой prompt, не раскрывая его. Добавлены локальные serial-консоли
(`/dev/tty*`, pyserial) за `allow_serial=true`. Несколько сессий держатся
одновременно; idle/lifetime defaults увеличены.

- Удалить nested и command gateway; оставить direct/socks/proxyjump и raw I/O.
- Добавить `terminal_write`/`terminal_read`/`terminal_write_secret` и
  mutable redaction для секретов, введённых в сессии.
- Добавить serial-транспорт с проверкой символьного устройства.
- Обновить тесты, документацию, skill и `opencode.json`.
- Встроить инструкцию по эксплуатации в MCP: `instructions` в initialize и
  ресурс `network-terminal://usage` (исходник `src/network_terminal_mcp/usage.md`).

Критерий завершения: `open_session` + `terminal_write("ssh ...")` +
`terminal_write_secret` + `terminal_read` доводят до CLI за двумя SSH-хопами в
одной сессии, а audit содержит ввод без значений секретов.

## Дальнейшие шаги

- Распознавание sensitive-команд (`conf t`, `system-view`, `commit`, `reload`)
  с pending-вводом и подтверждением через клиентское permission-окно.
- Опциональные transcripts сессий.
- Hardware-проверка serial, Telnet и TCP console на реальных целях.
