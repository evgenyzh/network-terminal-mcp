# Результаты проверок

## Scope

Проверялись только representative-устройства и диагностика. Адреса, hostnames,
serial numbers, usernames, fingerprints и password store contents намеренно не
записываются в репозиторий. Исторические проверки этапов 0-8 (прежний
командно-ориентированный gateway и `nested`) вынесены в
[docs/history.md](history.md).

## Подтвержденные устройства

Проверен представитель каждого семейства CLI. Драйверы не используются: тип
устройства и механика CLI определяются моделью по выводу уже после открытия
сессии. Login/prompt-проверки выполнены на Этапах 1-2 прежними инструментами;
семейства, баннеры и механика prompt актуальны для raw terminal, а полный
raw-цикл отдельно подтверждён на Этапе 9 (см. ниже).

| Семейство | Механика CLI | Результат |
| --- | --- | --- |
| Cisco IOS старого поколения | Cisco IOS (legacy SSH) | успешные login, prompt detection, `show version` |
| SNR S2985 | Cisco-like CLI | успешные login, prompt detection, `show version` |
| SNR S5210 eNOS | Cisco-like CLI | успешные login, prompt detection, `show version` |
| D-Link DES | D-Link CLI | успешные login, prompt detection, `show switch` |
| Huawei S6730 | VRP | успешные login, prompt detection, `display version` |
| Juniper MX204 | Junos | успешные login, prompt detection, `show version` |

## Legacy SSH

Проверенный Cisco предлагает только `diffie-hellman-group1-sha1`, `ssh-rsa`,
`3des-cbc` и `hmac-sha1`. Локальный OpenSSH 10.2 не смог с ним договориться;
Paramiko 4.0 подключился успешно. Поэтому отдельный oldssh не добавлялся.

## Этап 9 validation

Raw terminal API проверен unit-тестами:

- `terminal_write` отправляет точный ввод, добавляет `\n` (или `\r` для serial)
  только при `enter=true`, отклоняет ввод больше `max_write_bytes`, маскирует
  известные секреты в audit и при transport error переводит сессию в `failed`.
- `terminal_read` возвращает вывод без требования prompt, уважает `timeout` и
  `max_read_timeout`, обновляет offsets и truncation, сбои fail-closed.
- `terminal_write_secret` берёт значение из `pass`, не показывает его в
  результате и audit (только entry и размер), добавляет в redaction и
  отклоняет traversal/пустые записи.
- Serial-транспорт: path validation, char-device check, обработка ошибки
  открытия.
- ProxyJump: forwarded channel, независимые host key checks, cleanup jump
  client на всех путях ошибок и expiry; auth-ошибка jump и final target
  возвращается с раздельными понятными сообщениями.

Live проверка через OpenCode MCP: `proxyjump` до промежуточного Linux хоста,
`terminal_write("ssh <user>@<device>")` в той же сессии,
`terminal_write_secret` на password prompt и `terminal_read` уже на CLI
целевого Huawei S6730 (`display version`, pager пролистан через
`terminal_write(" ", enter=False)`), затем `close_session`. Audit содержит
entry и размер, но не значение секрета.

Сервер также отдаёт модель-ориентированную инструкцию: `instructions` в MCP
initialize и ресурс `network-terminal://usage`; unit-тесты проверяют наличие
контракта и читаемость ресурса.

## Legacy SSH, Telnet, console и serial (unit)

- Per-call allowlist превращается в Paramiko `disabled_algorithms` как
  дополнение полного набора алгоритмов; категории вне allowlist не трогаются,
  host key checking остаётся включенным.
- Прямой Telnet требует `allow_telnet=true` в вызове и не проверяет host key,
  возвращает cleartext warning; policy `allow_telnet: false` запрещает его
  полностью.
- `console` требует заданный `port`, использует `TelnetTerminal` и тот же
  флаг.
- `serial` требует `allow_serial=true` и абсолютного `/dev/` символьного
  устройства; относительные пути, несуществующие устройства и regular files
  отклоняются.

Hardware-проверка Telnet/console/serial на реальном устройстве не выполнялась.

## SOCKS validation

SOCKS-маршрут покрыт unit-тестами:

- минимальный SOCKS5 CONNECT (no-auth, IPv4/IPv6) против фейкового сервера:
  greeting, request, успешный reply;
- отказ при требующейся авторизации прокси (`0x05 0x02`);
- отказ для не-IP target (прокси не резолвит имена);
- обёртка ошибок подключения в `TransportError`;
- spec-валидация `route: {type: "socks"}` и совместимость только с SSH;
- менеджер: сокет из `socks5_connect` передаётся `SshTerminal` как `sock`,
  route в audit `socks`, SSH-бастион не создаётся, host key цели проверяется
  через SOCKS-сокет. Probe выполняется до открытия connection-сокета: некоторые
  старые устройства (SNR old) не выдерживают второе одновременное SSH-соединение
  через тот же туннель, и обратный порядок даёт EOF на handshake.

Проверка на реальном оборудовании через живой локальный SOCKS-прокси
описывается в этом разделе при её выполнении.

## Zero-config validation

- `OpenSpec` отклоняет неизвестные поля, plaintext-пароль без
  `allow_plaintext_password`, Telnet/console без `allow_telnet`, serial без
  `allow_serial` и вне `/dev/`, console без `port`, legacy вне SSH,
  SOCKS/proxyjump для telnet, serial с route и удалённый `nested` route.
- `build_plan` нормализует порты (22/23, явный console port, serial без порта)
  и формирует warnings (plaintext, cleartext, serial, SOCKS, legacy).
- Policy hard-deny покрыт тестами для `allow_telnet`,
  `allow_serial`, `allow_plaintext_password` и `allow_legacy_algorithms`.
- `pass`-entry с traversal (`..`, абсолютный путь) отклоняется на уровне схемы
  и на уровне `terminal_write_secret`.
- `python -m network_terminal_mcp check` проходит без файлов.
- Старые `inventory.yml`/`connections.yml`/`credentials.yml` игнорируются и не
  ломают запуск.

## OpenCode integration

Global OpenCode profile регистрирует `network-terminal` как local stdio MCP с
рабочим каталогом проекта и timeout 65 секунд. `opencode mcp list` подтвердил
подключение сервера. Каждый `open_session` проходит permission-попап клиента;
сервер отдаёт `instructions` и ресурс `network-terminal://usage`, которые
OpenCode вкладывает в контекст модели. Global skill `network-terminal` требует
model-described `open_session`, работу через raw terminal tools, секреты только
через `terminal_write_secret`, явное закрытие session и запрещает обходить MCP
через shell SSH. Device identifiers и terminal output в репозиторий не
записываются.

## Непроверенное

- Hardware-проверка serial, Telnet и TCP console на реальных целях — только
  unit-тесты.
- Распознавание sensitive-команд (`conf t`, `system-view`, `commit`, `reload`)
  с клиентским подтверждением — не реализовано.
- Transcripts сессий — не реализованы.
- BDCOM, EcoSGE, Eltex и PON на реальном оборудовании через generic tools.
- DSA-only SSH.
- Per-device legacy algorithm override на реальной лабораторной цели (логика
  покрыта unit-тестами через `disabled_algorithms`).
