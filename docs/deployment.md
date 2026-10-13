# Запуск TGX

## Локально: Python или Windows EXE

Установите Python 3.12+ и выполните `python -m pip install ".[server,bot]"`.
`tgx serve` запускает локальную панель на `http://127.0.0.1:8765` и печатает ссылку с ключом владельца.
В панели укажите собственные `api_id` и `api_hash` из https://my.telegram.org/apps.
Вход доступен по номеру/коду/2FA или по QR через «Настройки → Устройства» в Telegram.

Сессии, реквизиты API и БД находятся в пользовательском каталоге TGX. Для другого каталога
задайте `TGX_DATA_DIR`. Коды подтверждения и пароль 2FA сохраняются только в памяти на время входа.
«Выйти и удалить данные входа» отзывает сессию и удаляет её файлы и сохранённые приложением API-реквизиты.
Переменные окружения и внешние файлы настройки сервера принадлежат администратору процесса.

Windows: `dist/tgx.exe serve` работает без установленного Python. Для самостоятельной сборки:

```powershell
python -m pip install ".[server,bot]" pyinstaller
python -m PyInstaller --onefile --name tgx --collect-data tgx --collect-all telethon --add-data "src/tgx/migrations:tgx/migrations" deploy/launcher.py
```

## Необязательный сервер с доменом

Приложение может целиком работать на вашем VPS: Python, SQLite, worker и веб-панель находятся
на сервере. Браузер подключается к вашему домену. Закрытие браузера не останавливает экспорт.

1. Создайте DNS A/AAAA-запись домена на сервер и откройте входящие порты 80/443.
2. Скопируйте `.env.example` в `.env`; заполните `TGX_API_ID`, `TGX_API_HASH`, `TGX_DOMAIN`
   и `TGX_PUBLIC_URL=https://ваш-домен`.
3. Сгенерируйте `TGX_OWNER_TOKEN`: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. Для шифрования серверных сессий задайте `TGX_MASTER_KEY` из
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   Храните ключ отдельно от резервной копии volume. Вход создаёт зашифрованную сессию;
   существующую plaintext-сессию требуется сначала вывести из рабочего каталога.
5. Запустите `docker compose --profile public up -d --build`.
6. Откройте домен, укажите ключ владельца и авторизуйте Telegram-аккаунт.

Caddy обслуживает домен и получает/обновляет HTTPS-сертификат. Требования DNS и доступности
портов описаны в [официальной документации Caddy](https://caddyserver.com/docs/quick-starts/https).
Сам TGX доступен на loopback сервера и во внутренней сети Compose; публичный вход проходит через proxy.

Локальный Docker без домена: `docker compose up -d --build`, панель `http://127.0.0.1:8765`.
Профили `public` и `bot` необязательны. Данные приложения и сертификаты сохраняются в volumes.
В файле `deploy/tgx.service` есть альтернативный systemd-сервис для прямой установки Python.

## Необязательный Telegram-бот

Создайте Bot API-токен и задайте `TGX_BOT_TOKEN`, `TGX_BOT_ALLOWLIST` (ID владельцев через запятую).
Запуск с доменом: `docker compose --profile public --profile bot up -d --build`.
Без Docker запустите API/worker через `tgx serve`, а отдельным процессом — `tgx bot`.
Для удалённого API задайте `TGX_API_URL`; `TGX_PUBLIC_URL` используется в ссылках на большие файлы.

Команды: `/account`, `/accounts`, `/dialogs`, `/inspect`, `/export`, `/settings`, `/jobs`, `/status`,
`/pause`, `/resume`, `/cancel`, `/files`. Бот управляет серверным worker через тот же API.
Маленькие файлы отправляются в чат, большие — через подписанную ссылку на 15 минут.
Остановка бота не прерывает уже созданные выгрузки.

## Другие платформы

Linux/macOS используют тот же Python-пакет. В Termux установите Python и пакет TGX,
после чего используйте CLI; состояние восстанавливается через `tgx jobs resume`.
iOS и Android могут пользоваться серверной панелью по HTTPS.
Workflow `standalone.yml` готовит сборки Windows, Linux, macOS Intel и Apple Silicon.

На текущем компьютере проверены Windows, Python-пакет и реальный экспорт приватного канала.
Контейнеры, systemd, остальные ОС и доставка через настоящий Bot API требуют отдельного окружения.
