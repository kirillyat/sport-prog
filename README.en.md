# Competitive programming portal

A teaching portal for university programming courses. A teacher assigns problems
from LeetCode and Codeforces; the portal pulls each student's submissions by
itself and keeps the scoreboard. No spreadsheets, no "send me a screenshot".

One Python process and a SQLite file in a volume. No Postgres, no Kafka, no
Redis, no separate worker. It runs on the smallest VM you can rent: measured
peak is **86 MB of RAM** with the full problem catalogue loaded.

The project was written for the Faculty of AI at Moscow State University and is
in daily use there, but nothing in it is specific to that university: the name,
the wording about student accounts and the logo are configuration.

## What it does

**For students** — link Codeforces and LeetCode accounts (ownership is
verified), join a group by code, see assignments with deadlines and progress,
a solution feed, seminar materials and course notebooks rendered in the portal,
announcements with a countdown, a profile with stats, and a per-group scoreboard.

**For teachers** — groups with join codes, problem lists built by pasting links,
assignments with soft or hard deadlines, a `students × problems` matrix, manual
bonus points, CSV exports, a review queue for uploaded solutions, and per-section
visibility switches.

**Under the hood** — a background syncer pulls submissions every 10 minutes and
refreshes the problem catalogue daily (11 400 Codeforces + 4 055 LeetCode
problems). Notebooks are rendered server-side: formulas become MathML, code is
highlighted by Pygments, SVG is served inside `<img>` so it cannot execute
anything. No CDN requests at all — fonts and icons ship with the app.

## Running it

```bash
cp .env.example .env
docker run --rm -v "$PWD:/w" -w /w python:3.12-slim \
    python -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(48))"
# put the key into .env, then:
docker compose up -d --build
```

Open http://localhost:8000. **The first person to sign in becomes a teacher.**

Without your own `SECRET_KEY` the app refuses to start: session cookies are
signed with it, and a known value lets anyone sign in as anyone.

## Making it yours

| Setting | What it changes |
|---|---|
| `APP_NAME`, `APP_SHORT_NAME` | title in the rail, browser tab, notifications |
| `ORG_NAME` | the wording "confirm you are a student of X" |
| `ORG_ACCOUNT_NAME` | what the institutional login is called |
| `COURSE_TITLE`, `COURSE_SUBTITLE` | the heading of the "Course" section |
| `DEFAULT_LANGUAGE` | `ru`, `en` or `fr` for visitors who never chose |
| `OIDC_*` | any OpenID Connect provider; group → teacher role mapping |
| `TELEGRAM_*` | optional login and notifications through a bot |

Branding lives in the data volume, not in the image: drop `logo.svg`,
`logo-dark.svg`, `mark.svg` or `favicon.svg` into `DATA_DIR/branding` and the
portal picks them up — no rebuild, and your emblem never enters the source tree.

## Languages

The interface speaks Russian, English and French. The switcher sits in the rail
next to the theme and on the sign-in page — a language is needed before signing
in no less than after. The choice lives in the `lang` cookie; for a first-time
visitor the browser suggests one through `Accept-Language`, and
`DEFAULT_LANGUAGE` closes the chain.

The mechanism is deliberately small: **the string in the template is the
dictionary key**.

```jinja
{{ _("Задания") }}
```

The translation is looked up in `app/locales/en.json` and `fr.json`. Not found —
the Russian source is shown, so a partial translation never breaks a page, it
only leaves it Russian. No gettext, no `.mo` compilation step.

To add a language: put its code and endonym into `LANGUAGES` in `app/i18n.py`,
run `python scripts/i18n_sync.py` — it creates the dictionary and lists what is
missing — then fill in `app/locales/<code>.json`. Plural rules for the new
language go into `plural` (`app/templating.py`) and `portalPlural`
(`app/static/i18n.js`). Tests check that `%(count)s` placeholders survive the
translation and that the number of word forms matches the language.

Authentication is deliberately pluggable: sign-in through a Telegram bot works
without a public domain, OIDC works with any provider, and a dev login exists
for local work. Student identity is confirmed by the institutional account,
never by Telegram.

## Documentation

The detailed documentation is in Russian: [README.md](README.md) for how the
system works and why it is built this way, [DEPLOY.md](DEPLOY.md) for the
deployment runbook. Both are worth reading before changing the scoring rules —
they explain the decisions behind them.

## License

MIT, see [LICENSE](LICENSE). The MSU coat of arms that the reference instance
uses is a trademark and is **not** part of this repository; the default mark
shipped here is a neutral one.
