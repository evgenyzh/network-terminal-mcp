# network-terminal-mcp

Локальный MCP-сервер для постоянных интерактивных сессий с сетевым
оборудованием. Проект должен дать OpenCode удобный терминальный интерфейс:
подключиться к устройству, использовать контекстную подсказку `?`, выполнить
несколько команд в одной сессии и получить полный вывод без временных
`sshpass`-команд и одноразовых скриптов.

Статус: Этапы 1, 2 и 6 реализованы; Этап 3 поддерживает один SSH-only ProxyJump
hop. Есть конфигурация, политика,
аудит, credential backend, known_hosts, постоянные Netmiko-сессии, безопасный
`cli_help`, pager/control state machine и stdio MCP tools. Direct SSH проверен
на Cisco IOS, SNR old/eNOS, D-Link, Huawei VRP и Junos. `cli_help` подтвержден
на всех этих платформах: SNR old поддерживает `space`/`q` pager flow, а
Junos/Huawei очищают оставшуюся help-строку Ctrl-C и Ctrl-U. Current local
OpenCode profile подключён к server через stdio MCP и прошёл read-only
round-trip. Подробности в [результатах проверок](docs/validation.md).

Текущие ограничения: terminal servers, nested SSH, Telnet, `raw_input` и config
changes остаются следующими этапами. Команда с
policy-решением `ask` возвращает `confirmation_required`, но не исполняется.
`respond` отвечает только на уже распознанный prompt устройства и не является
механизмом policy confirmation.

## Основные цели

- Прямой SSH, ProxyJump, вложенный SSH, консольный сервер и Telnet.
- Современное и устаревшее оборудование с локальными профилями legacy SSH.
- Постоянная сессия: авторизация и переход через терминальный сервер выполняются
  один раз, затем модель продолжает работать с тем же prompt.
- Точные команды выбирает модель. MCP не переводит абстрактные операции в
  vendor CLI и не хранит полный каталог команд.
- Netmiko отвечает за SSH/Telnet-канал, prompt, paging и подготовку терминала.
- Дополнительные адаптеры описывают только механику нестандартного CLI.
- Пароли загружаются из `pass`/GPG и не попадают в аргументы MCP или ответы.
- Все команды и результаты подключения журналируются без секретов.
- Диагностика доступна по умолчанию; изменение конфигурации отделено и требует
  явного подтверждения.

## Первая область поддержки

- Cisco IOS/IOS-XE, включая старые 29xx/35xx.
- Huawei VRP и Huawei OLT.
- Juniper Junos.
- SNR 29xx и 52xx на базе механики Cisco IOS, но как разные CLI-диалекты.
- D-Link DGS/DES.
- Eltex MES/ESR.
- MikroTik RouterOS через обычный SSH.
- BDCOM, EcoSGE и PON-платформы через проверяемые пользовательские адаптеры.

## Не входит в первую версию

- Отдельный RouterOS API MCP.
- Полноценная система управления конфигурациями или Source of Truth.
- Автоматическая запись в production.
- Обход TACACS/RADIUS command authorization.
- Автоматическое включение слабых SSH-алгоритмов для всех устройств.

## Документы

- [Архитектура](docs/architecture.md)
- [План разработки](docs/development-plan.md)
- [Модель безопасности](docs/security.md)
- [Конфигурация](docs/configuration.md)
- [Эксплуатация](docs/operations.md)
- [Результаты проверок](docs/validation.md)
- [Разработка адаптеров](docs/adapters.md)
- [Стратегия тестирования](docs/testing.md)
- [Открытые вопросы](docs/open-questions.md)

## Запуск

Локальный MCP запускается OpenCode через `stdio`, без прослушивания TCP-порта:

```json
{
  "mcp": {
    "network-terminal": {
      "type": "local",
      "command": ["uv", "run", "network-terminal-mcp"],
      "enabled": true
    }
  }
}
```

Перед запуском нужны локальные конфигурационные файлы в
`~/.config/network-terminal-mcp/`; они не входят в git.

Проверка локальной конфигурации до запуска:

```bash
uv sync
uv run python -m network_terminal_mcp check
```

Подробный порядок первичной регистрации SSH host key и запуска через OpenCode
описан в [руководстве эксплуатации](docs/operations.md).
