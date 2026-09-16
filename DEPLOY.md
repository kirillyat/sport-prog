# Развёртывание — инструкция системному администратору

Учебный портал «Алгоритмы ФИИ МГУ». Одно веб-приложение на Python
и база SQLite в томе Docker. Внешних сервисов не требует: ни СУБД, ни очередей,
ни кеша.

Репозиторий: `https://git.ai.msu.ru/kirillyat/sport-prog-club`

---

## 1. Что нужно на сервере

| | |
|---|---|
| ОС | любая с Docker |
| Пакеты | `git`, `docker`, `docker compose` (или `docker-compose`) |
| Диск | 1 ГБ достаточно: база с каталогом задач ~30 МБ, растёт медленно |
| Память | 512 МБ |
| Порты | приложение слушает `127.0.0.1:8000`, наружу его отдаёт обратный прокси |
| Исходящий доступ | `codeforces.com`, `leetcode.com`, `api.telegram.org` — без них не будут подтягиваться решения и работать вход |

HTTPS обязателен: без него не работает вход через Authentik, а сессионные куки
передаются открытым текстом.

---

## 2. Что предоставляет владелец портала

Значения из блока Telegram присылает владелец портала. Значения OIDC заводит
и возвращает администратор Authentik — см. раздел 3. Ничего из этого нельзя
коммитить в репозиторий: файл `.env` намеренно в `.gitignore`.

| Переменная | Что это | Где взять |
|---|---|---|
| `BASE_URL` | внешний адрес портала, обязательно `https://` | заполняет admin, когда домен известен; тот же адрес идёт в Redirect URI |
| `TELEGRAM_BOT_TOKEN` | токен бота | @BotFather, даёт Кирилл |
| `TELEGRAM_BOT_USERNAME` | имя бота без `@` | там же |
| `TELEGRAM_NOTIFY_CHAT_ID` | необязательно: общий чат. Пусто — бот пишет каждому лично | даёт Кирилл, если чат появится |
| `TEACHER_TELEGRAM_IDS` | telegram id преподавателей через запятую | даёт Кирилл |
| `ASSIGNMENT_REMINDER_HOURS` | за сколько часов до дедлайна напомнить (по умолчанию 24) | можно не трогать |
| `OIDC_ISSUER` | адрес провайдера Authentik | админ Authentik, см. раздел 3 |
| `OIDC_CLIENT_ID` | идентификатор приложения | админ Authentik |
| `OIDC_CLIENT_SECRET` | секрет; пусто для публичного клиента | админ Authentik |
| `OIDC_TEACHER_GROUPS` | группа, дающая роль преподавателя | админ Authentik |

`SECRET_KEY` не передаётся: его генерирует admin прямо на сервере (шаг 4) и
никому не сообщает.

---

## 3. Authentik — что создать и что вернуть

Этот раздел для того, у кого есть админ-доступ к Authentik. Владелец портала
такого доступа не имеет, поэтому провайдера заводит администратор и возвращает
четыре значения.

### Создать провайдера

**Applications → Providers → Create → OAuth2/OpenID Provider**

| Поле | Значение |
|---|---|
| Name | `sport-prog-club` (любое понятное) |
| Authorization flow | стандартный `default-provider-authorization-explicit-consent` |
| Client type | **Public** — тогда секрет не нужен, используется PKCE. Confidential тоже подойдёт |
| Redirect URIs | `https://<домен портала>/login/oidc/callback` — строго этот адрес, одной строкой |
| Scopes | `openid`, `profile`, `email` |
| Subject mode | по умолчанию (`Based on the User's hashed ID`) |

### Создать приложение

**Applications → Applications → Create**, привязать к созданному провайдеру.
`Slug` приложения попадает в адрес issuer, поэтому его удобно сделать
`sport-prog-club`.

### Группы

Роль преподавателя на портале выдаётся по членству в группе Authentik.
Нужно, чтобы в токене был клейм со списком групп — в Authentik это делается
scope mapping'ом для `groups`. Если клейм называется не `groups`, сообщите его
имя: на стороне портала оно настраивается переменной `OIDC_GROUPS_CLAIM`.

Создать (или указать существующую) группу для преподавателей, например
`sport-teachers`, и добавить туда нужных людей.

### Что вернуть владельцу портала

| Что прислать | Где взять в Authentik | Переменная в `.env` |
|---|---|---|
| Issuer | карточка провайдера → **OpenID Configuration Issuer**, вид `https://<authentik>/application/o/sport-prog-club/` | `OIDC_ISSUER` |
| Client ID | карточка провайдера → **Client ID** | `OIDC_CLIENT_ID` |
| Client Secret | только если выбран Confidential; для Public — прислать пустым | `OIDC_CLIENT_SECRET` |
| Имя группы преподавателей | как названа группа | `OIDC_TEACHER_GROUPS` |

Client Secret — это секрет: передавать его тем же каналом, что и остальные
токены, и не оставлять в переписке, которая архивируется.

### Если Authentik не подключаем

Оставить `OIDC_ISSUER` пустым — блок входа через Authentik просто не появится
на странице входа. Вход через Telegram работает независимо.

---

## 4. Установка

```bash
git clone https://git.ai.msu.ru/kirillyat/sport-prog-club.git /srv/sport
cd /srv/sport
cp .env.example .env
```

Сгенерировать ключ подписи сессий и вписать его в `.env` вместо заготовки:

```bash
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
```

Заполнить остальные значения из таблицы выше. Проверить, что
`DEV_LOGIN_ENABLED=false` — эта настройка открывает вход по одному лишь имени.

Запустить:

```bash
docker compose up -d --build
```

Приложение само применит миграции при старте и начнёт скачивать каталоги задач
Codeforces и LeetCode — около 15 000 задач, занимает 1–2 минуты.

Проверка:

```bash
curl -fsS http://127.0.0.1:8000/healthz     # {"status":"ok"}
docker compose logs --tail 30
```

Если `SECRET_KEY` не задан, контейнер **осознанно не стартует** и пишет об этом
в лог: с известным ключом любой может подделать вход под чужим аккаунтом.

---

## 5. Обратный прокси

Наружу отдаётся один порт. Пример для nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name algo.ai.msu.ru;

    ssl_certificate     /etc/letsencrypt/live/algo.ai.msu.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/algo.ai.msu.ru/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Порт 8000 наружу закрыть — он должен быть доступен только прокси.

---

## 6. Проверка перед вводом в строй

| | |
|---|---|
| `SECRET_KEY` | свой, не из примера |
| `DEV_LOGIN_ENABLED` | `false` |
| `BASE_URL` | внешний адрес с `https://` |
| Порт 8000 | закрыт снаружи |
| `/healthz` | отвечает через прокси по HTTPS |
| Первый вход | **первый, кто войдёт, станет преподавателем** — пусть это будет Кирилл |

---

## 6а. Выкатка из Gitea

Мерж в `main` запускает задачу **Gitea Actions** (`.gitea/workflows/deploy.yml`),
которая зовёт `/usr/local/bin/sport-autodeploy` на этой же машине. Там же есть
кнопка **Run workflow** на вкладке Actions — ручной запуск без коммита.

Раннер `act_runner` зарегистрирован на виртуалке с меткой `algo:host`, служба
`act_runner.service`. Две настройки, без которых он молча не работает:
`workdir_parent` в `/etc/act_runner/config.yaml` и `Environment=HOME=` в
unit-файле — у службы systemd своего HOME нет, и рабочий путь задачи
получается пустым; задача при этом падает мгновенно и **без единой строки
вывода**, что сбивает с толку сильнее любой ошибки.

Запасной путь — таймер `sport-autodeploy.timer`: раз в минуту он спрашивает
у Gitea хеш ветки `main` и, если тот отличается от развёрнутого, забирает
изменения и пересобирает контейнер тем же скриптом. Сейчас **выключен**;
включается одной командой, если раннер недоступен:

```bash
systemctl enable --now sport-autodeploy.timer
```

```bash
systemctl status sport-autodeploy.timer   # работает ли
tail -f /srv/autodeploy.log               # что выкатывалось
systemctl start sport-autodeploy.service  # проверить прямо сейчас, не дожидаясь минуты
```

**Перед сборкой прогоняются линтер и тесты** — в отдельном контейнере из
`Dockerfile.tests`. Не прошли: выкатка отменяется, а на сервере продолжает
работать прежняя версия; в логе видно, что именно упало. Прогон занимает
около половины минуты и идёт на копии кода **без боевого `.env`** — иначе
тесты поехали бы на настройках прода и посыпались на ровном месте.

Образ для тестов пересобирается, только когда меняется `pyproject.toml`:
тег образа считается по его содержимому.

Перемотка только вперёд: если историю `main` переписали, автовыкатка
остановится и напишет об этом в лог — такие случаи разбираются руками.

Почему опрос, а не Actions или вебхук: Actions на этом Gitea выключены
(в репозитории нет вкладки Actions), а вебхуку некуда прийти — снаружи
к контейнеру ходит только трафик портала. Опрос ни от кого не зависит;
когда включат Actions, его место займёт обычный workflow.

Выключить автовыкатку: `systemctl disable --now sport-autodeploy.timer`.

---

## 7. Обновление

```bash
cd /srv/sport
docker compose exec -T web python -m app.cli backup /data/backup-$(date +%F).db
git pull
docker compose up -d --build
```

Миграции применяются сами при старте. **Откат кода не откатывает схему базы:**
если миграция уже применилась, возвращаться нужно вперёд, а не назад.

---

## 8. Резервное копирование

Обычное копирование файла базы не подходит: при включённом режиме WAL часть
транзакций лежит в `sport.db-wal`, и копия может оказаться битой. Правильный
способ работает на живом сервисе:

```bash
docker compose exec -T web python -m app.cli backup /data/backup.db
docker compose cp web:/data/backup.db /var/backups/sport-$(date +%F).db
```

В cron, ежедневно в 4 утра:

```cron
0 4 * * * cd /srv/sport && docker compose exec -T web python -m app.cli backup /data/backup.db && docker compose cp web:/data/backup.db /var/backups/sport-$(date +\%F).db
```

Файлы материалов и присланные решения в эту копию не попадают — они лежат не в базе,
а рядом с ней. Их забирает обычное копирование каталога:

```bash
docker compose cp web:/data/materials /var/backups/materials-$(date +%F)
```

Восстановление: остановить контейнер, положить файл как `sport.db` и каталог
`materials` в том `sport_sport-data`, запустить снова.

**Копии перед выкаткой чистятся сами.** Каждая выкатка кладёт в том свежий
`backup-ГГГГ-ММ-ДД-ЧЧММ.db` и удаляет всё, кроме последних двадцати —
иначе при автоматической выкатке том зарастал бы копиями. Число задаётся
переменной `KEEP_BACKUPS` в `/usr/local/bin/sport-deploy`.

Двадцать копий — это двадцать последних выкаток, а не двадцать дней:
в активный день их может набежать несколько. Если нужна история за неделю,
её даёт отдельный ежедневный бэкап по cron выше — он кладёт файлы за пределы
тома, и ротация выкатки их не трогает.

---

## 9. Если что-то не так

| Симптом | Причина |
|---|---|
| Контейнер не стартует, в логе про `SECRET_KEY` | ключ не задан или оставлен из примера |
| Вход через Telegram не предлагается | пустой `TELEGRAM_BOT_TOKEN` или `TELEGRAM_BOT_USERNAME` |
| Вход через Authentik не предлагается | пустой `OIDC_ISSUER` или `OIDC_CLIENT_ID` |
| `401` от Authentik после входа | Redirect URI в Authentik не совпадает с `<BASE_URL>/login/oidc/callback` |
| Решения студентов не подтягиваются | нет исходящего доступа к `codeforces.com` / `leetcode.com`, либо аккаунт не подтверждён студентом |
| Уведомления не приходят | пустой `TELEGRAM_BOT_TOKEN`; либо чат задан, но бот в него не добавлен; либо адресат не входил через бота |

Логи: `docker compose logs -f web`. Ошибки синхронизации видны и в интерфейсе —
на странице «Аккаунты» у студента.

---

## Приложение. Просьба по Gitea

На `git.ai.msu.ru` отключено зеркалирование репозиториев
(`mirrors_disabled: true` в API). Если это возможно, просьба включить в
`app.ini`:

```ini
[mirror]
ENABLED = true
```

Это позволит держать копию репозитория на GitHub автоматически, без ручных
операций и без хранения токенов в CI.

Отдельно: выкатку на стороне Gitea выполняет `act_runner`, зарегистрированный
на самой виртуалке с меткой `algo:host`; workflow лежит
в `.gitea/workflows/deploy.yml`.
