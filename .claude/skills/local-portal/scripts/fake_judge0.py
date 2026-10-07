#!/usr/bin/env python3
"""Поддельный judge0 для локальной проверки страницы задачи.

Настоящий судья — отдельная машина, и с ноутбука до неё обычно не достать.
Этот отвечает на те же два запроса, что делает портал (`GET /about` и
синхронный `POST /submissions`), и правда запускает присланный код текущим
python. Сравнение ответа упрощённое: по строкам без хвостовых пробелов.

    python fake_judge0.py [порт]      # по умолчанию 2358

Только для своей машины: код из запроса исполняется без песочницы.
"""
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ACCEPTED, WRONG, TIME_LIMIT, RUNTIME_ERROR = 3, 4, 5, 11


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in (text or "").rstrip().splitlines()]


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 — имя задаёт http.server
        self._send({"version": "fake-local"})

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        limit = float(payload.get("cpu_time_limit") or 2)
        try:
            run = subprocess.run(
                [sys.executable, "-c", payload["source_code"]],
                input=payload.get("stdin") or "",
                capture_output=True, text=True, timeout=limit + 1,
            )
        except subprocess.TimeoutExpired:
            return self._send({"status": {"id": TIME_LIMIT, "description": "Time Limit Exceeded"},
                               "time": str(limit)})
        if run.returncode:
            status = {"id": RUNTIME_ERROR, "description": "Runtime Error (NZEC)"}
        elif _lines(run.stdout) == _lines(payload.get("expected_output")):
            status = {"id": ACCEPTED, "description": "Accepted"}
        else:
            status = {"id": WRONG, "description": "Wrong Answer"}
        self._send({"status": status, "stdout": run.stdout, "stderr": run.stderr, "time": "0.02"})

    def log_message(self, *args):  # тишина в консоли сервера разработки
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2358
    print(f"поддельный judge0 на http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
