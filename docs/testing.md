# Стратегия тестирования

## Уровни

### Unit

- Валидация inventory, connection и policy schemas.
- Разрешение target/profile без доступа к произвольной записи `pass`.
- Redaction паролей из ошибок и audit.
- Command policy: allow, ask, deny, переносы строк и metacharacters.
- Session state machine, locks, timeout и output limits.
- `cli_help` cleanup, pager/control transitions, confirmation allowlist и
  отказ от secret prompt.
- Host key store: strict unknown host, TOFU enrollment и nonstandard SSH port.
- Transport params: построение ssh/telnet dict без `device_type`; передача
  `sock` (SOCKS/jump), `disabled_algorithms` и timeouts в `SshTerminal`/
  `TelnetTerminal`.
- MCP tool registration, Pydantic input validation и worker-thread dispatch.

### Transcript replay

- Prompt discovery (`find_prompt`).
- Терминальный expect-loop: `read_until_pattern` с таймаутом, `send_command`
  с expect и strip prompt/command, overflow обратно в buffer.
- `cli_help` без Enter и последующая очистка строки.
- Обычная команда, длинный вывод и pager.
- Потеря соединения на каждом этапе nested route.

### Integration

- Локальный fake SSH/Telnet server с заранее заданным CLI.
- Mock `pass` executable с тестовыми секретами.
- MCP client round trip для каждого tool.
- Проверка, что stdout содержит только MCP protocol, а logs уходят в stderr/file.

### Hardware lab

Только после явного разрешения пользователя для точных targets:

- одно устройство за прогон;
- только диагностические команды;
- сначала console/management reachability;
- никаких production config transitions без отдельного разрешения;
- после теста проверить отсутствие зависших sessions.

## Выполненная проверка Этапа 1

Проведена только после явного разрешения пользователя и без добавления IP,
hostname, serial number или fingerprint в репозиторий.

| Семейство | CLI-характер | Проверенный путь | Статус |
| --- | --- | --- | --- |
| Cisco IOS, legacy SSH | Cisco-like | TOFU, strict reconnect, `show version` | пройден |
| SNR old | Cisco-like | login, prompt detection, `show version` | пройден |
| SNR eNOS | Cisco-like | login, prompt detection, `show version` | пройден |
| D-Link DES | D-Link CLI | login, prompt detection, `show switch` | пройден |
| Huawei VRP | VRP | login, prompt detection, `display version` | пройден |
| Juniper Junos | Junos | login, prompt detection, `show version` | пройден |

Проверялся путь `MCPServer.call_tool`: `open_session` → `run_command` →
`read_output` → `close_session`. В Этапе 2 локальные scripted tests дополнительно
проверяют contracts `cli_help`, `send_control` и `respond`. Полноценный stdio
client round-trip и OpenCode registration остаются задачей Этапа 6. Pager,
`cli_help` и device confirmation на реальном CLI еще не тестировались.

## Матрица приемки

Для каждого устройства фиксируются:

| Проверка | Ожидаемый результат |
| --- | --- |
| Login | prompt определен, секрет не залогирован |
| Prompt detection | prompt распознан, вывод установился |
| `cli_help` | подсказка прочитана, строка отменена, prompt восстановлен |
| Single command | вывод завершен по prompt |
| Multiple commands | одна TCP/terminal session |
| Long output | pager обработан, лимит соблюден |
| Invalid command | ошибка возвращена, session остается ready |
| Close | соединение и hops закрыты |

## Обязательные негативные тесты

- Попытка передать пароль в ad-hoc target.
- Произвольное имя записи `pass` из MCP argument.
- Command chaining через `;`, `&&`, newline или carriage return.
- Переход в config mode при read-only policy.
- Неизвестный confirmation prompt.
- Telnet без explicit allow.
- Legacy algorithms для host вне профиля.
- Невозможность записи audit-файла.
- Transcript/output path traversal.
- `apply_change` с structural hazard в команде.
- Повторный `apply_change` одного плана.
- `apply_change` без плана (после рестарта) — fail-closed.
- `abort_change` после исполнения.
- Change-команда с pager/confirmation prompt.
- SOCKS5 с требующейся авторизацией прокси.
- SOCKS5 с не-IP target.
- SOCKS5 при недоступном прокси/цели.
- `proxyjump` с `socks` и `jump_host` одновременно.
- Ad-hoc без `default_credentials`/`default_connection`.

## Команды проверки

Текущий набор проверки:

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest --cov=network_terminal_mcp
uv build
```
