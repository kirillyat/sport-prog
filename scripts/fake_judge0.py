#!/usr/bin/env python3
"""Поддельный judge0 для проверки портала у себя — без Docker и без судьи.

Правда запускает присланный код и сравнивает вывод с ответом, поэтому на нём
видно и «Принято», и «Неверный ответ», и ошибку компиляции. Отвечает на то,
о чём спрашивает портал: `/about`, `/languages`, `POST /submissions`.

Языки: Python — тем же интерпретатором, что запустил скрипт; C и C++ — если
в системе есть `cc` и `c++`. Остальные языки из списка честно отвечают
«не поддерживается поддельным судьёй».

**Только для своей машины.** Скрипт исполняет любой присланный код без
песочницы, поэтому слушает только 127.0.0.1.

Запуск:  python scripts/fake_judge0.py [--port 2358]
В портале: «Интеграции» → адрес http://127.0.0.1:2358 → «Подключить».
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Номера — как в стандартной сборке judge0 1.13, чтобы портал вёл себя так же.
LANGUAGES = {
    71: "Python (3.8.1)",
    54: "C++ (GCC 9.2.0)",
    50: "C (GCC 9.2.0)",
    62: "Java (OpenJDK 13.0.1)",
}

ACCEPTED, WRONG, TIME_LIMIT, COMPILE_ERROR, RUNTIME_ERROR, INTERNAL = 3, 4, 5, 6, 11, 13
DESCRIPTIONS = {
    ACCEPTED: "Accepted",
    WRONG: "Wrong Answer",
    TIME_LIMIT: "Time Limit Exceeded",
    COMPILE_ERROR: "Compilation Error",
    RUNTIME_ERROR: "Runtime Error (NZEC)",
    INTERNAL: "Internal Error",
}


def _verdict(status: int, **extra) -> dict:
    return {"status": {"id": status, "description": DESCRIPTIONS[status]}, "time": "0.01",
            "memory": 1024, **extra}


def _command(language_id: int, source: str, workdir: Path) -> tuple[list[str] | None, dict | None]:
    """Команда запуска; для C и C++ — после компиляции. Второе — отказ, если не вышло."""
    if language_id == 71:
        path = workdir / "main.py"
        path.write_text(source)
        return [sys.executable, str(path)], None
    if language_id in (50, 54):
        compiler = shutil.which("c++" if language_id == 54 else "cc")
        if compiler is None:
            return None, _verdict(INTERNAL, stderr="компилятора нет в системе")
        path = workdir / ("main.cpp" if language_id == 54 else "main.c")
        path.write_text(source)
        binary = workdir / "main"
        built = subprocess.run([compiler, "-O2", "-o", str(binary), str(path)],
                               capture_output=True, text=True)
        if built.returncode != 0:
            return None, _verdict(COMPILE_ERROR, compile_output=built.stderr[-2000:])
        return [str(binary)], None
    return None, _verdict(INTERNAL, stderr="язык не поддерживается поддельным судьёй")


def judge(payload: dict) -> dict:
    language_id = int(payload.get("language_id") or 71)
    seconds = float(payload.get("wall_time_limit") or payload.get("cpu_time_limit") or 5)
    with tempfile.TemporaryDirectory() as raw:
        command, refused = _command(language_id, payload.get("source_code") or "", Path(raw))
        if refused:
            return refused
        try:
            run = subprocess.run(command, input=payload.get("stdin") or "", capture_output=True,
                                 text=True, timeout=seconds, cwd=raw)
        except subprocess.TimeoutExpired:
            return _verdict(TIME_LIMIT)
    if run.returncode != 0:
        return _verdict(RUNTIME_ERROR, stdout=run.stdout, stderr=run.stderr[-2000:])
    # Как у judge0: лишние пробелы в концах строк и пустые строки в конце не в счёт.
    def norm(text: str) -> str:
        return "\n".join(line.rstrip() for line in text.rstrip().splitlines())

    expected = payload.get("expected_output")
    status = ACCEPTED if expected is None or norm(run.stdout) == norm(expected) else WRONG
    return _verdict(status, stdout=run.stdout, stderr=run.stderr[-2000:] or None)


class Handler(BaseHTTPRequestHandler):
    def _send(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
        if self.path.startswith("/about"):
            self._send({"version": "1.13.1-fake"})
        elif self.path.startswith("/languages"):
            self._send([{"id": key, "name": name} for key, name in LANGUAGES.items()])
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/submissions"):
            self._send({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        self._send(judge(json.loads(self.rfile.read(length) or b"{}")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=2358)
    args = parser.parse_args()
    print(f"поддельный judge0 на http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
