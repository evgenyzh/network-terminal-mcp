# Стратегия тестирования

## Уровни

### Unit

- Валидация inventory, connection и policy schemas.
- Разрешение target/profile без доступа к произвольной записи `pass`.
- Redaction паролей из ошибок и audit.
- Command policy: allow, ask, deny, переносы строк и metacharacters.
- Session state machine, locks, timeout и output limits.
- Pager и confirmation prompt detection.

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

## Матрица приемки

Для каждой платформы фиксируются:

| Проверка | Ожидаемый результат |
| --- | --- |
| Login | prompt определен, секрет не залогирован |
| Session preparation | paging отключен или включен fallback |
| `cli_help` | подсказка прочитана, строка очищена |
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

## Команды проверки

После появления реализации:

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest --cov=network_terminal_mcp
```
