"""学生档案的 SQLite 存储：与 state.StudentStore 同一套方法签名。

为什么换库：JSON 文件版每个学生一个文件，列表/归属要靠扫目录，跨进程
并发只有 flock；账号体系需要「按用户过滤、复合主键、迁移能力」，SQLite
（WAL 模式）都给得了，而且仍是单文件、零部署。

接口约定（与文件版一致）：
    create(name, model, owner_id=None, is_demo=False) -> Student
    get(sid) -> Student                     # 不存在抛 KeyError
    save(stu)                               # 原子写
    mutate(sid, fn) -> (fn结果, Student)     # 锁内读改写
    list_all(owner_id=None) -> [摘要]
    delete(sid)

学生 **状态本体**（掌握度/作答史/复习队列）以 JSON 文本存在 state 列里，
结构与文件版完全相同——导入导出与回退到文件版都是直接搬 JSON。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .state import Attempt, MasteryState, ReviewItem, Student

_SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT 'bkt',
    created_ts REAL,
    owner_id   TEXT,               -- NULL = 演示/未归属
    is_demo    INTEGER NOT NULL DEFAULT 0,
    state      TEXT NOT NULL,      -- 学生状态 JSON（同文件版 payload）
    updated_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_students_owner ON students(owner_id);
"""


def _decode(state_json: str) -> Student:
    raw = json.loads(state_json)
    try:
        return Student(
            id=raw["id"], name=raw["name"], model=raw.get("model", "bkt"),
            created_ts=raw.get("created_ts", 0),
            mastery={k: MasteryState(**v) for k, v in raw.get("mastery", {}).items()},
            attempts=[Attempt(**a) for a in raw.get("attempts", [])],
            reviews={k: ReviewItem(**v) for k, v in raw.get("reviews", {}).items()},
            current_goals=raw.get("current_goals", {}),
            owner_id=raw.get("owner_id"), is_demo=bool(raw.get("is_demo")))
    except (KeyError, TypeError) as e:
        raise KeyError(f"学生档案损坏（state 列）：{e}") from e


def _encode(stu: Student) -> str:
    owner = stu.owner_id
    is_demo = stu.is_demo
    payload = {
        "id": stu.id, "name": stu.name, "model": stu.model,
        "created_ts": stu.created_ts,
        "mastery": {k: asdict(v) for k, v in stu.mastery.items()},
        "attempts": [asdict(a) for a in stu.attempts],
        "reviews": {k: asdict(v) for k, v in stu.reviews.items()},
        "current_goals": stu.current_goals,
        "owner_id": owner, "is_demo": is_demo,
    }
    return json.dumps(payload, ensure_ascii=False)


class StudentStore:
    """SQLite 后端。方法名与文件版 StudentStore 对齐，engine 无需感知差异。"""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # RLock 而非 Lock：mutate() 持锁内部还要调 save()（再进一次锁）。
        # 进程内写锁；跨进程靠 WAL + busy_timeout。
        self._lock = threading.RLock()
        with self._conn() as con:
            con.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    # ---------------- 基本读写 ----------------

    def create(self, name: str, model: str = "bkt",
               owner_id: str | None = None, is_demo: bool = False) -> Student:
        stu = Student(id=uuid.uuid4().hex[:12], name=name, model=model,
                      owner_id=owner_id, is_demo=is_demo)
        self.save(stu)
        return stu

    def get(self, sid: str) -> Student:
        with self._conn() as con:
            row = con.execute("SELECT state FROM students WHERE id=?",
                              (sid,)).fetchone()
        if row is None:
            raise KeyError(f"学生不存在：{sid}")
        return _decode(row["state"])

    def save(self, stu: Student) -> None:
        with self._lock, self._conn() as con:
            con.execute(
                "INSERT INTO students(id,name,model,created_ts,owner_id,is_demo,state,updated_ts)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, model=excluded.model,"
                " owner_id=excluded.owner_id, is_demo=excluded.is_demo,"
                " state=excluded.state, updated_ts=excluded.updated_ts",
                (stu.id, stu.name, stu.model, stu.created_ts,
                 stu.owner_id, int(stu.is_demo), _encode(stu), time.time()))

    def mutate(self, sid: str, fn):
        """锁内读改写：与文件版语义一致（fn 就地改，返回值透传）。"""
        with self._lock:
            stu = self.get(sid)
            result = fn(stu)
            self.save(stu)
        return result, stu

    def delete(self, sid: str) -> None:
        with self._lock, self._conn() as con:
            con.execute("DELETE FROM students WHERE id=?", (sid,))

    def list_all(self, owner_id: str | None = None
                 ) -> list[dict]:
        """学生列表。传 owner_id → 该用户自己的 + 演示/未归属；不传 → 全量。"""
        with self._conn() as con:
            rows = con.execute(
                "SELECT id,name,model,owner_id,is_demo,created_ts,"
                " json_array_length(state,'$.attempts') AS n_att"
                " FROM students").fetchall()
        out = []
        for r in rows:
            if owner_id and r["owner_id"] not in (owner_id, None) \
                    and not r["is_demo"]:
                continue           # 别人的正式档案不属于这里
            out.append({
                "id": r["id"], "name": r["name"], "model": r["model"],
                "owner_id": r["owner_id"], "is_demo": bool(r["is_demo"]),
                "attempt_count": r["n_att"] or 0,
            })
        return out

    # ---------------- 迁移 ----------------

    def import_legacy_dir(self, students_dir, overwrite: bool = False) -> int:
        """把 JSON 文件版档案批量搬进库（owner 视为未归属，is_demo=1）。"""
        n = 0
        for p in sorted(Path(students_dir).glob("*.json")):
            try:
                stu = _decode(p.read_text(encoding="utf-8"))
            except(KeyError, TypeError, json.JSONDecodeError, OSError):
                continue
            if not overwrite:
                with self._conn() as con:
                    if con.execute("SELECT 1 FROM students WHERE id=?",
                                   (stu.id,)).fetchone():
                        continue
            stu.owner_id = stu.owner_id if stu.owner_id else None
            stu.is_demo = True
            self.save(stu)
            n += 1
        return n
