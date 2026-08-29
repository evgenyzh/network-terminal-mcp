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

## Local validation Этапа 2

Scripted tests покрывают `MCPServer.call_tool` contracts, `cli_help` без Enter
и с Ctrl-C cleanup, pager continuation/abort/page limit, confirmation
allowlist, отказ от password prompt и state-bound control actions. Они не
заменяют transcript или hardware test реального CLI.

## Непроверенное

- Полный MCP stdio round-trip с клиентом OpenCode.
- Pager fallback, `cli_help` и interactive confirmations на реальном устройстве.
- Anonymized transcripts для platform-specific pager/prompt patterns.
- `raw_input`.
- ProxyJump, nested SSH/Telnet, console ports и Telnet.
- BDCOM, EcoSGE, Eltex и PON adapters.
- Per-device legacy algorithm override и DSA-only SSH.
