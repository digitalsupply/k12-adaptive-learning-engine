"""精选题库：AI 题教师审定入库与优先复用（审核工作台的数据层）。

流程：教师（班级创建者）在审题台抽题预览 → 「通过」按 qhash 入
精选题库；后续出题在出题缓存未命中时**优先复用精选题**，再把该题
按当前 seed 写回缓存与答案库——判分重建(qhash 核对)依然成立。

设计要点：
- 一张表管两种决定：status = approved / rejected；qhash 是主键，
  同一道题（kp+seed+options 都同）重复审只更新状态，不产生重复行。
- payload 存题目全部判分字段（含 answer_index 仅服务端使用），重建、
  复用都靠它；面向学生的下发永远不带 answer_index。
- 与 `ale/review.py`（SM-2 复习调度）互不相干：这里是题目质量审定。
"""

from __future__ import annotations

import json
import sqlite3
import time

from .quiz import Question

_FIELDS = ("kp_id", "kp_name", "kind", "prompt", "options",
           "answer_index", "explain", "objective", "misconception", "model")


def _from_payload(raw: dict, seed: int, source: str = "curated") -> Question:
    """payload → 题目。seed 可改写：精选题被复用到新 seed 时 qhash 随之
    重算（qhash 绑定 kp|seed|options），答案库会在下发时补一条对应记录。"""
    return Question(
        kp_id=raw["kp_id"], kp_name=raw["kp_name"], seed=seed,
        kind=raw["kind"], prompt=raw["prompt"], options=list(raw["options"]),
        answer_index=raw["answer_index"], explain=raw["explain"],
        objective=raw.get("objective"), misconception=raw.get("misconception"),
        source=source, model=raw.get("model", ""))


class CuratedStore:
    """curated_questions 表的访问层。"""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _init_db(self) -> None:
        with self._conn() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS curated_questions(
                    qhash       TEXT PRIMARY KEY,
                    kp_id       TEXT NOT NULL,
                    kp_name     TEXT NOT NULL,
                    payload     TEXT NOT NULL,     -- Question 判分字段 JSON
                    model       TEXT,
                    origin_seed INTEGER,
                    status      TEXT NOT NULL,     -- approved | rejected
                    reviewer    TEXT,
                    note        TEXT,
                    ts          REAL
                );
                CREATE INDEX IF NOT EXISTS idx_curated_kp
                    ON curated_questions(kp_id, status);
            """)

    # ---------------- 审定 ----------------

    def upsert(self, q: Question, status: str, reviewer: str,
               note: str = "") -> dict:
        """登记一次审定/修订。同一题重复审 → 覆盖状态与批注。

        status: approved（直接入库并参与出题复用）/ pending（学生等
        非教师提交的修订，待教师审）/ rejected（驳回或下架）。
        """
        if status not in ("approved", "rejected", "pending"):
            raise ValueError(f"未知的审定状态：{status}")
        payload = {f: getattr(q, f) for f in _FIELDS}
        with self._conn() as con:
            con.execute(
                "INSERT INTO curated_questions(qhash, kp_id, kp_name, payload,"
                " model, origin_seed, status, reviewer, note, ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(qhash) DO UPDATE SET status=excluded.status,"
                " reviewer=excluded.reviewer, note=excluded.note,"
                " ts=excluded.ts",
                (q.qhash, q.kp_id, q.kp_name,
                 json.dumps(payload, ensure_ascii=False), q.model, q.seed,
                 status, reviewer, note, time.time()))
        return {"qhash": q.qhash, "status": status,
                "kp_id": q.kp_id, "kp_name": q.kp_name}

    def get(self, qhash: str) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT qhash, kp_id, kp_name, status, reviewer, note, ts"
                " FROM curated_questions WHERE qhash=?", (qhash,)).fetchone()
        if not row:
            return None
        return {"qhash": row[0], "kp_id": row[1], "kp_name": row[2],
                "status": row[3], "reviewer": row[4], "note": row[5],
                "ts": row[6]}

    # ---------------- 复用 ----------------

    def _payloads(self, kp_id: str) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT payload FROM curated_questions"
                " WHERE kp_id=? AND status='approved' ORDER BY ts",
                (kp_id,)).fetchall()
        out = []
        for (raw,) in rows:
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue          # 一条坏数据不拖垮整个池子
        return out

    def pool(self, kp_id: str) -> list[Question]:
        """某知识点全部精选通过的题（按审定时间先后），seed 置 0 占位。"""
        return [_from_payload(raw, seed=0) for raw in self._payloads(kp_id)]

    def serve_at(self, kp_id: str, seed: int) -> Question | None:
        """为 (kp_id, seed) 挑一道精选题：确定性取 seed % 池子大小，
        池子骨架稳定，但新审批一入库即参与轮换。未命中返回 None。"""
        rows = self._payloads(kp_id)
        if not rows:
            return None
        return _from_payload(rows[seed % len(rows)], seed=seed)

    def counts(self, kp_id: str | None = None) -> dict:
        q = "SELECT status, COUNT(*) FROM curated_questions"
        args: tuple = ()
        if kp_id:
            q += " WHERE kp_id=?"
            args = (kp_id,)
        q += " GROUP BY status"
        with self._conn() as con:
            got = dict(con.execute(q, args).fetchall())
        return {"approved": got.get("approved", 0),
                "pending": got.get("pending", 0),
                "rejected": got.get("rejected", 0)}

    def recent(self, status: str | None = None, kp_id: str | None = None,
               limit: int = 50) -> list[dict]:
        """审定记录（教师面板用，含答案字段——仅面向教师端点）。"""
        q = ("SELECT qhash, kp_id, kp_name, payload, model, origin_seed,"
             " status, reviewer, note, ts FROM curated_questions")
        conds, args = [], []
        if status:
            conds.append("status=?")
            args.append(status)
        if kp_id:
            conds.append("kp_id=?")
            args.append(kp_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        out = []
        with self._conn() as con:
            for r in con.execute(q, args).fetchall():
                qh, kid, kname, payload, model, oseed, st, rv, note, ts = r
                try:
                    p = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                out.append({"qhash": qh, "kp_id": kid, "kp_name": kname,
                            "origin_seed": oseed, "status": st,
                            "reviewer": rv, "note": note, "ts": ts,
                            "model": model,
                            "prompt": p.get("prompt", ""),
                            "options": p.get("options", []),
                            "answer_index": p.get("answer_index"),
                            "explain": p.get("explain", "")})
        return out

    def to_question(self, qhash: str, seed: int | None = None) -> Question | None:
        """按 qhash 取回存的题（含答案）。seed=None 用入库时的原 seed。"""
        with self._conn() as con:
            row = con.execute(
                "SELECT payload, origin_seed FROM curated_questions"
                " WHERE qhash=?", (qhash,)).fetchone()
        if not row:
            return None
        try:
            raw = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return _from_payload(raw, seed=row[1] if seed is None else seed)

    def unapprove(self, qhash: str, reviewer: str) -> dict | None:
        """下架：精选题不再参与出题复用（保留记录，状态改 rejected）。"""
        row = self.get(qhash)
        if row is None or row["status"] != "approved":
            return None
        with self._conn() as con:
            con.execute(
                "UPDATE curated_questions SET status='rejected', reviewer=?,"
                " note=? WHERE qhash=?",
                (reviewer, (row["note"] + " ｜已下架").strip("｜ "), qhash))
        return self.get(qhash)
