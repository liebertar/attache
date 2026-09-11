"""화면 정적 서버. 캐시를 끕니다.

python3 -m http.server 는 Cache-Control 을 안 보내서 브라우저가 옛 map.html 을 며칠씩 들고
있었습니다. 바뀐 화면을 보려고 매번 강제 새로고침을 시키는 대신, 아예 저장하지 말라고 합니다.
"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — 상위 시그니처
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3100
    directory = sys.argv[2] if len(sys.argv) > 2 else "."
    handler = partial(NoCacheHandler, directory=directory)
    ThreadingHTTPServer(("", port), handler).serve_forever()


if __name__ == "__main__":
    main()
