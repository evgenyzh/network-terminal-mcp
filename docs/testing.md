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
- MCP tool registration, Pydantic input validation и worker-thread dispatch.

### Transcript replay

- Prompt discovery.
- `session_preparation`.
- `cli_help` без Enter и последующая очистка строки.
- Обычная команда, длинный вывод и pager.
- Потеря соединения на каждом этапе nested route.
- Custom BDCOM/EcoSGE/PON adapters.

### Integration

- Локальный fake SSH/Telnet server с заранее заданным CLI.
- Mock `pass` executable с тестовыми секретами.
- MCP client round trip для каждого tool.
- Проверка, что stdout содержит только MCP protocol, а logs уходят в stderr/file.

### Hardware lab

Только после явного разрешения пользователя для точных targets:

- одна платформа за прогон;
- только диагностические команды;
- сначала console/management reachability;
- никаких production config transitions без отдельного разрешения;
- после теста проверить отсутствие зависших sessions.

## Выполненная проверка Этапа 1

Проведена только после явного разрешения пользователя и без добавления IP,
hostname, serial number или fingerprint в репозиторий.

| Семейство | Driver | Проверенный путь | Статус |
| --- | --- | --- | --- |
| Cisco IOS, legacy SSH | `cisco_ios` | TOFU, strict reconnect, `show version` | пройден |
| SNR old | `cisco_ios` + `snr_29xx` dialect | login, preparation, `show version` | пройден |
| SNR eNOS | `cisco_ios` + `snr_52xx` dialect | login, preparation, `show version` | пройден |
| D-Link DES | `dlink_ds` | login, preparation, `show switch` | пройден |
| Huawei VRP | `huawei_vrp` | login, preparation, `display version` | пройден |
| Juniper Junos | `juniper_junos` | login, preparation, `show version` | пройден |

Проверялся путь `MCPServer.call_tool`: `open_session` → `run_command` →
`read_output` → `close_session`. В Этапе 2 локальные scripted tests дополнительно
проверяют contracts `cli_help`, `send_control` и `respond`. Полноценный stdio
client round-trip и OpenCode registration остаются задачей Этапа 6. Pager,
`cli_help` и device confirmation на реальном CLI еще не тестировались.

## Матрица приемки

Для каждой платформы фиксируются:

| Проверка | Ожидаемый результат |
| --- | --- |
| Login | prompt определен, секрет не залогирован |
| Session preparation | paging отключен или включен fallback |
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
- `apply_change` без `allow_writes` или при `write_change: deny`.
- Повторный `apply_change` одного плана.
- `abort_change` после исполнения.
- `close_session` при активной запланированной перезагрузке.
- Change-команда с pager/confirmation prompt.

## Команды проверки

Текущий набор проверки:

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest --cov=network_terminal_mcp
uv build
```
