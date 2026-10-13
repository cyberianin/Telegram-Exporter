# Telegram Exporter / TGX

[Русский](#русский) · [English](#english)

## Русский

TGX сохраняет доступную вашему аккаунту историю Telegram через MTProto: сообщения,
авторов, ответы, пересылки и файлы. Экспорт идёт страницами в SQLite с контрольными
точками, поэтому его можно приостановить и продолжить. Telegram Desktop и takeout не нужны.

### Установка и первый вход

Нужен Python 3.12 или новее.

```shell
python -m pip install ".[server,bot]"
tgx serve
```

Откройте локальный адрес, который напечатает команда. Укажите свои `api_id` и `api_hash`
из [my.telegram.org/apps](https://my.telegram.org/apps), затем войдите по QR-коду либо
номеру телефона, коду и паролю двухэтапной проверки. Панель показывает состояние входа.
Ключ панели создаёт TGX; он нужен для доступа к сервису и не является паролем Telegram.

Для второго аккаунта нажмите «Добавить аккаунт», задайте другое имя профиля и войдите.
Сохранённые профили переключаются отдельным списком. Сессии аккаунтов хранятся раздельно.

### Выгрузка и скачивание

Выберите канал или диалог, объём истории, режим файлов и формат. Поддерживаются ID,
`@username`, ссылки `t.me` и `tg://chat?id=…`. Ссылки-приглашения не вызывают автоматическое вступление.

- Без файлов скачивается один файл `Название_дата_время.ext`.
- С файлами или HTML скачивается ZIP с одноимённой папкой. Распакуйте архив целиком и
  откройте `index.html` либо `messages.html`; ссылки на медиа и ответы работают локально.
- TXT содержит время, номер сообщения, автора, пересылку и `#номер -> #номер ответа`.
- HTML и JSON приближены к структуре экспорта Telegram Desktop; полные записи сохранены в JSONL.

Закрытие браузера не останавливает задание. При ограничении Telegram сервис ждёт и продолжает
автоматически. Ручная пауза требует ручного продолжения. Ошибки отдельных файлов не удаляют текст.
«Отменить и удалить» удаляет задание и его файлы; выход из аккаунта удаляет данные входа,
сохраняя готовые выгрузки. Коды входа и пароли двухэтапной проверки не сохраняются.

### Командная строка

Для установки без веб-панели достаточно `python -m pip install .`.

```shell
tgx auth login --account main
tgx dialogs --account main
tgx export --output archive.jsonl --format txt -- -1001234567890
tgx archive verify archive.jsonl
tgx doctor
```

Перед отрицательным ID используйте `--`. Полный список команд и режимов приведён ниже.
Преобразование готового архива в другой формат не требует подключения к Telegram.

### Сервер и бот — необязательно

Сервис можно целиком развернуть на VPS со своим доменом и HTTPS, а Telegram-бот подключить
как дополнительный интерфейс. По умолчанию достаточно локального запуска.
Для сервера скопируйте `.env.example` в `.env`, заполните необходимые настройки и следуйте
[инструкции развёртывания](docs/deployment.md). Секреты и каталог данных не должны попадать в Git.

```shell
docker compose --profile public up -d --build
docker compose --profile public --profile bot up -d --build
```

Бот доступен только пользователям из `TGX_BOT_ALLOWLIST`. Сервер рассчитан на одного владельца.
Пути данных задаются через `TGX_DATA_DIR`; иначе используется системный каталог пользователя.

### Проверки и ограничения

```shell
python -m pip install -e ".[dev,server,bot]"
python -m ruff check src tests deploy
python -m pytest
```

Проверены локальный вход, реальный экспорт истории, целостность архивов и синтетическая
обработка миллиона сообщений с ограниченной памятью. Реальный Docker/HTTPS, бот и другие ОС
имеют отдельный статус проверки; см. [отчёт](docs/spec-status.md).
TGX сохраняет то, что возвращает Telegram: завершение обхода не доказывает восстановление
удалённых или скрытых сервером сообщений. Доступ к истории и доступ к файлам проверяются отдельно.

Подробнее: [форматы и связи файлов](docs/portable-export.md),
[архитектура](docs/architecture.md), [развёртывание](docs/deployment.md).

## English

TGX exports history available to an authorized Telegram user account through regular MTProto.
The engine streams pages into SQLite/WAL, keeps durable checkpoints and generates portable archives.
Private channels and protected-content flags are diagnosed independently from media-download access.
It does not require Telegram Desktop Export or takeout.

### Install and open

Python 3.12+: `python -m pip install ".[server,bot]"`.
Run `tgx serve` and open its printed local URL. Enter your own Telegram `api_id` and `api_hash`,
then sign in by phone/code/2FA or QR. The interface shows the authenticated profile explicitly.
Sessions and configuration live in the OS user-data directory, or `TGX_DATA_DIR`.

For a CLI-only installation use `python -m pip install .`; it needs no running HTTP service,
Docker, Redis, PostgreSQL or Node.js. Credentials can come from `TGX_API_ID`/`TGX_API_HASH`,
a saved local setup, or the CLI prompt. Login codes and 2FA passwords are never persisted.

### CLI

```text
tgx auth login --account main
tgx auth qr --account main
tgx auth status --account main
tgx accounts
tgx dialogs --account main
tgx inspect -- -1001234567890
tgx export --output archive.jsonl --mode full --media full --format html -- -1001234567890
tgx jobs list
tgx jobs status JOB_ID
tgx jobs pause JOB_ID
tgx jobs resume JOB_ID
tgx jobs cancel JOB_ID
tgx media retry JOB_ID
tgx archive verify archive.jsonl
tgx archive convert archive.jsonl --format txt --output archive.txt
tgx archive import result.json 123
tgx doctor
tgx serve
tgx bot
```

Use `--` before a negative positional ID if your shell/CLI treats it as an option.
The resolver accepts canonical IDs, internal positive IDs, usernames, Telegram links, private
post links and `tg://` dialog links. Numeric IDs are matched against the profile's dialogs and
entity cache to obtain the real access hash. Invite links never join automatically.

Export options include `--mode` (full/text/media/range/incremental/reconcile/recovery),
`--strategy` (direct/compatibility/recovery/diagnostic), `--media`
(none/metadata/selected/full), `--format` (jsonl/json/html/txt/csv/sqlite/jsonl.zst),
`--min-id`, `--max-id`, `--since`, `--until`, `--page-size`, `--media-concurrency`.
Date bounds require an ISO 8601 timezone. JSONL is always retained as the canonical interchange file.
Conversion requires no Telegram connection. CSV escapes formula-like values; HTML escapes message text.
Use `--json-progress --non-interactive` for automation.

### Web, server and bot

The browser manages a durable queue; closing it does not cancel an export. The panel provides
history probes, progress, named downloads, pause/resume, cancellation with deletion and logout
with removal of saved authentication data. Archive names include the source dialog and time;
technical job IDs stay internal to the interface.

API endpoints are under `/api/v1`; owner authentication uses a Bearer token. For loopback-only
startup the CLI generates and stores a private owner token. Network listening requires
`TGX_OWNER_TOKEN` of at least 24 characters. Artifact download links expire after 15 minutes.

Optional whole-service deployment on your VPS/domain uses `docker compose --profile public up -d --build`.
Optional bot deployment adds `--profile bot`. Caddy serves HTTPS; the bot uses aiogram 3 and
an explicit Telegram user-ID allowlist. The worker runs independently of both browser and bot.

See [deployment instructions](docs/deployment.md), [architecture](docs/architecture.md)
and [specification status and verification limits](docs/spec-status.md).

### Integrity and recovery

Every committed page updates messages and cursor in one transaction. A message's composite identity
is account/peer/message ID, with per-job membership and observed edit versions. Resume retains the
captured upper boundary; repeated exports do not reuse another job's count or expand its output range.

Media has a separate bounded queue with retries, file-reference refresh, size checks, SHA-256,
temporary files and atomic publication. File failures preserve text and appear as warnings.
Archive verification checks manifests, ordering, counts and media hashes. Scanning completion is
distinct from proof that unavailable or previously deleted history ever existed.

### Development and verification

`python -m pip install -e ".[dev,server,bot]"`, `ruff check src tests deploy`, `pytest`.
Run the explicit scale test with `python tests/performance_scan.py`.
Tests use synthetic transports; a real private-channel scan and downloaded TXT were also verified
during development. The scale test reaches 1,000,000 messages with bounded process memory.

CI includes Windows/macOS/Linux and Python 3.12/3.13. Standalone builds use PyInstaller;
release workflows build wheel/sdist and amd64/arm64 containers.
Current host verification is Windows. Live protected-media downloads, a real Bot API deployment,
Docker/Caddy and other OS/device behavior are reported separately as NOT VERIFIED.
TDLib, multi-user PostgreSQL and native mobile shells remain optional later extensions.
