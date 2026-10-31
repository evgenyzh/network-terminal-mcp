# Результаты проверок 0.2.1

Проверка выполнена 31 октября 2026 года для версии `0.2.1`. Реальные адреса,
hostnames, usernames, fingerprints и содержимое секретов в документ не
включались.

## Автоматические проверки

| Проверка | Результат |
| --- | --- |
| `uv run pytest` | 110 passed |
| `uv run ruff check .` | чисто |
| `uv run mypy src` | чисто, 12 source files |
| Markdown links | проверены |
| `git diff --check` | чисто |
| stdio initialize + `session_status` | ошибка с конкретным текстом |
| fake SSH/Telnet в PTY | persistent I/O, secret prompt, early exit |
| Serial transport | unit-тесты с fake pyserial |
| PTY write timeout | блокирующийся клиент завершает запись ошибкой |
| idle metadata | `last_output_at` и `idle_seconds` обновляются по выводу |
| wheel smoke | wheel 0.2.1 установлен в отдельный venv; `check` прошёл |
| живой SSH через jump host (0.2.0) | вход по ключу, persistent shell, несколько команд, close |

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
- PTY-запись с нечитающим клиентом завершается по `write_timeout`, а не висит.
- После write timeout сессия переходит в `failed`, но `close_session` работает.
- `last_output_at` не обновляется пустым чтением; `idle_seconds` растёт, пока
  нового вывода нет.

## Живая SSH-проверка

Живая проверка ниже выполнена для `0.2.0`. В `0.2.1` SSH-путь не менялся:
добавлены только bounded write и метаданные простоя, поэтому отдельная live-
проверка для патча не выполнялась.

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
