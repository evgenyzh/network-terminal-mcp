# Стратегия тестирования

## Уровни

### Unit

- Валидация policy schema и inline connection specs (`OpenSpec`).
- Connection plan: порты, route-метки, serial-параметры, warnings, запрет
  несовместимых полей.
- Redaction паролей из ошибок и audit; добавление секретов, введённых в сессии.
- Raw terminal API: `terminal_write` (enter/control bytes/serial `\r`, лимит
  размера), `terminal_read` (таймауты, offsets, truncation),
  `terminal_write_secret` (entry вместо значения, redaction, пустая запись).
- Session lifecycle: locks, idle/lifetime timeout, max sessions, fail-closed
  поведение при transport error.
- Mutable redaction и `read_output` offsets при переполнении буфера.
- Host key store: strict unknown host, TOFU enrollment и nonstandard SSH port.
- Transport params: построение ssh/telnet/serial dict без `device_type`;
  передача `sock` (SOCKS/jump), `disabled_algorithms` и timeouts.
- Гейты: `allow_telnet`, `allow_serial`, `allow_plaintext_password`, policy
  hard-deny, валидация `pass`-entry (traversal) в spec и secret input.
- Serial device validation: относительный путь, несуществующий путь,
  не-символьное устройство, ошибка открытия.
- MCP tool registration, Pydantic input validation и worker-thread dispatch.

### Transcript replay

- Prompt discovery (`find_prompt`) как best-effort: отсутствие prompt не
  ломает сессию.
- Терминальный read-loop: `read_channel_timing` с тишиной и таймаутом,
  ANSI/CRLF нормализация, overflow обратно в buffer.
- Scripted SSH/Telnet/serial endpoints: login prompt, команды, pager, второй
  `ssh` внутри сессии, password prompt для `terminal_write_secret`.

### Integration

- Локальный fake SSH/Telnet server с заранее заданным CLI.
- Mock `pass` executable с тестовыми секретами.
- MCP client round trip для каждого tool.
- Проверка, что stdout содержит только MCP protocol, а logs уходят в stderr/file.

### Hardware lab

Только после явного разрешения пользователя для точных targets:

- одно устройство за прогон;
- сначала console/management reachability;
- никаких production config transitions без отдельного разрешения;
- после теста проверить отсутствие зависших sessions.

## Выполненная проверка (Этапы 1-9)

Проведена только после явного разрешения пользователя и без добавления IP,
hostname, serial number или fingerprint в репозиторий.

| Семейство | CLI-характер | Проверенный путь | Статус |
| --- | --- | --- | --- |
| Cisco IOS, legacy SSH | Cisco-like | TOFU, strict reconnect, `show version` | пройден |
| SNR old | Cisco-like | login, prompt detection, `show version` | пройден |
| SNR eNOS | Cisco-like | login, prompt detection, `show version` | пройден |
| D-Link DES | D-Link CLI | login, prompt detection, `show switch` | пройден |
| Huawei S6730 | VRP | login, prompt detection, `display version` | пройден |
| Juniper MX204 | Junos | login, prompt detection, `show version` | пройден |

На Этапе 9 через живой MCP подтверждены: `proxyjump` до промежуточного Linux
хоста, `terminal_write("ssh ...")` внутри сессии, ввод пароля через
`terminal_write_secret` и `terminal_read` уже на CLI целевого Huawei S6730.
Адреса, usernames и fingerprints в репозиторий не записываются.

## Матрица приемки

Для каждого устройства фиксируются:

| Проверка | Ожидаемый результат |
| --- | --- |
| Login | prompt определён или сессия остаётся ready с warning, секрет не залогирован |
| Prompt detection | best-effort, не блокирует raw I/O |
| Second hop | `ssh`/`telnet` в сессии доходит до CLI, пароль через `terminal_write_secret` |
| Single command | вывод получен через `terminal_read`, offsets корректны |
| Multiple commands | одна terminal session |
| Long output | pager листается `terminal_write`, buffer не теряет offsets |
| Invalid command | ошибка видна в выводе, session остаётся ready |
| Close | соединение и jump client закрыты |

## Обязательные негативные тесты

- Попытка передать неизвестное поле или plaintext-пароль без флага.
- `pass`-entry с traversal (`..`, абсолютный путь) в spec и secret input.
- Telnet/serial без явного флага и при policy hard-deny.
- Plaintext credentials при policy hard-deny.
- Legacy algorithms при policy hard-deny.
- Невозможность записи audit-файла.
- `terminal_write` при failed session и transport error → fail-closed.
- `terminal_read` с timeout вне границ.
- `terminal_write_secret` с пустым значением записи.
- Serial с относительным путём, несуществующим устройством и не-символьным
  устройством.
- SOCKS5 с требующейся авторизацией прокси.
- SOCKS5 с не-IP target.
- SOCKS5 при недоступном прокси/цели.
- Несовместимые route/protocol комбинации (socks+telnet, console+route,
  serial+route).

## Команды проверки

Текущий набор проверки:

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest --cov=network_terminal_mcp
uv build
```
