# Результаты проверок 0.2.0

Проверка выполнена 31 октября 2026 года для версии `0.2.0`. Реальные адреса,
hostnames, usernames, fingerprints и содержимое секретов в документ не
включались.

## Автоматические проверки

| Проверка | Результат |
| --- | --- |
| `uv run pytest` | 103 passed |
| `uv run ruff check .` | чисто |
| `uv run mypy src` | чисто, 12 source files |
| Markdown links | проверены |
| `git diff --check` | чисто |
| stdio initialize + `session_status` | ошибка с конкретным текстом |
| fake SSH/Telnet в PTY | persistent I/O, secret prompt, early exit |
| Serial transport | unit-тесты с fake pyserial |
| wheel smoke | wheel 0.2.0 установлен в отдельный venv; `check` прошёл |
| живой SSH через jump host | вход по ключу, persistent shell, несколько команд, close |

## Что именно подтверждено

- `open_session(command)` принимает только `command`; старые structured-поля
  отклоняются.
- SSH/Telnet запускаются как реальные процессы в PTY и сохраняют состояние
  между вызовами.
- Первый `terminal_read` возвращает стартовый banner, не теряя его.
- Ранний exit возвращает exit code и реальный stderr.
- Password prompt обрабатывается `terminal_write_secret`; значение отсутствует
  в результате и audit.
- Cursor-чтение не повторяет уже прочитанный вывод.
- `truncated=true` и `next_output_offset` позволяют продолжить чтение.
- Неожиданная внутренняя ошибка не раскрывает модели traceback.
- stdio-вызов с неизвестной сессией возвращает:
  `session_error: unknown session ...`, а не общую заглушку.
- `close_session` и shutdown закрывают дочерние PTY.

## Живая SSH-проверка

С отдельного разрешения выполнена одна проверка через реальный OpenSSH и jump
host:

- host key prompt обработан; fingerprint сверен с уже известным ключом, в
  документацию не записан;
- вход по SSH-ключу без интерактивного пароля;
- открыт persistent shell на тестовом Linux-хосте;
- в одной сессии выполнены `hostname`, `whoami`, `pwd`, `uname -sr`;
- повторное чтение не вернуло старую историю;
- сессия закрыта через `close_session`.

Ручное закрытие OpenSSH завершилось с кодом 255 из-за SIGHUP; это ожидаемое
следствие принудительного закрытия PTY, состояние сессии — `closed`.

## Что не проверялось

- Реальные сетевые устройства и production credentials.
- Реальный Telnet/TCP console terminal server.
- Реальный serial-кабель `/dev/ttyUSB*`.
- Windows PTY.
- Длительная эксплуатация и ротация audit.

Остальные живые проверки требуют отдельного разрешения на конкретные цели и
команды. Реальные адреса, hostnames, usernames и fingerprints в документ не
включаются.
