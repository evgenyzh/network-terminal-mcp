# Эксплуатация 0.4.0

## Требования

- POSIX/Linux (PTY и signals);
- Python 3.12+;
- системный `ssh` для SSH-сессий;
- системный `telnet` для Telnet/TCP console;
- `pyserial` устанавливается вместе с пакетом;
- никаких обязательных конфигурационных файлов, inventory или `pass`.

## Установка

```bash
uv tool install network-terminal-mcp
```

Бинарь появится в `~/.local/bin`. Обновление:

```bash
uv tool upgrade network-terminal-mcp
```

Проверка:

```bash
network-terminal-mcp check
```

`check` показывает версию, пути `ssh`/`telnet`, доступность pyserial и audit
каталога. Отсутствие только `telnet` не делает установку нерабочей; отсутствие
обоих клиентов — ошибка.

## OpenCode V2

Рекомендуется прямой список инструментов без Code Mode:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "network-terminal": {
        "type": "local",
        "command": ["/home/USER/.local/bin/network-terminal-mcp"],
        "codemode": false,
        "disabled": false
      }
    }
  }
}
```

`codemode: false` оставляет `open_session` и остальные инструменты в native tool
list. Обязательный permission gate для `open_session` не нужен. Клиент может
добавить собственный `ask`, но это не часть установки.

## Открытие сессии

`open_session` принимает только `command`:

```text
ssh operator@192.0.2.10
ssh -J operator@bastion.example.net operator@192.0.2.10
ssh -i ~/.ssh/id_ed25519 -p 2222 operator@192.0.2.20
ssh -o KexAlgorithms=diffie-hellman-group14-sha1 legacy@192.0.2.21
telnet 192.0.2.30 23
telnet 192.0.2.31 2001
serial /dev/ttyUSB0 115200
```

SSH использует локальные `~/.ssh/config`, agent, ключи, ProxyJump,
ProxyCommand и любые переданные опции. Аргументы SSH/Telnet не фильтруются.
Shell не используется: `|`, `&&`, redirection и подстановки в команде не
выполняются.

`ready` означает «процесс запущен, в терминал можно писать», а не «логин и CLI
prompt завершены». Начальный banner и prompt читаются через `terminal_read`.

## Основной цикл

```text
open_session("ssh operator@192.0.2.10")
terminal_read(session_id)                     # banner / login prompt
terminal_write(session_id, "show version")
terminal_read(session_id)
terminal_write(session_id, "show interface brief")
terminal_read(session_id)
close_session(session_id)
```

- `terminal_read` возвращает только новый вывод.
- Если вывод усечён, продолжайте с `next_output_offset` через `read_output`.
- `read_output` без offset не повторяет уже прочитанное.
- Одиночные клавиши: `terminal_write(..., enter=false)`, например `" "` для
  pager, `"q"` для выхода, `"\u0003"` для Ctrl-C.
- На serial `enter=true` добавляет `\r`, на SSH/Telnet — `\n`.
- Если SSH CLI ждёт DSR или требует `\r`, см.
  [terminal cursor queries](../../src/network_terminal_mcp/usage.md#terminal-cursor-queries).

Внутри одной сессии можно делать вложенные переходы:

```text
terminal_write(session_id, "ssh operator@192.0.2.10")
terminal_read(session_id)
terminal_write_secret(session_id, env="DEVICE_PASSWORD")
terminal_read(session_id)
```

## Нестабильные соединения

`ready` означает, что жив локальный процесс `ssh`/`telnet`, а не что удалённая
сторона отвечает. При «чёрной дыре» в сети локальный клиент может оставаться
живым, и сессия будет выглядеть рабочей, хотя канал уже мёртв.

Для SSH добавляйте явные параметры контроля связи:

```text
ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 operator@192.0.2.10
```

Тогда при отсутствии ответа клиент завершится примерно через 45 секунд, а
следующий `session_status` или `terminal_read` покажет `failed`. Значения
подбирайте под качество канала.

- OpenSSH keepalive по умолчанию выключен; TCP-keepalive ядра может обнаружить
  простой только через часы.
- У Telnet нет MCP-уровня keepalive. При сомнении закройте сессию и откройте
  новую.
- После сна ноутбука, смены Wi-Fi/VPN или долгой паузы считайте все открытые
  сессии подозрительными. Запись в полуразрушенное TCP-соединение может
  выглядеть успешной, хотя данные не дойдут до устройства. Закройте и
  переоткройте сессию, затем выполните безвредную проверку.
- `terminal_read` ограничен `timeout` (по умолчанию 60 с, максимум 300 с).
  Пустой вывод означает только отсутствие нового вывода, а не исправность
  канала. Для быстрой проверки передавайте короткий timeout, например 5 с.
- `terminal_write` ограничен 10 секундами. Если клиент перестал читать ввод,
  вызов вернёт `transport_error: terminal write timed out...`, а сессия станет
  `failed`. `close_session` после этого работает. Часть ввода могла уже уйти;
  проверьте состояние устройства, прежде чем повторять команду.
- `session_status` возвращает `last_output_at` и `idle_seconds`. Большой
  `idle_seconds` нормален для молчащего устройства; это подсказка, а не
  доказательство обрыва.
- Автоматического idle timeout нет. Закрывайте ненужные сессии: «зависшие»
  `ready`-сессии занимают слот из 10.

## Секреты

Ровно один источник:

```json
{"session_id": "...", "value": "secret"}
```

```json
{"session_id": "...", "env": "DEVICE_PASSWORD"}
```

```json
{"session_id": "...", "file": "/run/secrets/device-password"}
```

- `value` — простая строка, но она остаётся в контексте модели и истории
  клиента.
- `env` — переменная окружения MCP-процесса.
- `file` — локальный файл; один завершающий CR/LF удаляется.
- Значение не возвращается и не пишется в audit.
- После отправки оно маскируется в последующем выводе этой сессии.

Не передавайте пароль обычным `terminal_write` и не включайте его в `command`:
сервер не сможет удалить его из истории клиента и audit.

## Ошибки

- `executable_not_found: telnet executable was not found on PATH` — клиент не
  установлен.
- `transport_error: ssh exited with code 255 during startup: ...` — реальный
  вывод OpenSSH, например host key verification failed.
- `session_error: unknown session ...` — неверный ID.
- `credential_error: secret file is not readable: ...` — проблема источника
  секрета.
- `session_error: session ... is not ready: failed: process exited with code N` —
  процесс завершился; прочитайте остаток вывода и откройте новую сессию.
- `transport_error: terminal write timed out...` — клиент перестал принимать
  ввод; сессия помечена `failed`, её можно закрыть и открыть заново.

Если процесс завершился уже после открытия, `session_status` покажет `closed`
или `failed`, `exit_code` и сохранённый `error`.

## Serial

`serial DEVICE [BAUDRATE]` использует pyserial, 8N1, по умолчанию 9600.
Устройство должно существовать и быть character device. Ошибки доступа и
занятого порта возвращаются как `transport_error`.

## Завершение

`close_session` завершает клиент и сохраняет финальный статус. Закрытая сессия
остаётся доступной для `session_status`; писать в неё нельзя. При остановке MCP
все дочерние процессы закрываются.

При ручном закрытии OpenSSH может показать `exit_code=255`: это SIGHUP от
принудительного завершения PTY, а не ошибка MCP. Состояние сессии остаётся
`closed`.

Автоматического idle timeout нет. Одновременно можно держать до 10 сессий.

## Ограничения

- Windows PTY не поддерживается.
- MCP остаётся локальным stdio-процессом; сетевого listener нет.
- Одноразовые `run_command` отсутствуют: для `ssh host 'command'` используйте
  обычный shell.
- Обязательной ротации audit пока нет; файл находится в XDG state directory.
