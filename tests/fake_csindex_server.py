"""A deterministic loopback-only CSIndex HTTP server for integration tests.

It deliberately speaks the real endpoints and JSON shapes consumed by
``CsindexClient``.  Scenario actions are queued per endpoint/code, which keeps
network error classification tests independent from crawler policy tests.
"""

from __future__ import annotations

from collections import defaultdict, deque
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import time
from typing import Deque, Iterable
from urllib.parse import unquote, urlparse


TARGET_DATE = "2026-09-03"
_LIST_PATH = "/csindex-home/index-list/query-index-item"
_YIELD_PREFIX = "/csindex-home/perf/get-index-yield-item/"
_VOLATILITY_PREFIX = "/csindex-home/perf/get-index-yield-item-nianHua/"


class FakeCsindexServer(AbstractContextManager["FakeCsindexServer"]):
    """Own a random-port 127.0.0.1 server and expose request observations."""

    def __init__(self, *, index_count: int = 20) -> None:
        self._indices: list[str] = []
        self.set_indices(f"{number:06d}" for number in range(1, index_count + 1))
        self._plans: dict[tuple[str, str], Deque[object]] = defaultdict(deque)
        self.detail_request_counts: defaultdict[str, int] = defaultdict(int)
        self.request_paths: list[str] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("fake server has not started")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/csindex-home"

    def set_indices(self, codes: Iterable[str]) -> None:
        self._indices = list(codes)
        # The production coordinator always probes the canonical CSI 300 code
        # before constructing a run, and the database enforces index foreign
        # keys. Keep that real production invariant in every fake catalogue.
        if "000300" not in self._indices:
            self._indices.append("000300")

    def plan(self, endpoint: str, code: str, *actions: object) -> None:
        if endpoint not in {"yield", "volatility"}:
            raise ValueError(f"unknown endpoint: {endpoint}")
        self._plans[(endpoint, code)].extend(actions)

    def __enter__(self) -> "FakeCsindexServer":
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - HTTP handler API
                parent._handle(self)

            def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
                parent._handle(self)

        class LoopbackServer(ThreadingHTTPServer):
            # ``server_close`` waits for the deliberately slow timeout handler
            # too, so a failed test cannot leave a worker behind.
            daemon_threads = False
            block_on_close = True
            allow_reuse_address = True

        self._httpd = LoopbackServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("fake CSIndex server did not stop")
        self._httpd = None
        self._thread = None

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        path = urlparse(handler.path).path
        self.request_paths.append(path)
        if path == _LIST_PATH:
            self._json(handler, 200, self._index_list_payload(handler))
            return
        if path.startswith(_YIELD_PREFIX):
            self._detail(handler, "yield", unquote(path.removeprefix(_YIELD_PREFIX)))
            return
        if path.startswith(_VOLATILITY_PREFIX):
            self._detail(
                handler, "volatility", unquote(path.removeprefix(_VOLATILITY_PREFIX))
            )
            return
        self._json(handler, 404, {"message": "not found"})

    def _index_list_payload(self, handler: BaseHTTPRequestHandler) -> dict:
        # The client sends pageSize=100. Supporting pageNum makes failures
        # meaningful even if a future integration scenario grows past 100 rows.
        body_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(body_length) if body_length else b"{}"
        try:
            page_num = int(json.loads(body).get("pager", {}).get("pageNum", 1))
        except (TypeError, ValueError, json.JSONDecodeError):
            page_num = 1
        start = max(page_num - 1, 0) * 100
        return {
            "total": len(self._indices),
            "data": [
                {
                    "indexCode": code,
                    "indexName": f"测试指数{code}",
                    "ifTracked": "是",
                    "indexSeries": "规模指数",
                }
                for code in self._indices[start : start + 100]
            ],
        }

    def _detail(self, handler: BaseHTTPRequestHandler, endpoint: str, code: str) -> None:
        key = f"{endpoint}:{code}"
        self.detail_request_counts[key] += 1
        actions = self._plans[(endpoint, code)]
        action = actions.popleft() if actions else "ok"
        if action == "waf_403":
            self._bytes(handler, 403, "您的访问被阻断，WAF".encode("utf-8"), "text/html")
            return
        if action == "business_404":
            self._json(handler, 404, {"message": "index does not exist"})
            return
        if action == "bad_json":
            self._bytes(handler, 200, b"{not-json", "application/json")
            return
        if action == "timeout":
            # The test client has a 30ms timeout. This is a local transport
            # timeout, not a production rate-limit or cooldown delay.
            time.sleep(0.08)
            self._json(handler, 200, self._detail_payload(endpoint, code, TARGET_DATE))
            return
        if isinstance(action, tuple) and action[0] == "yield_date":
            if endpoint != "yield" or not isinstance(action[1], str):
                raise AssertionError("yield_date can only configure a yield response")
            self._json(handler, 200, self._detail_payload(endpoint, code, action[1]))
            return
        if action != "ok":
            raise AssertionError(f"unknown fake-server action: {action!r}")
        self._json(handler, 200, self._detail_payload(endpoint, code, TARGET_DATE))

    @staticmethod
    def _detail_payload(endpoint: str, code: str, data_date: str) -> dict:
        if endpoint == "yield":
            return {
                "code": "200",
                "data": {
                    "indexCode": code,
                    "endDate": data_date,
                    "oneMonth": "1.10",
                    "threeMonth": "2.20",
                    "thisYear": "3.30",
                    "oneYear": "4.40",
                    "threeYear": "5.50",
                    "fiveYear": "6.60",
                },
            }
        return {
            "code": "200",
            "data": {
                "oneYearNianHua": "7.70",
                "threeYearNianHua": "8.80",
                "fiveYearNianHua": "9.90",
            },
        }

    @staticmethod
    def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        FakeCsindexServer._bytes(
            handler,
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    @staticmethod
    def _bytes(
        handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str
    ) -> None:
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Expected after the local client deliberately times out.
            return
