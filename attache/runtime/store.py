"""What the tower took in, kept on disk: sqlite, stdlib only, one file.

The ledger is the record of decisions. This is the record of intake — every item that
arrived (from where, what it said, who read it, what became of it) and every rule that
came out of it (a weather hold, an incident circle, a restriction; from when, until when,
who lifted it). It exists so that a restart does not make the runtime read a searched page
or a typed line twice, and so that the report can list what the tower saw without parsing
the ledger for it. Nothing here judges: rows go in after code has decided.

It is best-effort. A broken or locked file must not stop the runtime or turn /state into a
500: a file that cannot be opened leaves the store in memory for the run, and a call that fails
returns an empty answer and leaves the file alone for a while. The runtime ledgers each change.
"""

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_PATH = ".run/intake.sqlite"
MEMORY = ":memory:"
# 재시작을 넘어 '본 것' 으로 치는 출처. 검색 결과와 손으로 넣은 문장은 한 번 읽으면 끝입니다 —
# 재시작 뒤에 같은 페이지가 다시 사람 카드로 올라오면 안 됩니다. 시뮬레이터 공지(판마다 같은 id 로
# 다시 옴)와 METAR(지금 유효한 관측은 판마다 다시 걸려야 함)는 여기 안 듭니다.
DEDUPE_SOURCES = frozenset({"tavily", "manual"})
# 사람을 기다리던 항목의 결과. 카드는 프로세스와 함께 사라지므로, 시작할 때 이 줄들을 다시
# 읽게 돌려놓습니다(reopen_waiting). 안 그러면 사람이 한 번도 못 본 채 '본 것' 으로 남아 영영
# 안 읽힙니다. 사람이 답하거나 창·판이 끝나면 decide_item 이 이 값을 그 끝으로 바꿉니다.
WAITING = "held"
# 한 번의 읽기·쓰기가 잠긴 파일을 기다리는 최대 시간(초). 세계 스레드가 부르므로 짧게 — sqlite
# 기본값(5초)이면 다른 연결이 파일을 쥔 동안 폴링마다 5초씩 멈췄습니다.
BUSY_TIMEOUT_S = 0.2
# 호출이 실패한 뒤 파일을 다시 건드리기까지(초). 그 사이의 호출은 파일을 기다리지 않고 곧장
# 돌아갑니다 — 잠긴 파일이 한 폴링에 여러 번 0.2초씩 쌓이지 않게.
RETRY_AFTER_S = 30.0

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        kind TEXT,
        text TEXT NOT NULL,
        fetched_tick INTEGER,
        url TEXT,
        read_by TEXT,
        outcome TEXT,
        hints TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        from_tick INTEGER,
        until_tick INTEGER,
        applied INTEGER NOT NULL DEFAULT 0,
        lifted_by TEXT
    )""",
)
ITEM_COLUMNS = ("id", "source", "kind", "text", "fetched_tick", "url", "read_by", "outcome")
RULE_COLUMNS = ("id", "item_id", "kind", "from_tick", "until_tick", "applied", "lifted_by")
WAITING_COLUMNS = ("id", "source", "kind", "text", "url", "hints")
# 보고서에 싣는 최대 줄 수. 원장처럼 자라는 표라 마지막 것만.
REPORT_ROWS = 200


class IntakeStore:
    """연결 하나, 잠금 하나. 세계 스레드가 쓰고 보고서(HTTP 스레드)가 읽습니다.

    최선을 다할 뿐입니다. 이 표는 판정이 아니라 기록이라, 파일이 깨졌거나 잠겼다고 런타임이 서거나
    /state 가 500 이 되면 안 됩니다. 열지 못하면 이번 실행은 메모리로 돌고(open_error — 파일은
    다음 시작 때 다시 봅니다), 돌다가 한 호출이 실패하면 그 호출은 빈 답을 돌려주고 RETRY_AFTER_S
    동안 파일을 쉬게 둡니다(error — 다음 호출이 성공하면 비웁니다). 원장에 적는 것은 런타임입니다.
    """

    def __init__(self, path: str | Path | None = None):
        wanted = str(path or MEMORY)
        self.path = wanted
        self.open_error: str | None = None
        self.error: str | None = None
        self._failed_at: float | None = None
        self._lock = threading.Lock()
        try:
            self._db = _connect(wanted)
        except (sqlite3.Error, OSError) as error:
            # 깨진 파일("file is not a database")·다른 프로세스가 쥔 파일·쓸 수 없는 자리.
            # 판정은 계속되어야 합니다 — 이번 실행의 기록은 메모리에.
            self.open_error = f"{wanted}: {type(error).__name__}: {error}"
            self.path = MEMORY
            self._db = _connect(MEMORY)

    @property
    def failing(self) -> bool:
        """지금 기록이 파일에 들어가지 않고 있나(열지 못함, 또는 마지막 호출이 실패)."""
        return self.open_error is not None or self.error is not None

    def _run(self, work, default=None):
        """한 번의 읽기·쓰기. 실패하면 default 를 돌려주고 RETRY_AFTER_S 동안 파일을 쉬게 둡니다."""
        with self._lock:
            if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER_S:
                return default
            try:
                result = work(self._db)
                self._db.commit()
            except sqlite3.Error as error:
                self._failed_at, self.error = time.monotonic(), f"{type(error).__name__}: {error}"
                with contextlib.suppress(sqlite3.Error):
                    self._db.rollback()
                return default
            self._failed_at, self.error = None, None
            return result

    # ---------- 항목 ----------

    def seen(self, item_id: str, source: str) -> bool:
        """재시작 전에 읽고 끝낸 것인가. 출처가 DEDUPE_SOURCES 에 들 때만 — 나머지는 판마다
        다시 읽습니다. 받기만 하고 끝을 못 본 것(결과 없음: 읽다가 멈췄거나, 사람을 기다리다
        카드를 잃어 reopen_waiting 이 되돌린 것)은 본 것이 아닙니다. 기록을 못 읽으면 본 적
        없는 것으로 — 한 번 더 읽는 쪽이 영영 안 읽는 쪽보다 낫습니다."""
        if source not in DEDUPE_SOURCES:
            return False
        row = self._run(lambda db: db.execute("SELECT outcome FROM items WHERE id = ?",
                                              (item_id,)).fetchone())
        return row is not None and row[0] is not None

    def put_item(self, item_id: str, source: str, text: str, fetched_tick: int,
                 url: str = "", kind: str | None = None, hints: dict | None = None) -> None:
        """받은 항목 한 줄. 같은 id 가 다시 오면(다음 판의 같은 공지) 틱만 새로 적습니다.

        hints 는 문장 밖의 구조화 값(주소·반경·창)입니다. 재시작 뒤에 다시 읽을 때 같이 돌려줍니다 —
        문장만 있으면 주소로 온 사고를 못 읽습니다."""
        packed = json.dumps(hints, ensure_ascii=False, sort_keys=True) if hints else None
        self._run(lambda db: db.execute(
            "INSERT INTO items (id, source, kind, text, fetched_tick, url, hints) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET fetched_tick = excluded.fetched_tick, "
            "hints = excluded.hints, read_by = NULL, outcome = NULL",
            (item_id, source, kind, text[:2000], fetched_tick, url or None, packed)))

    def settle_item(self, item_id: str, kind: str | None, read_by: str, outcome: str) -> None:
        self._run(lambda db: db.execute(
            "UPDATE items SET kind = ?, read_by = ?, outcome = ? WHERE id = ?",
            (kind, read_by or None, outcome, item_id)))

    def decide_item(self, item_id: str, outcome: str) -> None:
        """사람을 기다리던 항목의 끝: approved · refused · lapsed · round …. 기다리던 줄(held)만
        고칩니다 — 문법이 읽고 곧장 걸린 줄은 규칙이 풀려도 '읽음' 그대로입니다."""
        self._run(lambda db: db.execute(
            "UPDATE items SET outcome = ? WHERE id = ? AND outcome = ?",
            (outcome, item_id, WAITING)))

    def reopen_waiting(self) -> list[dict]:
        """시작할 때 한 번. 재시작 전에 사람을 기다리던 항목은 카드를 잃었습니다 — 돌려주어 다시
        읽게(카드를 다시 올리게) 하고, 결과를 비워 '본 것' 이 아니게 합니다. 열린 규칙 줄은 전부
        restart 로 닫습니다 — 메모리의 기상 대기·사고 구역·보류는 프로세스와 함께 사라졌습니다."""
        def work(db) -> list[dict]:
            rows = db.execute(f"SELECT {', '.join(WAITING_COLUMNS)} FROM items "
                              "WHERE outcome = ? ORDER BY rowid", (WAITING,)).fetchall()
            db.execute("UPDATE items SET outcome = NULL, read_by = NULL WHERE outcome = ?",
                       (WAITING,))
            db.execute("UPDATE rules SET lifted_by = 'restart' WHERE lifted_by IS NULL")
            return [_waiting_item(row) for row in rows]

        return self._run(work, [])

    # ---------- 규칙 ----------

    def open_rule(self, item_id: str, kind: str, from_tick: int | None,
                  until_tick: int | None, applied: bool = True) -> int | None:
        """항목에서 나온 규칙 한 줄. 돌려주는 번호로 나중에 풀린 것을 적습니다(못 적었으면 None)."""
        cursor = self._run(lambda db: db.execute(
            "INSERT INTO rules (item_id, kind, from_tick, until_tick, applied) VALUES (?,?,?,?,?)",
            (item_id, kind, from_tick, until_tick, int(applied))))
        return int(cursor.lastrowid) if cursor is not None else None

    def close_rule(self, rule_id: int | None, lifted_by: str,
                   until_tick: int | None = None) -> None:
        """규칙이 끝났습니다 — 사람이 풀었거나(human), 창이 닫혔거나(window), 판이
        바뀌었거나(round), 사람이 거부했거나(refused), 확인 전에 지나갔거나(lapsed),
        프로세스가 다시 시작했습니다(restart)."""
        if rule_id is None:
            return
        if until_tick is None:
            self._run(lambda db: db.execute("UPDATE rules SET lifted_by = ? WHERE id = ?",
                                            (lifted_by, rule_id)))
        else:
            self._run(lambda db: db.execute(
                "UPDATE rules SET lifted_by = ?, until_tick = ? WHERE id = ?",
                (lifted_by, until_tick, rule_id)))

    def apply_rule(self, rule_id: int | None, from_tick: int | None = None) -> None:
        """사람이 확인해 보류였던 규칙이 걸렸습니다(applied 0 → 1)."""
        if rule_id is None:
            return
        if from_tick is None:
            self._run(lambda db: db.execute("UPDATE rules SET applied = 1 WHERE id = ?",
                                            (rule_id,)))
        else:
            self._run(lambda db: db.execute(
                "UPDATE rules SET applied = 1, from_tick = ? WHERE id = ?", (from_tick, rule_id)))

    def extend_rule(self, rule_id: int | None, until_tick: int) -> None:
        if rule_id is None:
            return
        self._run(lambda db: db.execute("UPDATE rules SET until_tick = ? WHERE id = ?",
                                        (until_tick, rule_id)))

    # ---------- 읽기 ----------

    def items(self, limit: int = REPORT_ROWS) -> list[dict]:
        # 열 이름을 적어서 읽습니다. hints 열을 나중에 붙인 파일은 열 순서가 다릅니다.
        rows = self._run(lambda db: db.execute(
            f"SELECT {', '.join(ITEM_COLUMNS)} FROM items "
            "ORDER BY fetched_tick DESC, rowid DESC LIMIT ?", (limit,)).fetchall(), [])
        return [dict(zip(ITEM_COLUMNS, tuple(row), strict=True)) for row in reversed(rows)]

    def rules(self, limit: int = REPORT_ROWS) -> list[dict]:
        rows = self._run(lambda db: db.execute("SELECT * FROM rules ORDER BY id DESC LIMIT ?",
                                               (limit,)).fetchall(), [])
        out = []
        for row in reversed(rows):
            record = dict(zip(RULE_COLUMNS, tuple(row), strict=True))
            record["applied"] = bool(record["applied"])
            out.append(record)
        return out

    def counts(self) -> dict:
        """화면(/state.intake.store)과 보고서의 머리. 셀 수 없으면 None, 실패 이유는 error 에."""
        counted = self._run(lambda db: (db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                                        db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]))
        items, rules = counted if counted is not None else (None, None)
        return {"items": None if items is None else int(items),
                "rules": None if rules is None else int(rules), "path": self.path,
                "error": self.open_error or self.error}

    def report(self, limit: int = REPORT_ROWS) -> dict:
        """보고서(/ledger/report)에 싣는 접수 기록. 원장이 결정의 기록이면 이것은 들어온 것의
        기록입니다."""
        return {**self.counts(), "items": self.items(limit), "rules": self.rules(limit)}

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _waiting_item(row) -> dict:
    """다시 읽을 항목 하나. 힌트를 펼치고, 다시 올린 것임을 적습니다(원장의 받음 줄이 말하게)."""
    record = dict(zip(WAITING_COLUMNS, tuple(row), strict=True))
    try:
        hints = json.loads(record.pop("hints") or "{}")
    except ValueError:
        hints = {}
    item = {key: value for key, value in record.items() if value is not None}
    return {**(hints if isinstance(hints, dict) else {}), **item, "reopened": True}


def _connect(path: str) -> sqlite3.Connection:
    """연결을 열고 표를 만듭니다. 깨진 파일은 여기서(첫 문장에서) 드러납니다."""
    if path != MEMORY:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # 스레드마다 연결을 열지 않습니다 — 메모리 DB 는 연결마다 딴 DB 가 됩니다.
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, check_same_thread=False)
    db.row_factory = sqlite3.Row
    try:
        for statement in SCHEMA:
            db.execute(statement)
        # hints 열이 생기기 전의 파일. 표를 다시 만들지 않고 열만 붙입니다(옛 줄의 힌트는 빔).
        if "hints" not in {row[1] for row in db.execute("PRAGMA table_info(items)")}:
            db.execute("ALTER TABLE items ADD COLUMN hints TEXT")
        db.commit()
    except sqlite3.Error:
        db.close()
        raise
    return db
