"""Health-контракт движковых сервисов: /healthz (жив) + /readyz (зависимости достижимы).
Stdlib http.server, чтобы не тянуть web-фреймворк в каркас."""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Awaitable, Callable


def serve(port: int, readiness: Callable[[], Awaitable[bool]]) -> threading.Thread:
    loop = asyncio.new_event_loop()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._send(200, b"ok")
            elif self.path == "/readyz":
                ok = asyncio.run_coroutine_threadsafe(readiness(), loop).result(timeout=5)
                self._send(200 if ok else 503, b"ready" if ok else b"not-ready")
            else:
                self._send(404, b"not-found")

        def _send(self, code: int, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):  # тихо
            return

    threading.Thread(target=loop.run_forever, daemon=True).start()
    srv = HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return t
