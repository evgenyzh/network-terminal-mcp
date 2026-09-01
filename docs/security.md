# Модель безопасности

## Границы доверия

MCP запускается локально через `stdio`. Он имеет доступ к сетевым устройствам и
секретам, поэтому считается привилегированным компонентом. Ответ модели,
пользовательский текст и вывод устройства не считаются доверенными командами.

MCP-policy является дополнительной защитой, но не заменяет TACACS/RADIUS command
authorization. Предпочтительна отдельная read-only AAA-учетная запись.

## Секреты

- Инвентарь хранит только имя credential profile.
- Credential profile ссылается на запись `pass`.
- `pass` вызывается сервером без shell interpolation.
- Пароль существует в памяти только во время жизни открытой session, поскольку
  redaction использует его для фильтрации terminal output.
- Пароли не передаются в MCP arguments, tool results, transcripts и audit.
- Redaction применяется к исключениям до логирования.
- Сервер не предоставляет инструмент для чтения произвольной записи `pass`.

Рекомендуемый формат записи `pass`:

```text
<password on first line>
```

Имя записи и AAA username выбираются только из локального credential profile, а
не из аргумента модели. Username не является секретом; отдельный enable secret,
если понадобится, будет храниться в отдельной записи `pass`.

## Политика команд

Результат проверки: `allow`, `ask` или `deny`.

- `run_command` исполняет только `allow`.
- Диагностические `show`, `display` и эквиваленты разрешаются командной policy.
- Неизвестные exec-команды и config mode получают `ask`, но пока не исполняются.
- Перезагрузка, factory reset, удаление конфигурации и файлов запрещены.
- Тяжелые команды вроде полного tech-support могут быть запрещены или `ask`.
- Переносы строк, command chaining и shell metacharacters запрещены в
  `run_command`.

Политика проверяет полную строку до отправки Enter. `cli_help` имеет отдельный
default policy и не принимает `?`, control characters или structural hazards.
По умолчанию он не нажимает Enter. Если помощь уходит в pager, tool возвращает первый экран
(`pager_active`) и модель листает `space`/выходит `q`; после распознанного
`prompt + остаток строки` сервер очищает line buffer Ctrl-C и Ctrl-U. Такая же
последовательность используется для непостраничного help, который уже вернул
prompt с остатком строки. Если pager не вернулся к распознанному prompt, сессия
переводится в `failed`, а не оставляется в неизвестном состоянии. `cli_help`
всегда отправляет `<line>?` без Enter; для CLI с помощью только после Enter
(D-Link) модель повторяет запрос с Enter, а завершающий `?` не даёт строке
выполниться.
Generic `raw_input` остается отключенным.

## Интерактивные вопросы

`respond` не является generic input и не подтверждает policy `ask`. Он доступен
только для текущего распознанного device confirmation с конечным allowlist
`y`/`n` или `yes`/`no`. Prompt должен содержать явный текст действия и marker
`[Y/N]`, `(Y/N)`, `[yes/no]` или `(yes/no)`; unknown prompts не получают
ответа. Password, passphrase и secret prompts переводят session в `failed` без
отправки token.

`send_control` не принимает произвольные управляющие последовательности:
`space`, `q` и `ctrl-c` разрешены только в совместимых состояниях pager/prompt.
После page limit сервер отправляет `q`, а не продолжает неограниченный вывод.

## Аудит

Каждая операция получает correlation ID. JSONL-запись содержит:

- время и session ID;
- логическое имя устройства и адрес;
- connection profile;
- пользователя AAA, но не пароль;
- event/tool, команду, help line, control action или allowlisted response;
- решение policy, outcome и размер вывода для выполненной команды;
- ошибки после redaction.

Если pre-action audit-файл недоступен, команда не выполняется. Права файлов:
каталоги 0700, файлы 0600. Full transcripts в Этапе 1 не реализованы;
`transcripts_enabled` пока является зарезервированной настройкой.

## Legacy SSH

Слабые алгоритмы включаются только в connection/device profile. Запрещено
изменять глобальный `~/.ssh/config` шаблоном `Host *`.

Каждое legacy-соединение возвращает предупреждение и отмечается в audit. Явные
списки `host_key_algorithms`/`kex_algorithms`/`ciphers` в профиле ограничивают
только перечисленные категории (allowlist → Paramiko `disabled_algorithms`);
остальные категории сохраняют значения Paramiko по умолчанию. Host
key checking остается включенным: default `host_key_policy: strict` сверяет
ключ с локальным `known_hosts`. Для первичной регистрации допускается только
явный `accept_new` (TOFU), который сохраняет fingerprint в аудит; затем профиль
нужно вернуть в `strict`.

`accept_changed` — слабый доверительный режим для устройств, у которых host key
заведомо меняется при каждой загрузке (например, некоторые SNR). Он допустим
только как явный opt-in для конкретного profile и при каждой смене ключа пишет
в audit предупреждение со старым и новым fingerprint. Этот режим не защищает от
MITM и не должен применяться к устройствам со стабильными ключами.

На этой машине OpenSSH 10.2 не смог согласовать `ssh-rsa` с проверенным старым
Cisco, даже при локальных client options. Paramiko 4.0 поддержал его набор
`diffie-hellman-group1-sha1`, `ssh-rsa`, `3des-cbc` и `hmac-sha1`, поэтому
отдельный oldssh пока не нужен. DSA-only оборудование все еще потребует
изолированного решения или Telnet и не входит в текущую реализацию.

## ProxyJump

Один SSH jump host задаётся только локальным connection profile. Сервер сначала
проверяет и аутентифицирует bastion, затем открывает Paramiko `direct-tcpip`
channel и проверяет key final target через этот channel. Password и host key
policy у hop и final target независимы; оба password добавляются в redaction.

Literal `ProxyCommand`, shell command, automatic agent/key discovery и system
host keys не используются. Key-based profile допускает только явный локальный
`key_file`; он не передается в MCP arguments. Поддерживаются только один hop и
SSH forwarding; запрет forwarding на bastion, ошибка проверки key или target
login закрывают весь маршрут fail-closed.

## Nested SSH

Nested profile подключается к intermediate host через SSH/Telnet terminal,
затем из его shell выполняет `ssh` (или `telnet`) до final target. Host key
intermediate host проверяется локальным `known_hosts_file` с
политикой `host_key_policy` профиля. Inner SSH выполняется SSH-клиентом
intermediate host: host key final target проверяется именно им, а не локальным
store, поэтому nested маршрут по умолчанию доверяет SSH-конфигурации
промежуточного хоста. Username final target передается в команду `ssh`;
password final target читается из `pass` и добавляется в redaction. Shell
`ssh` без параметров, raw command от модели и произвольный hop не
поддерживаются.

## Telnet

Telnet разрешается только явно и только для конкретных targets/profiles: нужен
`allow_telnet: true` на устройстве **и** `defaults.telnet: allow` в policy
(по умолчанию `deny`). Это двойное gating предотвращает случайное включение.
Сервер сообщает, что учетные данные и трафик передаются открытым текстом, и
добавляет обязательный warning в каждую Telnet-сессию. Host key для Telnet не
проверяется (ключа нет). Автоматический fallback с SSH на Telnet запрещен:
смена протокола должна быть явной конфигурацией.

Nested Telnet (`next_protocol: telnet`) выполняет `telnet` из shell
промежуточного хоста и наследует то же двойное gating; промежуточный хост
проверяется по SSH host key, а final target — нет. `console` profile
(ТСP console port терминального сервера) требует того же gating и не
поддерживает произвольную `connect_command`.

## Запись конфигурации

Изменения конфигурации отделены от диагностики: `run_command` остаётся
read-only, а изменения идут только через `plan_change` → `apply_change` →
`finalize_change`. Модель не может применить изменение одним вызовом: план,
подтверждение и аудит обязательны.

Команды пишет модель; сервер хранит их канонический список и исполняет при
`apply_change` ровно то, что было сохранено. `apply_change` двухшаговый: первый
вызов возвращает план с `confirmation_required` и ничего не исполняет, второй —
применяет. `auto_approve` разрешён только с явного согласия пользователя на
конкретную пару (сессия, устройство) и обязан быть объявлен заранее.

Команды плана проходят структурную безопасность (без chaining, newline и shell
metacharacters), но keyword-deny `run_command` к ним не применяется: reload,
save и commit-команды легитимны внутри подтверждённого плана.

Откат (reload/commit confirmed) — рекомендация модели, а не серверная механика:
сервер не хранит и не исполняет команды страховки. Модель по собственному
усмотрению может взвести откат перед изменениями и отменить его после
проверки; надёжность этой страховки — на ответственности модели и устройства,
а не сервера. Полный экспорт конфигурации на сторону сервера не выполняется
и не обязателен.
