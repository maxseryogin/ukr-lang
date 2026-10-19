# Запуск бота на Railway

## 1. Подготовь токен

Если токен попал на скриншот или в переписку, перевыпусти его через `/revoke` в [@BotFather](https://t.me/BotFather). Новый токен добавляется в переменные Railway. В GitHub его сохранять не нужно.

## 2. Подключи репозиторий

1. Открой https://railway.com и войди через GitHub.
2. Выбери **New Project → Deploy from GitHub repo** и репозиторий **maxseryogin/ukr-lang**.
3. Если репозитория нет в списке, открой настройку GitHub App и разреши Railway доступ к `ukr-lang`.
4. В сервисе оставь **Root Directory** пустым или `/`: `bot.py` и `Dockerfile` лежат в корне репозитория. Локальный путь `outputs/ukrainian_bot` здесь указывать не надо.

Railway обнаружит Dockerfile. Файл `railway.json` задаёт сборку Docker, один экземпляр бота, перезапуск при сбое до 10 раз и нулевое время перекрытия старого и нового развёртывания. Отдельная Build Command или Start Command не требуется: Dockerfile запускает `python bot.py`.

Первый запуск до добавления токена может завершиться сообщением о BOT_TOKEN — добавь настройки ниже и повтори Deploy.

## 3. Добавь переменные

В сервисе открой **Variables → Raw Editor** и добавь:

```dotenv
BOT_TOKEN=ВСТАВЬ_НОВЫЙ_ТОКЕН_ОТ_BOTFATHER
STUDENT_ID=1374449699
TEACHER_ID=1252270301
TIMEZONE=Europe/Kyiv
START_HOUR=17
END_HOUR=22
DB_PATH=/data/bot.sqlite3
```

Замени только значение BOT_TOKEN на реальный токен, без кавычек. Сохрани переменные. Файл `.env` на сервер не загружается: Railway передаёт значения через окружение.

## 4. Подключи постоянный диск

В схеме проекта нажми правой кнопкой → **Create Volume**, выбери сервис бота и укажи **Mount Path: `/data`**. При другом расположении пункта найди Create Volume через меню создания ресурсов проекта.

Переменная `DB_PATH=/data/bot.sqlite3` и диск `/data` должны совпадать. Без Volume темы, оценки и очередь могут потеряться при следующем развёртывании. `railway.json` сам не создаёт диск — этот шаг нужно выполнить в Railway.

На новом диске база будет пустой. Локальная папка `data/` в GitHub не отправляется. Если уже есть важные занятия, останови локального бота, сделай копию `data/bot.sqlite3` и перенеси её в `/data/bot.sqlite3` через управление файлами Volume до запуска облачной копии. Загрузка файлов описана в [документации Volumes](https://docs.railway.com/volumes#manage-volume-files).

## 5. Запусти

1. Останови бота на компьютере: **Ctrl+C** в PowerShell либо остановка запуска в редакторе. Одновременно должен работать только один бот с этим токеном, иначе Telegram вернёт ошибку 409.
2. В Railway отключи **Serverless / App Sleeping**, если эта настройка включена. Бот должен постоянно работать по расписанию. Оставь **Replicas: 1**.
3. Примени изменения кнопкой **Deploy**. Не включай Cron Schedule.
4. В **Deploy Logs** дождись `Bot running; timezone=Europe/Kyiv`.
5. Открой бота с обоих аккаунтов, отправь `/start`, затем `/status`.

Домен, публичный порт и HTTP Healthcheck этому боту не нужны: он сам получает события Telegram через polling. Поле Healthcheck Path оставь пустым. Docker Compose используется только для локального запуска; на Railway его роли выполняют Dockerfile, Variables и Volume.

## 6. Проверь занятие

С аккаунта преподавателя:

1. `/new Тестовая тема`
2. PDF, ссылка и голосовое отдельными сообщениями.
3. `/publish`

С аккаунта ученика проверь получение материалов, кнопку завершения, оценку и запрос следующей темы. Проверить расписание можно в ближайшую пятницу/субботу: 17:00–22:00 по Киеву, каждый час. После завершения темы напоминаний о ней больше нет.

## Обновления и ошибки

- После отправки новых коммитов в `main` Railway может автоматически развёртывать обновления подключённого сервиса. Volume сохраняется.
- `401` — неверный или отозванный токен: обнови BOT_TOKEN и повтори Deploy.
- `409` — работает другая копия бота либо настроен webhook. Останови прежний процесс/развёртывание. Этот проект использует polling.
- `403` при доставке — получатель не нажал `/start` или заблокировал бота. После исправления бот повторит отправку.
- Ошибка о BOT_TOKEN — проверь Variables именно сервиса бота и примени изменения.
- Если база пустая после обновления — проверь подключение Volume к сервису и DB_PATH.
- Стоимость зависит от выбранного плана и использования ресурсов. Актуальные условия: https://railway.com/pricing. Проверь лимиты и бюджет в своём аккаунте перед постоянной работой.

## Основная локальная папка

Основной рабочий репозиторий находится в папке, где уже лежит бот:

```text
C:\Users\mserj\Documents\Codex\2026-10-19\new-chat\outputs\ukrainian_bot
```

Открывай именно эту папку в редакторе. Она связана с `https://github.com/maxseryogin/ukr-lang.git`. Для дальнейших обновлений из PowerShell в этой папке:

```powershell
git status
git add bot.py README.md RAILWAY.md railway.json Dockerfile requirements.txt test_bot.py .gitignore .dockerignore .env.example compose.yaml
git commit -m "Update learning bot"
git push origin main
```

`.env`, `data/` и файлы базы исключены из Git. После изменения кода запускай `py -m unittest -v` перед отправкой.

Официальные инструкции: [GitHub-сервисы](https://docs.railway.com/services), [Dockerfile](https://docs.railway.com/builds/dockerfiles), [Variables](https://docs.railway.com/variables), [Volumes](https://docs.railway.com/volumes), [настройки развёртывания](https://docs.railway.com/config-as-code/reference).
