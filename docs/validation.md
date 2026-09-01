# Результаты проверок Этапов 1, 2, 3, 4, 6 и 7

## Scope

Проверялись только direct SSH, read-only commands и один представитель каждого
семейства. Адреса, hostnames, serial numbers, usernames, fingerprints и password
store contents намеренно не записываются в репозиторий.

## Подтвержденные устройства

Проверен представитель каждого семейства CLI. Драйверы не используются: тип
устройства и механика CLI определяются моделью по выводу уже после открытия
сессии.

| Семейство | Механика CLI | Результат |
| --- | --- | --- |
| Cisco IOS старого поколения | Cisco IOS (legacy SSH) | успешные login, session preparation, `show version` |
| SNR S2985 | Cisco-like CLI | успешные login, preparation, `show version` |
| SNR S5210 eNOS | Cisco-like CLI | успешные login, preparation, `show version` |
| D-Link DES | D-Link CLI | успешные login, preparation, `show switch` |
| Huawei S6730 | VRP | успешные login, preparation, `display version` |
| Juniper MX204 | Junos | успешные login, preparation, `show version` |

## Legacy SSH

Проверенный Cisco предлагает только `diffie-hellman-group1-sha1`, `ssh-rsa`,
`3des-cbc` и `hmac-sha1`. Локальный OpenSSH 10.2 не смог с ним договориться;
Paramiko 4.0 подключился успешно. Поэтому отдельный oldssh не добавлялся.

## MCP acceptance

На каждом представителе проверен поток:

```text
open_session -> run_command -> read_output -> close_session
```

Первое подключение через явный `accept_new` сохраняет ключ в локальный
`known_hosts`. Последующие соединения выполняются с `host_key_policy: strict` и
передают этот файл `SshTerminal` как `known_hosts_file`.

## Hardware validation Этапа 2

Read-only проверки через `MCPServer.call_tool` на реальном оборудовании.

| Устройство | `cli_help` | `run_command` | Примечание |
| --- | --- | --- | --- |
| Cisco IOS | работает | работает | `show ?` возвращает подсказку, Ctrl-C восстанавливает prompt |
| SNR eNOS | работает | работает | корректная форма `show interface ?` / `show interface brief` |
| D-Link DES | работает | работает | через `cli_help` без Enter — подсказка не возвращается; модель повторяет запрос с Enter (`show ?` + Enter), завершающий `?` не даёт команде выполниться |
| SNR old | работает | работает | help-pager поддерживает `space`/`q`; host key меняется при каждой загрузке, профиль `direct-snr` использует `accept_changed` |
| Huawei VRP | работает | работает | help возвращает `prompt + display `, Ctrl-C + Ctrl-U очищают строку |
| Juniper Junos | работает | работает | help возвращает `prompt + show ` с backspace bytes, Ctrl-C + Ctrl-U очищают строку |

На SNR old проверены flows `cli_help -> space -> q -> run_command`,
`show interface -> space -> q -> show version` и естественное завершение
`show interface` после 29 страниц с последующим `show version` в одной сессии.
На Junos/Huawei проверен `cli_help -> run_command` в одной сессии после silent
Ctrl-C/Ctrl-U cleanup. Если help-pager не возвращает распознанный prompt,
сессия всё ещё безопасно переводится в `failed`.

Pager на проверенных Junos и Huawei не отключается автоматически: большие
выводы возвращаются с `pager_active: true`, и модель листает их
`send_control("space")` или завершает `q`. SNR old сохраняет pager для help
и больших команд (`show interface`).

Проверка `respond` на реальном оборудовании не проводилась: confirmation
prompts обычно предшествуют write/destructive действиям.

## Local validation Этапа 2

Scripted tests покрывают `MCPServer.call_tool` contracts, `cli_help` без Enter,
prompt-tail cleanup Ctrl-C/Ctrl-U, pager continuation/abort/page limit,
confirmation allowlist, отказ от password prompt и state-bound control actions.
Отдельные тесты фиксируют короткий `cli_help_timeout` и гарантированный Ctrl-C
cleanup даже при ошибке чтения.

## OpenCode integration

Текущий global OpenCode profile регистрирует `network-terminal` как local stdio
MCP с рабочим каталогом проекта и timeout 65 секунд. `opencode mcp list`
подтвердил подключение сервера. Через этот MCP выполнен read-only round-trip
`open_session -> run_command -> close_session` на зарегистрированной цели;
device identifiers и terminal output в репозиторий не записываются.

Global skill `network-terminal` требует inventory targets, read-only commands,
явное закрытие session и запрещает обходить MCP через shell SSH. Безопасность
команд остаётся в policy самого сервера: `ask` не исполняется.

## SOCKS validation

SOCKS-маршрут покрыт unit-тестами:
- минимальный SOCKS5 CONNECT (no-auth, IPv4/IPv6) против фейкового сервера:
  greeting, request, успешный reply;
- отказ при требующейся авторизации прокси (`0x05 0x02`);
- отказ для не-IP target (прокси не резолвит имена);
- обёртка ошибок подключения в `TransportError`;
- конфиг-валидация `proxyjump` с полем `socks`: дефолты, взаимоисключение
  `socks` и `jump_host`, обязательность `socks` или `jump_host`;
- менеджер: сокет из `socks5_connect` передаётся `SshTerminal` как `sock`,
  route в audit `socks`, SSH-бастион не создаётся, host key цели проверяется
  через SOCKS-сокет. Probe выполняется до открытия connection-сокета: некоторые
  старые устройства (SNR old) не выдерживают второе одновременное SSH-соединение
  через тот же туннель, и обратный порядок даёт EOF на handshake.

Проверка на реальном оборудовании через живой локальный SOCKS-прокси
описывается в этом разделе при её выполнении.

## Ad-hoc validation

- `_resolve_ad_hoc` подставляет `default_credentials` / `default_connection`,
  когда модель не передала их явно; без дефолтов ad-hoc требует их явно.

Живой ad-hoc доступ (open_session по IP через дефолты) проверяется на реальном
оборудовании при его выполнении.

## One-hop ProxyJump validation

Проверен реальный маршрут local -> SSH jump host (explicit `ssh_key` profile с
encrypted key и passphrase из `pass`) -> SNR old target. Bastion auth по
explicit local key file, final target по password profile. Flow
`open_session -> show version -> close_session` выполнен в одной session;
jump host key после TOFU enrollment проверяется в `strict`, target — в
`accept_changed` из-за rotating keys. В репозиторий записаны только обезличенные
выводы и hostkey не входят.

## Nested SSH validation

Проверен реальный маршрут local -> intermediate host (password profile) -> SNR
old target. Intermediate host — Linux shell; из его shell выполняется `ssh` до
final target с credentials целевого устройства. Flow
`open_session -> show version -> close_session` выполнен в
одной session; intermediate host key проверяется в `strict`. Inner SSH
использует SSH-клиент промежуточного хоста, поэтому host key final target
проверяется им, а не локальным store.

## Этап 4 validation

Legacy SSH overrides, Telnet и console проверены unit-тестами:
- Per-profile allowlist превращается в Paramiko `disabled_algorithms` как
  дополнение полного набора алгоритмов; категории вне allowlist не трогаются,
  host key checking остаётся включенным.
- Прямой Telnet требует `allow_telnet: true` на устройстве и
  `defaults.telnet: allow`; при нарушении любого из условий сессия не
  открывается. Telnet использует `TelnetTerminal`, не проверяет host key и
  возвращает cleartext warning.
- Nested Telnet (`next_protocol: telnet`) выполняет `telnet` из shell
  промежуточного хоста и тоже требует двойного gating.
- `console` profile требует заданный `port`, использует `TelnetTerminal` и не
  поддерживает `connect_command`.

Hardware-проверка Telnet/console на реальном устройстве не выполнялась.

## Этап 7 validation

Изменения конфигурации проверены unit-тестами:
- `plan_change` отклоняет пустой title/список и structural hazards; ничего не
  исполняется.
- `apply_change` двухшаговый: первый вызов возвращает `confirmation_required`
  и не исполняет, второй исполняет только сохранённые канонические команды;
  повторный apply запрещён.
- `abort_change` отменяет план до исполнения.
- `finalize_change` помечает применённый план завершённым; повторная
  финализация запрещена.
- При pager/confirmation в change-команде план переводится в `failed`.
- `auto_approve` пропускает подтверждающий вызов, когда разрешён явно.

Откат (Junos `commit confirmed`, Huawei `schedule reboot delay`/`undo`, Cisco
`reload in 10`/`reload cancel`) — рекомендация модели, а не серверная механика;
hardware-проверка не выполнялась и требует отдельного явного разрешения на
точные команды.

## Непроверенное

- Interactive confirmations на Junos/Huawei.
- Anonymized transcripts для device-specific pager/prompt patterns.
- `raw_input`.
- Telnet (прямой и nested) и console на реальном оборудовании — только
  unit-тесты; hardware-проверка не выполнялась.
- Изменения конфигурации и rollback на реальном оборудовании — только
  unit-тесты; hardware-проверка не выполнялась.
- BDCOM, EcoSGE, Eltex и PON на реальном оборудовании через generic tools.
- DSA-only SSH.
- Per-device legacy algorithm override на реальной лабораторной цели (логика
  покрыта unit-тестами через `disabled_algorithms`).
