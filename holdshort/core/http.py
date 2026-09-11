"""Stdlib-only JSON HTTP. No web framework: the runtime's guarantees should not depend on one."""

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

Route = tuple[str, re.Pattern, callable]


def route(method: str, pattern: str) -> tuple:
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
    return method.upper(), regex


class _QuietServer(ThreadingHTTPServer):
    """브라우저가 요청 도중 연결을 끊으면(새로고침·탭 닫기) 받을 상대가 없을 뿐 오류가 아닙니다.
    그 경우만 조용히 넘기고, 나머지 오류는 표준 처리(트레이스백)대로 둡니다."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class JsonServer:
    def __init__(self, port: int):
        self.port = port
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler) -> None:
        verb, regex = route(method, pattern)
        self._routes.append((verb, regex, handler))

    def _dispatch(self, verb: str, path: str, query: dict, body: dict):
        for method, regex, handler in self._routes:
            match = regex.match(path)
            if match and method == verb:
                return handler(body=body, query=query, **match.groupdict())
        return 404, {"error": "no such route", "path": path}

    def serve_forever(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 데모 로그를 어지럽히지 않습니다
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _respond(self, verb: str):
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                try:
                    status, payload = server._dispatch(verb, parsed.path, query, body)
                except Exception as exc:  # noqa: BLE001 - 데모 서버는 죽지 않는 편이 낫습니다
                    status, payload = 500, {"error": str(exc)}
                # 문자열 본문은 그대로 보냅니다(마크다운 보고서). 나머지는 전부 JSON 입니다.
                if isinstance(payload, str):
                    encoded, content_type = payload.encode(), "text/markdown; charset=utf-8"
                else:
                    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode()
                    content_type = "application/json; charset=utf-8"
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    # 화면이 폴링 도중 탭을 닫거나 새로고침하면 답을 받을 상대가 없습니다. 스레드
                    # 서버가
                    # 스택 트레이스를 찍어 로그를 더럽히던 것이라 조용히 접습니다. 판정과는
                    # 무관합니다.
                    return

            def do_GET(self):
                self._respond("GET")

            def do_POST(self):
                self._respond("POST")

        _QuietServer(("0.0.0.0", self.port), Handler).serve_forever()

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread


def get_json(url: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def post_json(url: str, payload: dict, timeout: float = 20.0, headers: dict | None = None):
    return post_json_status(url, payload, timeout=timeout, headers=headers)[1]


def post_json_status(url: str, payload: dict, timeout: float = 20.0,
                     headers: dict | None = None) -> tuple[int, dict | None]:
    """(HTTP 상태, 본문). 닿지 못했으면 (0, None).

    모델 서버가 어떤 인자를 거절했는지(400)와 그냥 느린 것(타임아웃)은 다르게 다뤄야 합니다.
    전자는 그 인자를 빼고 한 번 더 내면 되고, 후자는 다시 내봐야 또 기다리기만 합니다.
    None 하나로 뭉치면 둘을 구분할 수 없어서 상태 코드를 같이 돌려줍니다.
    """
    data = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return 0, None
