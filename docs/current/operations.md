# Эксплуатация 0.2.0

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
        "codemode": false
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

Внутри одной сессии можно делать вложенные переходы:

```text
terminal_write(session_id, "ssh operator@192.0.2.10")
terminal_read(session_id)
terminal_write_secret(session_id, env="DEVICE_PASSWORD")
terminal_read(session_id)
```

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
