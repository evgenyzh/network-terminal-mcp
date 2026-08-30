# Результаты проверок Этапов 1-2

## Scope

Проверялись только direct SSH, read-only commands и один представитель каждого
семейства. Адреса, hostnames, serial numbers, usernames, fingerprints и password
store contents намеренно не записываются в репозиторий.

## Подтвержденные платформы

| Семейство | Механика | Driver | Результат |
| --- | --- | --- | --- |
| Cisco IOS старого поколения | legacy SSH | `cisco_ios` | успешные login, session preparation, `show version` |
| SNR S2985 | Cisco-like CLI | `cisco_ios`, dialect `snr_29xx` | успешные login, preparation, `show version` |
| SNR S5210 eNOS | Cisco-like CLI | `cisco_ios`, dialect `snr_52xx` | успешные login, preparation, `show version` |
| D-Link DES | D-Link CLI | `dlink_ds` | успешные login, preparation, `show switch` |
| Huawei S6730 | VRP | `huawei_vrp` | успешные login, preparation, `display version` |
| Juniper MX204 | Junos | `juniper_junos` | успешные login, preparation, `show version` |

## Legacy SSH

Проверенный Cisco предлагает только `diffie-hellman-group1-sha1`, `ssh-rsa`,
`3des-cbc` и `hmac-sha1`. Локальный OpenSSH 10.2 не смог с ним договориться;
Paramiko 4.0 в составе Netmiko подключился успешно. Поэтому отдельный oldssh не
добавлялся.

## MCP acceptance

На каждом представителе проверен поток:

```text
open_session -> run_command -> read_output -> close_session
```

Первое подключение через явный `accept_new` сохраняет ключ в локальный
`known_hosts`. Последующие соединения выполняются с `ssh_strict=True` и
передают этот файл Netmiko как `alt_key_file`.

## Hardware validation Этапа 2

Read-only проверки через `MCPServer.call_tool` на реальном оборудовании.

| Устройство | `cli_help` | `run_command` | Примечание |
| --- | --- | --- | --- |
| Cisco IOS | работает | работает | `show ?` возвращает подсказку, Ctrl-C восстанавливает prompt |
| SNR eNOS | работает | работает | корректная форма `show interface ?` / `show interface brief` |
| D-Link DES | работает | работает | через `cli_help_requires_enter: true` — `show ?` + Enter показывают подсказку |
| SNR old | работает | работает | help-pager поддерживает `space`/`q`; host key меняется при каждой загрузке, профиль `direct-snr` использует `accept_changed` |
| Huawei VRP | работает | работает | `session_preparation` отключает pager; help возвращает `prompt + display `, Ctrl-C + Ctrl-U очищают строку |
| Juniper Junos | работает | работает | `session_preparation` отключает pager; help возвращает `prompt + show ` с backspace bytes, Ctrl-C + Ctrl-U очищают строку |

На SNR old проверен flow `cli_help -> space -> q -> run_command` в одной
сессии. На Junos/Huawei проверен `cli_help -> run_command` в одной сессии после
silent Ctrl-C/Ctrl-U cleanup. Если help-pager не возвращает распознанный prompt,
сессия всё ещё безопасно переводится в `failed`.

Netmiko `session_preparation` отключает pager на проверенных Junos и Huawei,
но SNR old сохраняет его для help и больших команд (`show interface`).

Проверка `respond` на реальном оборудовании не проводилась: confirmation
prompts обычно предшествуют write/destructive действиям.

## Local validation Этапа 2

Scripted tests покрывают `MCPServer.call_tool` contracts, `cli_help` без Enter,
prompt-tail cleanup Ctrl-C/Ctrl-U, pager continuation/abort/page limit,
confirmation allowlist, отказ от password prompt и state-bound control actions.
Отдельные тесты фиксируют короткий `cli_help_timeout` и гарантированный Ctrl-C
cleanup даже при ошибке чтения.

## Непроверенное

- Полный MCP stdio round-trip с клиентом OpenCode.
- Interactive confirmations на Junos/Huawei.
- Continuation большого обычного command pager на реальном устройстве.
- Anonymized transcripts для platform-specific pager/prompt patterns.
- `raw_input`.
- ProxyJump, nested SSH/Telnet, console ports и Telnet.
- BDCOM, EcoSGE, Eltex и PON adapters.
- Per-device legacy algorithm override и DSA-only SSH.
