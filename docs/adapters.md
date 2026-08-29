# Адаптеры платформ

## Когда нужен адаптер

Новый адаптер нужен только если отличается механика терминала:

- login flow;
- prompt или режимы;
- отключение paging;
- line endings;
- обработка enable/config mode;
- ANSI/control sequences;
- завершение команды или confirmation prompts.

Различие vendor-команд само по себе не является причиной писать драйвер.

Например, SNR 29xx и 52xx могут использовать Cisco IOS driver, даже если
команды MAC/VLAN отличаются. Разницу исследует модель через `cli_help`.

Этап 1 подтвердил этот подход на SNR old и SNR eNOS: обе линейки прошли login,
session preparation и `show version` через `cisco_ios`. Имена `snr_29xx` и
`snr_52xx` остаются dialect metadata; отдельный Netmiko driver не требуется.

## Реализация

Локальный класс наследуется от ближайшего Netmiko-драйвера и переопределяет
минимум методов:

```python
class ExamplePlatformSSH(CiscoIosBase):
    def session_preparation(self) -> None:
        self._test_channel_read(pattern=r"[>#]")
        self.set_base_prompt()
        self.disable_paging(command="terminal length 0")
```

Это только иллюстрация. Реальная реализация должна исходить из transcript и
проверяться на устройстве.

Не следует копировать целый vendor driver ради одной команды paging.

## Registry

Собственный registry сопоставляет platform name с:

- штатным Netmiko `device_type`; или
- локальным SSH/Telnet-классом;
- dialect metadata;
- pager/control patterns;
- capability flags.

Не требуется изменять `netmiko.ssh_dispatcher.CLASS_MAPPER` или monkey-patch
установленного пакета.

## BDCOM

Начальные семейства:

- `bdcom_huawei_like`;
- `bdcom_cisco_legacy`.

До реализации нужны обезличенные transcripts:

- login до первого prompt;
- пустой Enter;
- команда версии;
- команда отключения paging;
- вывод с pager;
- вход/выход из enable и config без применения конфигурации;
- `?` и очистка строки.

## EcoSGE

EcoSGE имеет SSH CLI и TACACS+. Сначала пробуется ближайший generic/Cisco-like
механизм. Отдельный адаптер добавляется только после проверки prompt, paging и
режимов на установленной версии ПО.

## PON

Huawei OLT начинает со штатного `huawei_olt`. BDCOM, Eltex и SNR PON нельзя
объединять в один общий `pon` driver: адаптер выбирается по фактической CLI-
механике и версии.

## Transcript fixtures

Transcripts для тестов должны быть обезличены:

- заменить hostnames, IP, MAC, serial numbers и usernames;
- удалить banners с внутренними названиями;
- никогда не записывать password prompts вместе с отправленным секретом;
- сохранить управляющие символы в escaped-виде;
- отметить ожидаемый prompt и точки записи клиента.
