# Установка и выкатка

Как поднять портал у себя и на сервере, как его проверяют и выкатывают.
Пошаговая инструкция системному администратору — [DEPLOY.md](../DEPLOY.md),
что положить в `.env` — [«Настройка»](configuration.md).

---

## Быстрый старт

### Docker (так это живёт в проде)

```bash
cp .env.example .env
docker run --rm -v "$PWD:/w" -w /w python:3.12-slim python -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(48))"
```

Полученную строку вставь в `.env` вместо заготовки, туда же — токен бота
и свой telegram id. Затем:

```bash
docker compose up -d --build
```

Открой http://localhost:8000. **Первый вошедший становится преподавателем.**

База лежит в именованном томе `sport-data` и переживает `docker compose down`
и пересборку образа. Миграции применяются при каждом старте контейнера.

> Если `docker compose` не работает как подкоманда, используй отдельный
> бинарник `docker-compose` — команды те же.

Без своего `SECRET_KEY` приложение **осознанно не стартует**: этим ключом
подписываются сессионные куки, и с известным значением любой может войти
под чужим аккаунтом. Сообщение об ошибке скажет, что делать.

### Локально

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env          # поставь DEV_LOGIN_ENABLED=true
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

С `DEV_LOGIN_ENABLED=true` на `/login` появится форма входа по имени —
Telegram-бот для разработки не нужен. **В проде обязательно `false`.**

---

## Выкатка на сервер

> Отдельная инструкция для системного администратора — [DEPLOY.md](../DEPLOY.md):
> что положить в `.env`, как поднять, проксировать и бэкапить.

Что нужно на сервере: `git`, `docker`, `docker compose`.

```bash
git clone <адрес репозитория> /srv/sport
cd /srv/sport
cp .env.example .env
python3 -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(48))"   # в .env
docker compose up -d --build
```

Сервис слушает `127.0.0.1:8000`. Наружу его отдаёт обратный прокси —
он же выдаёт HTTPS, без которого Telegram-вход через штатный виджет
работать не будет (вход через бота работает и без домена).

Проверка перед боем:

| | |
|---|---|
| `SECRET_KEY` | свой, не из примера |
| `DEV_LOGIN_ENABLED` | `false` |
| `TELEGRAM_BOT_TOKEN` | задан, иначе входа не будет |
| `TEACHER_TELEGRAM_IDS` | твой telegram id |
| `BASE_URL` | внешний адрес с `https://` |
| Порт 8000 | закрыт снаружи, доступен только прокси |

### Обновление

```bash
cd /srv/sport && git pull && docker compose up -d --build
```

### Резервная копия

Простой `cp` файла базы не подходит: при включённом WAL часть транзакций
лежит в `sport.db-wal` и копия может оказаться битой. Правильный способ —
через backup API SQLite, он безопасен на работающем сервисе:

```bash
docker compose exec web python -m app.cli backup /data/backup.db
docker compose cp web:/data/backup.db ./backup-$(date +%F).db
```

Восстановление — положить файл как `sport.db` в том `sport-data`
при остановленном контейнере.

---

## Проверки и выкатка

**Проверки** — линтер и тесты — гоняются на GitHub
(`.github/workflows/ci.yml`) на каждый push и pull request, плюс там же
проверяется, что собирается образ. Локально то же самое:

```bash
ruff check app tests scripts migrations
pytest -q
```

**Выкатка** живёт отдельно и в открытую копию не уезжает: она привязана
к конкретной виртуалке. Дальше — про неё.

### Открытая копия

Правки идут в наш Gitea, оттуда выкатываются на сервер, и оттуда же снимок
уходит на [GitHub](https://github.com/kirillyat/sport-prog) одной
командой:

```bash
python scripts/publish_github.py          # --dry-run, чтобы посмотреть
```

Скрипт собирает копию без того, чему в ней не место, и кладёт то, что нужно
только ей:

| Каталог | Почему |
|---|---|
| `course/` | материалы курса — чужая авторская работа, под MIT их выкладывать нельзя |
| `.gitea/` | выкатка на нашу машину; чужому вузу не нужна, а Gitea и GitHub ещё и подрались бы за один каталог workflow |
| `.claude/skills/msu-*` | навыки агентов про наш экземпляр: сервер, судья, курс. Общие навыки уезжают вместе с кодом |
| `publish/` | сама обвязка: `.github/workflows`, CONTRIBUTING и `mkdocs.yml` раскладываются отсюда по корню открытой копии |

Публикуется снимок — один коммит на публикацию, со списком вошедшего
в сообщении. Так открытая история читается и не расходится с рабочей по
хэшам, иначе каждый раз потребовался бы force push.

Портал без `course/` работает как обычно: раздел «Курс» просто не
показывается, пока материалов нет (`course.is_available()`). Фирменный знак
вуза так же живёт в томе данных, а не в исходниках.

### Выкатка на нашем стенде

Выкатка живёт в `.gitea/workflows/deploy.yml`. Она запускается на push
в `main` и кнопкой «Run workflow» на вкладке Actions, а всю работу делает
скрипт `/usr/local/bin/sport-autodeploy` на сервере: забирает новый `main`,
прогоняет линтер и тесты в отдельном контейнере и, только если они прошли,
пересобирает портал.

Раннер зарегистрирован на самой виртуалке с меткой `algo:host` — шаги идут
прямо на машине, а не в контейнере: выкатка и так живёт в скриптах на этом
хосте, и запускать их изнутри контейнера значило бы городить docker в docker.

Рядом работает страховка — systemd-таймер `sport-autodeploy.timer`, который
раз в минуту сверяет `main` в Gitea с развёрнутым и выкатывает, если тот
ушёл вперёд. Он появился раньше Actions и остаётся на случай, когда раннер
недоступен; подробности в [DEPLOY.md](../DEPLOY.md#6а-выкатка-из-gitea).

**Откат кода не откатывает схему** — если миграция уже применилась,
возвращайся вперёд, а не назад.

---

## Команды

```bash
.venv/bin/python -m app.cli sync-catalog       # скачать каталоги задач
.venv/bin/python -m app.cli sync-submissions   # обновить посылки вручную
.venv/bin/python -m app.cli make-teacher Имя   # выдать роль преподавателя
.venv/bin/python -m app.cli stats              # сводка по базе
.venv/bin/python -m app.cli assignments        # задания с номерами и сроками
# продлить задания 12 и 13 до 3 октября 23:59 по DISPLAY_TIMEZONE;
# без --apply — пробный прогон, с ним — сначала копия базы рядом с ней
.venv/bin/python -m app.cli extend-deadline 2026-10-03T23:59 12 13 --apply
```

Миграции — только через Alembic:

```bash
.venv/bin/alembic revision --autogenerate -m "описание"
.venv/bin/alembic upgrade head
.venv/bin/alembic check      # проверить, что модели и миграции не разъехались
```

Тесты и линтер:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```
