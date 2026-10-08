# network-terminal-mcp

[![PyPI](https://img.shields.io/pypi/v/network-terminal-mcp)](https://pypi.org/project/network-terminal-mcp/)
[![CI](https://github.com/evgenyzh/network-terminal-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/evgenyzh/network-terminal-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Локальный MCP-сервер для постоянных интерактивных сессий с сетевым
оборудованием. Проект даёт OpenCode сырой терминал: подключиться к устройству,
использовать контекстную подсказку `?`, выполнить несколько команд, при
необходимости зайти вторым `ssh`/`telnet` внутрь той же сессии и получить полный
вывод без временных `sshpass`-команд и одноразовых скриптов.

Статус: v0.1.0 — этапы 1-9 реализованы. Сессия — это один постоянный терминальный stream
(SSH, Telnet, TCP console или локальный serial `/dev/tty*`); модель пишет в него
точно то, что нужно, включая вложенные переходы, и читает вывод без требования
определённой формы prompt. Для первого подключения поддерживаются direct, один
локальный SOCKS5 hop и один SSH ProxyJump hop; `nested`-маршрутов и
командно-ориентированных инструментов больше нет. Секреты, запрашиваемые уже
внутри сессии, вводятся через `terminal_write_secret` со ссылкой на `pass` и не
попадают в audit. Один процесс держит несколько независимых сессий.

Подключение описывает модель в самом вызове `open_session`: host, protocol,
credentials (ссылки `pass`/key file или plaintext за флагом), route
(direct/socks/proxyjump), host key policy, serial-параметры и legacy-алгоритмы.
Инвентаря и profile-конфигов больше нет — после установки достаточно открыть
сессию. Единственный необязательный локальный файл — `policy.yml` (posture и
лимиты). Проверено на живом оборудовании: direct SSH на Cisco IOS, SNR
old/eNOS, D-Link, Huawei VRP и Junos; ProxyJump через реальные bastion.
Подробности в [результатах проверок](docs/validation.md).

Текущие ограничения: raw input выполняется без per-команды подтверждения —
после одобренного `open_session` модель работает в устройстве свободно; оператор
может добавить permission `ask` для `terminal_write` в OpenCode. Автоматического
распознавания sensitive-команд (`conf t`, `system-view`, `commit`) пока нет.
Telnet, console и serial требуют явных per-call флагов и могут быть hard-deny
политикой. `transcripts_enabled` остаётся зарезервированной настройкой.

## Основные цели

- Прямой SSH, SOCKS5, ProxyJump, Telnet, TCP console и локальный serial.
- Современное и устаревшее оборудование: legacy SSH алгоритмы включаются явно
  для конкретного host в вызове.
- Постоянная сессия: авторизация выполняется один раз, затем модель пишет
  команды, `ssh`/`telnet` и одиночные клавиши в тот же stream.
- Несколько параллельных сессий в одном процессе: переключение между
  устройствами без переподключения.
- Zero-config: модель описывает соединение сама, локально нужен только
  необязательный `policy.yml`.
- Точные команды выбирает модель. MCP не переводит абстрактные операции в
  vendor CLI и не хранит полный каталог команд.
- Собственный терминальный слой на Paramiko, telnetlib3 и pyserial; тип
  устройства модель определяет сама по баннеру и выводу.
- Пароли загружаются из `pass` или явного key file; секреты вводятся в живой
  prompt через `terminal_write_secret` и не попадают в MCP arguments, results и
  audit. Plaintext-пароль — только за явным insecure-флагом.
- Все подключения, ввод и события терминала журналируются без секретов.


## Первая область поддержки

- Cisco IOS/IOS-XE, включая старые 29xx/35xx.
- Huawei VRP и Huawei OLT.
- Juniper Junos.
- SNR 29xx и 52xx на базе механики Cisco IOS, но как разные CLI-диалекты.
- D-Link DGS/DES.
- Eltex MES/ESR.
- MikroTik RouterOS через обычный SSH.
- BDCOM, EcoSGE и PON-платформы через generic transport.

## Не входит в первую версию

- Отдельный RouterOS API MCP.
- Полноценная система управления конфигурациями или Source of Truth.
- Автоматическая запись в production.
- Обход TACACS/RADIUS command authorization.
- Автоматическое включение слабых SSH-алгоритмов для всех устройств.

## Документы

- [Инструкция для модели](src/network_terminal_mcp/usage.md) — она же MCP-ресурс
  `network-terminal://usage`; краткий контракт едет в MCP `instructions`
- [Архитектура](docs/architecture.md)
- [План разработки](docs/development-plan.md)
- [Модель безопасности](docs/security.md)
- [Конфигурация](docs/configuration.md)
- [Эксплуатация](docs/operations.md)
- [Результаты проверок](docs/validation.md)
- [История этапов 0-8](docs/history.md)
- [Разработка адаптеров](docs/adapters.md)
- [Стратегия тестирования](docs/testing.md)
- [Открытые вопросы](docs/open-questions.md)

## Установка

Для постоянной работы MCP-клиента (OpenCode) ставьте пакет как инструмент:

```bash
uv tool install network-terminal-mcp
```

Команда `network-terminal-mcp` появится в `~/.local/bin`. Обновление:
`uv tool upgrade network-terminal-mcp`. Разовый запуск без установки:
`uvx network-terminal-mcp@latest`; также доступен `pip install network-terminal-mcp`.

## Запуск

Локальный MCP запускается OpenCode через `stdio`, без прослушивания TCP-порта.
Указывайте полный путь к установленному бинарю — так клиент не зависит от
`PATH` и не тратит время на сетевой резолвинг `uvx` при каждом старте:

```json
{
  "mcp": {
    "network-terminal": {
      "type": "local",
      "command": ["/home/USER/.local/bin/network-terminal-mcp"],
      "enabled": true
    }
  },
  "permission": {
    "network-terminal_open_session": "ask"
  }
}
```

Замените `USER` на свой логин; если `~/.local/bin` уже в `PATH`, достаточно
`["network-terminal-mcp"]`.

Конфигурационные файлы не обязательны. Для строгих ограничений (например,
hard-deny Telnet, serial, legacy-алгоритмов, plaintext) можно положить
`policy.yml` в `~/.config/network-terminal-mcp/`. Ввод в живой сессии по
умолчанию не подтверждается: после одобренного `open_session` модель работает в
терминале свободно.

Проверка локальной политики до запуска:

```bash
network-terminal-mcp check
# в чекауте проекта:
uv sync && uv run python -m network_terminal_mcp check
```

Подробный порядок регистрации SSH host key и запуска через OpenCode описан в
[руководстве эксплуатации](docs/operations.md).
