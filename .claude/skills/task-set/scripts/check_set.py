#!/usr/bin/env python3
"""Проверяет набор своих задач до загрузки на портал и собирает архив.

Набор — папка, в ней папка на задачу:

    набор/
      two-sum/
        statement.md            условие с шапкой (title, time_limit_ms, …)
        tests/open/01.in|.out   открытые тесты
        tests/closed/01.in|.out закрытые
        solution.py             эталон — обязателен
        brute.py                медленный, но заведомо верный — по желанию
        wrong/*.py              типичные ошибки — каждая должна упасть
        slow/*.py               верные, но медленные — не должны уложиться

Что проверяется:
- архив проходит те же правила, что и загрузка на портале;
- эталон на каждом тесте печатает ровно .out и укладывается в лимит с запасом;
- перебор совпадает с ответом на маленьких тестах;
- каждое неверное решение падает хотя бы на одном тесте — иначе тесты слабые;
- медленные решения не укладываются в лимит.

Запуск из корня репозитория портала (нужен код портала для правил архива):

    python .claude/skills/task-set/scripts/check_set.py набор/ --zip набор.zip [--python python3.8]

В архив попадают только statement.md и tests/: эталоны на портал не уезжают.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

LIMIT_BYTES = 256 * 1024
BRUTE_MAX_INPUT = 2_000          # перебор гоняем только на маленьких тестах
HEADROOM = 3                     # эталон должен укладываться в лимит с таким запасом


def tests_of(task: Path) -> list[tuple[str, Path, Path]]:
    found = []
    for kind in ("open", "closed"):
        for stdin in sorted((task / "tests" / kind).glob("*.in")):
            found.append((f"{kind}/{stdin.stem}", stdin, stdin.with_suffix(".out")))
    return found


def run(python: str, program: Path, stdin: str, timeout: float) -> tuple[str | None, float, str]:
    start = time.perf_counter()
    try:
        done = subprocess.run([python, str(program)], input=stdin, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, timeout, "timeout"
    return done.stdout, time.perf_counter() - start, done.stderr[-300:] if done.returncode else ""


def same(out: str | None, expected: str) -> bool:
    """Как сравнивает судья, мы точно не знаем — берём строгое сравнение по строкам
    без хвостовых пробелов: если прошло оно, пройдёт и мягкое."""
    if out is None:
        return False
    norm = lambda s: [line.rstrip() for line in s.rstrip().splitlines()]  # noqa: E731
    return norm(out) == norm(expected)


def time_limit(statement: str) -> float:
    match = re.search(r"^time_limit_ms:\s*(\d+)", statement, re.M)
    return (int(match.group(1)) if match else 2000) / 1000


def archive(tasks: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for task in tasks:
            z.write(task / "statement.md", f"{task.name}/statement.md")
            for _, stdin, answer in tests_of(task):
                for path in (stdin, answer):
                    z.write(path, f"{task.name}/{path.relative_to(task)}")
    return buffer.getvalue()


def check(task: Path, python: str) -> list[str]:
    problems: list[str] = []
    limit = time_limit((task / "statement.md").read_text(encoding="utf-8"))
    tests = tests_of(task)
    print(f"\n{task.name}: тестов {len(tests)}, лимит {limit:g} с")

    worst = 0.0
    brute_checked = brute_skipped = 0
    for name, stdin_path, answer_path in tests:
        stdin = stdin_path.read_text(encoding="utf-8")
        expected = answer_path.read_text(encoding="utf-8")
        for path in (stdin_path, answer_path):
            if path.stat().st_size >= LIMIT_BYTES:
                problems.append(f"{task.name}/{name}: {path.name} тяжелее 256 КБ")
        out, seconds, err = run(python, task / "solution.py", stdin, timeout=limit * 10)
        worst = max(worst, seconds)
        if not same(out, expected):
            problems.append(f"{task.name}/{name}: эталон ответил не то {err}".strip())
        brute = task / "brute.py"
        if brute.exists() and len(stdin) <= BRUTE_MAX_INPUT:
            # Короткий ввод ещё не значит маленькую задачу: при k = 10⁹ перебор
            # не дождётся. Не успел — тест пропускаем, а не объявляем ошибкой.
            out, _, err = run(python, brute, stdin, timeout=20)
            if out is None:
                brute_skipped += 1
            elif not same(out, expected):
                problems.append(f"{task.name}/{name}: перебор не согласен с ответом {err}".strip())
            else:
                brute_checked += 1
    if (task / "brute.py").exists():
        skipped = f", не дождались {brute_skipped}" if brute_skipped else ""
        print(f"  перебор: сверено {brute_checked} тестов{skipped}")
        if not brute_checked:
            problems.append(f"{task.name}: перебор не сверил ни одного теста — добавь маленькие")
    note = "" if worst * HEADROOM <= limit else f"  ← запас меньше {HEADROOM}×"
    print(f"  эталон: худший тест {worst:.2f} с{note}")
    if note:
        problems.append(
            f"{task.name}: эталону мало запаса по времени ({worst:.2f} с при лимите {limit:g} с)"
        )

    for wrong in sorted((task / "wrong").glob("*.py")):
        failed = []
        for name, stdin_path, answer_path in tests:
            stdin = stdin_path.read_text(encoding="utf-8")
            out, _, _ = run(python, wrong, stdin, timeout=limit * 3)
            if not same(out, answer_path.read_text(encoding="utf-8")):
                failed.append(name)
        where = f" ({', '.join(failed[:5])})" if failed else ""
        print(f"  неверное {wrong.name}: падает на {len(failed)} тестах{where}")
        if not failed:
            problems.append(f"{task.name}: тесты не ловят {wrong.name}")

    for slow in sorted((task / "slow").glob("*.py")):
        biggest = max(tests, key=lambda t: t[1].stat().st_size)
        _, seconds, _ = run(python, slow, biggest[1].read_text(encoding="utf-8"), timeout=limit * 4)
        print(f"  медленное {slow.name} на {biggest[0]}: {seconds:.1f} с")
        if seconds <= limit * 1.5:
            problems.append(
                f"{task.name}: медленное {slow.name} укладывается почти в лимит ({seconds:.1f} с)"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("folder", type=Path)
    parser.add_argument("--zip", type=Path, help="куда сложить архив для загрузки")
    parser.add_argument("--python", default=sys.executable,
                        help="чем запускать решения; лучше той же версией, что на судье")
    args = parser.parse_args()

    tasks = sorted(p for p in args.folder.iterdir() if (p / "statement.md").is_file())
    if not tasks:
        print(f"в {args.folder} нет папок с statement.md", file=sys.stderr)
        return 2

    problems: list[str] = []
    data = archive(tasks)
    # Те же правила, что у кнопки «Загрузить набор»: открытый тест, пары .in/.out, размеры.
    os.environ.setdefault("ALLOW_INSECURE_SECRET", "true")
    sys.path.insert(0, str(Path.cwd()))
    try:
        from app.services.tasks import ArchiveError, parse_archive
    except ImportError:
        print(
            "запускай из корня репозитория портала: правила архива берутся из его кода",
            file=sys.stderr,
        )
        return 2
    try:
        parsed = parse_archive(data)
        summary = ", ".join(f"{t.slug} ({len(t.tests)})" for t in parsed)
        print(f"архив по правилам портала: {summary}")
    except ArchiveError as exc:
        problems.append(f"портал не примет архив: {exc}")

    for task in tasks:
        if not (task / "solution.py").exists():
            problems.append(f"{task.name}: нет solution.py — ответам не на что опереться")
            continue
        problems += check(task, args.python)

    if args.zip and not problems:
        args.zip.write_bytes(data)
        print(f"\nархив: {args.zip} ({len(data) // 1024} КБ)")
    print("\n" + ("всё в порядке" if not problems else "проблемы:\n- " + "\n- ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
