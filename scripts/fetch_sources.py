#!/usr/bin/env python3
"""Забирает исходники посылок и отдаёт их порталу.

Почему это скрипт на машине преподавателя, а не работа сервера: страницы
посылок и на Codeforces, и на LeetCode закрыты Cloudflare для автоматических
клиентов — проверено и с сервера, и из управляемого браузера, везде 403 либо
вечное «Just a moment…». Обычный Chrome проверку проходит, поэтому страницы
открываются им, а портал только принимает результат.

Что нужно:

    pip install playwright httpx
    playwright install chromium      # или используйте свой Chrome: --channel chrome

Запуск (токен берётся из окружения, в командной строке ему не место):

    INGEST_TOKEN=... python scripts/fetch_sources.py https://algo.ai.msu.ru

Первый запуск открывает окно браузера: пройдите проверку Cloudflare и, если
нужен LeetCode, войдите в свой аккаунт. Профиль сохраняется в ~/.sport-fetcher,
дальше запуски проходят молча.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

PROFILE = Path.home() / ".sport-fetcher"
CF_SOURCE = "#program-source-text"


def codeforces_url(external_id: str, problem_slug: str | None) -> str | None:
    """Номер контеста лежит в слаге задачи: «1234a» → 1234."""
    match = re.match(r"^(\d+)", problem_slug or "")
    if not match:
        return None
    return f"https://codeforces.com/contest/{match.group(1)}/submission/{external_id}"


def leetcode_url(external_id: str, problem_slug: str | None) -> str:
    return f"https://leetcode.com/submissions/detail/{external_id}/"


async def open_page(page, url: str) -> bool:
    """Переход, который не роняет всю работу: страница может увести на логин."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        return True
    except Exception as exc:
        print(f"    страница не открылась: {str(exc).splitlines()[0]}")
        return False


async def grab_codeforces(page, url: str) -> str | None:
    if not await open_page(page, url):
        return None
    try:
        await page.wait_for_selector(CF_SOURCE, timeout=45_000)
    except Exception:
        return None
    return await page.inner_text(CF_SOURCE)


async def grab_leetcode(page, url: str, external_id: str) -> str | None:
    """Код берём запросом самой страницы: так работает её сессия, а не наша."""
    if not await open_page(page, url):
        return None
    if "accounts/login" in page.url:
        print("    LeetCode просит войти — запустите с --login")
        return None
    query = """
    query detail($id: Int!) {
      submissionDetails(submissionId: $id) { code }
    }
    """
    try:
        result = await page.evaluate(
            """async ([query, id]) => {
                const r = await fetch('/graphql/', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({query, variables: {id: Number(id)}}),
                });
                const data = await r.json();
                return data?.data?.submissionDetails?.code ?? null;
            }""",
            [query, external_id],
        )
    except Exception:
        return None
    return result


async def login(args) -> int:
    """Открывает окно и ждёт, пока в профиле появится сессия LeetCode.

    Профиль остаётся на диске, поэтому вход нужен один раз, а не каждый запуск.
    """
    import asyncio

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch_persistent_context(
            str(PROFILE),
            channel=None if args.channel == "chromium" else args.channel,
            headless=False,
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        await open_page(page, "https://leetcode.com/accounts/login/")
        print(f"окно открыто: войдите в LeetCode. Жду до {args.wait} с…")

        for _ in range(args.wait):
            cookies = await browser.cookies("https://leetcode.com")
            if any(c["name"] == "LEETCODE_SESSION" and c["value"] for c in cookies):
                print("сессия LeetCode есть — профиль сохранён")
                await browser.close()
                return 0
            await asyncio.sleep(1)

        print("не дождался входа; попробуйте ещё раз")
        await browser.close()
        return 1


async def main() -> int:
    import httpx
    from playwright.async_api import async_playwright

    parser = argparse.ArgumentParser(description="Загрузка исходников посылок в портал")
    parser.add_argument("portal", help="адрес портала, например https://algo.ai.msu.ru")
    parser.add_argument("--limit", type=int, default=25, help="сколько посылок за раз")
    parser.add_argument("--channel", default="chromium", help="chromium или chrome")
    parser.add_argument("--headless", action="store_true", help="без окна (после первой проверки)")
    parser.add_argument(
        "--login",
        action="store_true",
        help="открыть браузер и подождать, пока вы войдёте на площадки",
    )
    parser.add_argument("--wait", type=int, default=300, help="сколько ждать входа, секунд")
    args = parser.parse_args()

    if args.login:
        return await login(args)

    token = os.environ.get("INGEST_TOKEN", "")
    if not token:
        print("нет INGEST_TOKEN в окружении", file=sys.stderr)
        return 2

    headers = {"X-Ingest-Token": token}
    async with httpx.AsyncClient(base_url=args.portal, headers=headers, timeout=30) as api:
        response = await api.get("/ingest/wanted", params={"limit": args.limit})
        response.raise_for_status()
        wanted = response.json()
        if not wanted:
            print("исходники всех посылок уже загружены")
            return 0
        print(f"ждут исходника: {len(wanted)}")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch_persistent_context(
                str(PROFILE),
                channel=None if args.channel == "chromium" else args.channel,
                headless=args.headless,
            )
            page = browser.pages[0] if browser.pages else await browser.new_page()

            saved = skipped = 0
            for item in wanted:
                platform, external_id = item["platform"], item["external_id"]
                slug = item.get("problem_slug")
                if platform == "codeforces":
                    url = codeforces_url(external_id, slug)
                    code = await grab_codeforces(page, url) if url else None
                else:
                    code = await grab_leetcode(page, leetcode_url(external_id, slug), external_id)

                if not code:
                    skipped += 1
                    print(f"  — {platform} {external_id}: исходник не достался")
                    continue

                put = await api.post(
                    "/ingest/source",
                    json={"platform": platform, "external_id": external_id, "code": code},
                )
                if put.status_code == 200:
                    saved += 1
                    print(f"  ✓ {platform} {external_id}: {len(code)} символов")
                else:
                    skipped += 1
                    print(f"  — {platform} {external_id}: портал ответил {put.status_code}")

            await browser.close()

    print(f"загружено: {saved}, пропущено: {skipped}")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
