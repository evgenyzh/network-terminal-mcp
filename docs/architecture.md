# Архитектура

## Назначение

`network-terminal-mcp` предоставляет модели постоянный сырой терминал к
сетевому оборудованию, а не набор заранее подготовленных vendor-команд.
Модель сама исследует CLI через `?`, читает ошибки синтаксиса, продолжает
работу в той же сессии и при необходимости запускает внутри неё следующий
`ssh` или `telnet`.

Соединение тоже описывает модель: в `open_session` она передаёт host, protocol,
credentials (ссылки, не секреты), опциональный route (socks/proxyjump для
первого SSH), host key policy и, при необходимости, legacy SSH алгоритмы для
конкретного host. Локальный inventory/connection/credential YAML не нужен;
остаётся только необязательный `policy.yml`.

Транспорт реализован в `src/network_terminal_mcp/terminal.py` как сырой
терминальный слой: `SshTerminal` (Paramiko `invoke_shell`), `TelnetTerminal`
(telnetlib3 для Telnet и TCP console) и `SerialTerminal` (pyserial для
`/dev/tty*`) без vendor-драйверов и без понятия «платформа». Слой не переводит
запросы пользователя в команды и не является инвентарем или NMS.

## Компоненты

```text
OpenCode
  |
  | MCP stdio
  v
MCP tools
  |
  +-- connection spec -------- Pydantic OpenSpec: host/route/credential refs
  +-- connection plan -------- validates defaults, ports, serial params, warnings
  +-- credential resolver ---- pass / explicit key file / plaintext opt-in
  +-- policy gates ----------- per-call allow flags + policy.yml defaults
  +-- session manager -------- lifetime, locking, raw write/read, redaction
  +-- audit logger ----------- JSONL, fail-closed
  +-- host key store --------- strict known_hosts or explicit TOFU
  |
  v
connection backend
  +-- SshTerminal (Paramiko invoke_shell) --- direct SSH / socks / proxyjump
  +-- TelnetTerminal (telnetlib3) ----------- direct Telnet / console TCP
  +-- SerialTerminal (pyserial) ------------- local /dev/tty* console
```

Реализованы direct SSH/Telnet/console/serial, один model-described SOCKS5 hop и
один SSH ProxyJump hop для первого подключения. Дальнейшие переходы (второй
`ssh`, `telnet`) модель выполняет сама внутри уже открытой сессии. Тип
устройства заранее не известен и не требуется: модель читает banner/вывод и
определяет CLI сама через generic tools.

## MCP-инструменты

### Инструкция для модели

Сервер поставляет модель-ориентированную инструкцию двумя способами: краткий
контракт `instructions` в MCP initialize (OpenCode вкладывает его в контекст
модели) и полный мануал как ресурс `network-terminal://usage`. Исходник обоих —
`src/network_terminal_mcp/usage.md` и `usage.py`; их нужно держать
синхронизированными со skill и этим документом.

### `open_session`

Принимает валидированный `OpenSpec` и открывает постоянное соединение: host,
protocol (`ssh`/`telnet`/`console`/`serial`), credentials, port final target,
опциональный route, host key policy, legacy-алгоритмы и serial-параметры.
Секреты в вызове недопустимы, кроме явного plaintext-режима за флагом. Все
risky-опции (`telnet`, `serial`, plaintext, legacy) помечаются warning'ами и
требуют явного per-call флага; policy может запретить их полностью. Соединение
строится в `connections/plan.py` до любого сетевого вызова.

Host key проверяется через локальный `known_hosts`; у ProxyJump отдельно
проверяются ключи jump host и final target. Telnet/console/serial не проверяют
host key. Каждый вызов `open_session` проходит через клиентский
permission-попап OpenCode.

Запуск `ssh`/`telnet` внутри сессии — обычный ввод модели: host key
следующего hop проверяет SSH/Telnet-клиент промежуточного хоста, а не локальный
store.

### `terminal_write`

Отправляет точный ввод в терминальный поток: команды, одиночные клавиши
(`space`, `q`, Ctrl-C как `\u0003`) и переходы вида `ssh user@host` или
`telnet host`. `enter=true` (по умолчанию) добавляет перевод строки транспорта
(`\n`, на serial — `\r`). Полная строка пишется в audit с masking известных
секретов. Пароли и другие секреты должны отправляться только через
`terminal_write_secret`.

### `terminal_read`

Читает всё, что пришло, пока поток не стихнет (0.5 с тишины) или не истечёт
`timeout`. Никакой формы prompt не требуется: pager-экраны, password prompts,
banner, shell-вывод и ошибки CLI возвращаются как есть. Возвращается только
вывод, пришедший с предыдущего чтения; при усечении `next_output_offset`
указывает на первый непрочитанный символ. Прочитанное накапливается в bounded
session buffer и доступно через `read_output` (продолжение с курсора либо явный
offset); при переполнении `oldest_offset` сообщает границу.

### `terminal_write_secret`

Разрешает запись `pass` внутри сервера и вводит её значение в текущий prompt
(например, `Password:`). Только имя записи и число байт попадают в audit;
значение не появляется в аргументах, результатах и audit и добавляется в
redaction текущей сессии. Обычный `terminal_write` для секретов использовать
нельзя: он логируется целиком.

### `read_output`, `session_status`, `close_session`

`read_output` без `offset` возвращает непрочитанный буфер с курсора чтения и не
дублирует историю в контексте; явный `offset` — осознанный доступ к старому
выводу. `session_status` и `close_session` возвращают состояние (prompt,
warnings, lifetime) либо закрывают соединение. Сессии живут в памяти сервера и
не переживают рестарт.

## Жизненный цикл сессии

```text
connecting -> ready -> failed
                 |       |
                 v       v
              closing -> closed
```

Каждая сессия имеет reentrant lock: одновременно выполняется одна операция.
Задаются idle timeout (по умолчанию 1 час), hard lifetime (24 часа) и
максимальный объем буфера. Один процесс держит до `max_open_sessions` (10)
независимых сессий, поэтому несколько устройств можно держать открытыми
одновременно и переключаться без переподключения.

## Определение prompt и устройства

После подключения сервер делает best-effort попытку определить prompt первым
Enter. Если CLI не показывает распознаваемый prompt (serial-консоль в загрузке,
pager с самого начала), сессия остаётся `ready` с warning; модель работает
через `terminal_read`/`terminal_write`. Специфичной per-driver подготовки нет.

Проект не знает тип устройства заранее и нигде его не хранит: поля platform нет
ни на соединении, ни в `SessionInfo`, ни в audit, ни в аргументах
MCP-инструментов. Модель определяет производителя и синтаксис CLI по banner и
первым выводам, затем исследует команды через `?` и чтение ошибок.

## Транспортные маршруты

- `direct` SSH: `SshTerminal` на Paramiko `invoke_shell`. Legacy-алгоритмы
  передаются per-call списками и превращаются в `disabled_algorithms` только для
  этого соединения; host key checking не ослабляется.
- `socks`: целевой сокет создаётся модулем `socks` через локальный SOCKS5-прокси
  (обычно `ssh -D`) и передаётся `SshTerminal` как `sock`; host key final target
  проверяется через тот же SOCKS-сокет. Прокси не резолвит имена, поэтому target
  должен быть IP-адресом.
- `proxyjump`: один SSH hop через Paramiko `direct-tcpip` channel; используется,
  чтобы быстро попасть на bastion или terminal server и дальше работать в его
  shell.
- `direct` Telnet и `console`: `TelnetTerminal` на telnetlib3 с обязательным
  per-call `allow_telnet=true`; host key не проверяется.
- `serial`: `SerialTerminal` на pyserial; `host` — абсолютный путь `/dev/tty*`,
  который должен быть символьным устройством. Аутентификации и транспорта
  защиты нет; требуется `allow_serial=true`.

Маршруты состоят только из typed-полей `open_session`. Модель не может передать
произвольную shell-команду перехода или literal `ProxyCommand`; но внутри уже
открытой сессии ввод не ограничен по синтаксису — это и есть смысл сырого
терминала.

## Сырой терминальный слой

`SshTerminal`, `TelnetTerminal` и `SerialTerminal` реализуют общий
`TerminalConnection` protocol: `find_prompt`, `write_channel`,
`read_channel_timing`, `disconnect` (плюс низкоуровневые helpers). SessionManager
собирает transport params и передает их в подходящий класс: ключи `transport`
("ssh"|"telnet"|"serial"), `host`/`device`, `port`, `username`,
`password`/`key_file`/`passphrase`, serial-параметры, `known_hosts_file`,
timeouts, опциональный `sock` (SOCKS/jump) и опциональный
`disabled_algorithms`. Среди параметров нет `device_type`: слой открывает любой
терминал, читает вывод, а интерпретацию оставляет модели.
