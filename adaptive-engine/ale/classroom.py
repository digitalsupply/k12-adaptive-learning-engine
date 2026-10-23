"""班级与作业：教师建班、学生凭邀请码加入、按知识点布置作业、判分与全班矩阵。

设计要点：

- 账号即身份：建班者=教师(owner)，成员用「自己注册的那个学习档案」做题，
  掌握度/答题数据完全复用现有链路（_writable_student 不需要新逻辑）。
- 作业题目在“布置”时只记录 (kp_id, seed) 对；实际题目由服务端出题三链生成，
  seed 由作业 id + 题号确定性派生 → 全班做同一道题，成绩可直接横向比较。
- 每题判分都过 eng.answer_question（qhash 防串题、答案库兜底），
  判完把结果登记到 homework_results，教师端出 × 学生题号的矩阵视图。
- 游客不进班（会话是一次性的，数据不长期保留）；注册用户 join 后即成员。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid

from .accounts import Accounts

_JOIN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉易混的 0/O/1/I


def derive_seed(aid: str, index: int) -> int:
    """作业题号 → 确定性出题 seed（同一作业、同一题号全员同题）。"""
    return int(hashlib.sha1(f"{aid}|{index}".encode("utf-8")).hexdigest()[:8], 16) % 1_000_000


class Classroom:
    """classes / members / assignments / homework_results 四张表。"""

    def __init__(self, db_path, accounts: Accounts):
        self.db_path = str(db_path)
        self.accounts = accounts
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _init_db(self) -> None:
        with self._conn() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS classes(
                    id        TEXT PRIMARY KEY,
                    name      TEXT NOT NULL,
                    owner_uid TEXT NOT NULL,
                    join_code TEXT UNIQUE NOT NULL,
                    created_ts REAL
                );
                CREATE TABLE IF NOT EXISTS class_members(
                    class_id  TEXT NOT NULL,
                    uid       TEXT NOT NULL,
                    joined_ts REAL,
                    PRIMARY KEY(class_id, uid)
                );
                CREATE TABLE IF NOT EXISTS assignments(
                    id         TEXT PRIMARY KEY,
                    class_id   TEXT NOT NULL,
                    title      TEXT NOT NULL,
                    items      TEXT NOT NULL,      -- JSON: [{kp_id, kp_name, seed}]
                    created_ts REAL
                );
                CREATE TABLE IF NOT EXISTS homework_results(
                    assignment_id TEXT NOT NULL,
                    uid           TEXT NOT NULL,
                    item          INTEGER NOT NULL,
                    correct       INTEGER NOT NULL,
                    choice        INTEGER NOT NULL,
                    ts            REAL,
                    PRIMARY KEY(assignment_id, uid, item)
                );
            """)
            # 角色自选迁移：已创建过班级的老账号回填为教师角色
            cols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
            if "role" in cols:
                con.execute(
                    "UPDATE users SET role='teacher' WHERE role='student'"
                    " AND id IN (SELECT owner_uid FROM classes)")

    # ---------------- 建班 / 加入 / 我的班级 ----------------

    def create_class(self, owner_uid: str, name: str) -> dict:
        name = (name or "").strip()
        if not (1 <= len(name) <= 32):
            raise ValueError("班级名长度需在 1~32 字之间")
        cid = uuid.uuid4().hex[:12]
        code = "".join(secrets.choice(_JOIN_ALPHABET) for _ in range(6))
        with self._conn() as con:
            con.execute(
                "INSERT INTO classes(id, name, owner_uid, join_code, created_ts)"
                " VALUES(?,?,?,?,?)", (cid, name, owner_uid, code, time.time()))
            con.execute(
                "INSERT INTO class_members(class_id, uid, joined_ts) VALUES(?,?,?)",
                (cid, owner_uid, time.time()))
        return self.class_summary(cid, owner_uid)

    def join(self, code: str, uid: str) -> dict:
        code = (code or "").strip().upper()
        with self._conn() as con:
            row = con.execute(
                "SELECT id FROM classes WHERE join_code=?", (code,)).fetchone()
            if not row:
                raise ValueError("邀请码不存在，请核对大小写")
            cid = row[0]
            already = con.execute(
                "SELECT 1 FROM class_members WHERE class_id=? AND uid=?",
                (cid, uid)).fetchone()
            if not already:
                con.execute(
                    "INSERT INTO class_members(class_id, uid, joined_ts) VALUES(?,?,?)",
                    (cid, uid, time.time()))
        return self.class_summary(cid, uid)

    def _membership(self, cid: str, uid: str) -> str | None:
        """成员在班级里的身份：'owner' / 'member'；不是成员返回 None。"""
        with self._conn() as con:
            row = con.execute(
                "SELECT c.owner_uid FROM class_members m"
                " JOIN classes c ON c.id = m.class_id"
                " WHERE m.class_id=? AND m.uid=?", (cid, uid)).fetchone()
        if not row:
            return None
        return "owner" if row[0] == uid else "member"

    def class_summary(self, cid: str, viewer_uid: str) -> dict:
        role = self._membership(cid, viewer_uid)
        if role is None:
            raise KeyError(f"班级不存在或未加入：{cid}")
        with self._conn() as con:
            c = con.execute(
                "SELECT id, name, owner_uid, join_code FROM classes WHERE id=?",
                (cid,)).fetchone()
            n = con.execute(
                "SELECT COUNT(*) FROM class_members WHERE class_id=?",
                (cid,)).fetchone()[0]
            a = con.execute(
                "SELECT COUNT(*) FROM assignments WHERE class_id=?",
                (cid,)).fetchone()[0]
        return {
            "id": c[0], "name": c[1],
            "owner_uid": c[2], "role": role,
            "join_code": c[3] if role == "owner" else None,
            "members": n, "assignments": a,
        }

    def my_classes(self, uid: str) -> list[dict]:
        with self._conn() as con:
            ids = [r[0] for r in con.execute(
                "SELECT class_id FROM class_members WHERE uid=? ORDER BY joined_ts",
                (uid,)).fetchall()]
        return [self.class_summary(cid, uid) for cid in ids]

    def owned_classes(self, uid: str) -> list[str]:
        """uid 以教师身份创建的班级 id 列表（审题台等教师功能的门禁依据）。"""
        with self._conn() as con:
            return [r[0] for r in con.execute(
                "SELECT id FROM classes WHERE owner_uid=?", (uid,)).fetchall()]

    def members(self, cid: str, uid: str) -> list[dict]:
        """成员列表（全员可见；含用户名与各自档案 id）。"""
        if self._membership(cid, uid) is None:
            raise KeyError(f"班级不存在或未加入：{cid}")
        with self._conn() as con:
            rows = con.execute(
                "SELECT m.uid, u.username, u.is_guest, u.student_id, m.joined_ts"
                " FROM class_members m JOIN users u ON u.id = m.uid"
                " WHERE m.class_id=? ORDER BY m.joined_ts", (cid,)).fetchall()
        return [{"uid": r[0], "username": r[1], "is_guest": bool(r[2]),
                 "student_id": r[3], "joined_ts": r[4]} for r in rows]

    # ---------------- 作业 ----------------

    def create_assignment(self, cid: str, uid: str, title: str,
                          kp_items: list[dict]) -> dict:
        """owner-only。kp_items: [{kp_id, kp_name}, ...]，1~20 题。"""
        if self._membership(cid, uid) != "owner":
            raise PermissionError("只有班级创建者（教师）能布置作业")
        title = (title or "").strip()
        if not (1 <= len(title) <= 60):
            raise ValueError("标题长度需在 1~60 字之间")
        if not (1 <= len(kp_items) <= 20):
            raise ValueError("一次作业 1~20 个知识点")
        aid = uuid.uuid4().hex[:12]
        seen: set[str] = set()
        items = []
        for it in kp_items:
            kid = (it.get("kp_id") or "").strip()
            if not kid or kid in seen:
                continue
            seen.add(kid)
            items.append({"kp_id": kid,
                          "kp_name": (it.get("kp_name") or kid)[:40],
                          "seed": derive_seed(aid, len(items))})
        if not items:
            raise ValueError("没有有效的知识点")
        with self._conn() as con:
            con.execute(
                "INSERT INTO assignments(id, class_id, title, items, created_ts)"
                " VALUES(?,?,?,?,?)",
                (aid, cid, title, json.dumps(items, ensure_ascii=False), time.time()))
        return {"id": aid, "title": title, "items": len(items)}

    def _assignment(self, aid: str) -> tuple[dict, list[dict]]:
        with self._conn() as con:
            row = con.execute(
                "SELECT id, class_id, title, items FROM assignments WHERE id=?",
                (aid,)).fetchone()
        if not row:
            raise KeyError(f"作业不存在：{aid}")
        return {"id": row[0], "class_id": row[1], "title": row[2]}, \
               json.loads(row[3])

    def assign_role_ok(self, aid: str, uid: str) -> str:
        """要求 uid 既在班级又作业属该班级，返回身份；否则抛异常。"""
        a, _ = self._assignment(aid)
        role = self._membership(a["class_id"], uid)
        if role is None:
            raise KeyError("班级不存在或未加入")
        return role

    def assignments_for(self, cid: str, uid: str) -> list[dict]:
        """班级作业列表 + 当前用户进度。"""
        role = self._membership(cid, uid)
        if role is None:
            raise KeyError(f"班级不存在或未加入：{cid}")
        with self._conn() as con:
            rows = con.execute(
                "SELECT id, title, items, created_ts FROM assignments"
                " WHERE class_id=? ORDER BY created_ts DESC", (cid,)).fetchall()
            my = {r[0]: r[1] for r in con.execute(
                "SELECT assignment_id, COUNT(*) FROM homework_results"
                " WHERE uid=? GROUP BY assignment_id", (uid,)).fetchall()}
            my_ok = dict(con.execute(
                "SELECT assignment_id, SUM(correct) FROM homework_results"
                " WHERE uid=? GROUP BY assignment_id", (uid,)).fetchall())
            # 仅 owner 才算全班统计
            done_map: dict[str, int] = {}
            ok_map: dict[str, int] = {}
            if role == "owner":
                for r in con.execute(
                        "SELECT assignment_id, COUNT(DISTINCT uid), SUM(correct)"
                        " FROM homework_results GROUP BY assignment_id").fetchall():
                    done_map[r[0]], ok_map[r[0]] = r[1], r[2] or 0
            n_members = con.execute(
                "SELECT COUNT(*) FROM class_members WHERE class_id=?",
                (cid,)).fetchone()[0]
        out = []
        for aid, title, items, created in rows:
            n = len(json.loads(items))
            done = my.get(aid, 0)
            entry = {"id": aid, "title": title, "created_ts": created,
                     "total": n, "my_done": done, "my_correct": int(my_ok.get(aid) or 0)}
            if role == "owner":
                # 全班：完成人数（做过至少一题的独立 uid）与对题总数
                entry["class_done"] = done_map.get(aid, 0)
                entry["class_correct"] = ok_map.get(aid, 0)
                entry["members"] = n_members
            out.append(entry)
        return out

    def assignment_detail(self, aid: str, uid: str) -> dict:
        """作业详情 + 我的结果（teacher 视角另见 results()）。"""
        a, items = self._assignment(aid)
        if self._membership(a["class_id"], uid) is None:
            raise KeyError("班级不存在或未加入")
        with self._conn() as con:
            mine = {r[0]: r for r in con.execute(
                "SELECT item, correct, choice, ts FROM homework_results"
                " WHERE assignment_id=? AND uid=?", (aid, uid)).fetchall()}
        for i, it in enumerate(items):
            it["seed"] = it.get("seed") or derive_seed(aid, i)
            it["index"] = i
            r = mine.get(i)
            it["done"] = bool(r)
            it["correct"] = bool(r[1]) if r else None
            it["choice"] = r[2] if r else None
        return {**a, "items": items}

    def results(self, aid: str, uid: str) -> dict:
        """owner-only：全班 × 全题矩阵（1 对 / 0 错 / undefined 未做）+ 汇总。"""
        a, items = self._assignment(aid)
        if self._membership(a["class_id"], uid) != "owner":
            raise PermissionError("只有班级创建者（教师）能查看成绩矩阵")
        ms = self.members(a["class_id"], uid)
        with self._conn() as con:
            res = con.execute(
                "SELECT uid, item, correct FROM homework_results WHERE assignment_id=?",
                (aid,)).fetchall()
        grid: dict[tuple[str, int], int] = {(u, i): int(c) for u, i, c in res}
        rows = []
        for m in ms:
            per = [grid.get((m["uid"], i)) for i in range(len(items))]
            rows.append({"uid": m["uid"], "username": m["username"],
                         "per": per,
                         "correct": sum(1 for p in per if p == 1),
                         "done": sum(1 for p in per if p is not None)})
        per_q = [sum(1 for r in rows if r["per"][i] is not None)
                 for i in range(len(items))]
        ok_q = [sum(1 for r in rows if r["per"][i] == 1)
                for i in range(len(items))]
        return {"assignment": a, "items": items, "rows": rows,
                "per_item_done": per_q, "per_item_correct": ok_q}

    def record_result(self, aid: str, uid: str, item: int,
                      correct: bool, choice: int) -> None:
        a, items = self._assignment(aid)
        role = self._membership(a["class_id"], uid)
        if role is None:
            raise KeyError("班级不存在或未加入")
        if not (0 <= item < len(items)):
            raise ValueError(f"题号越界：{item}")
        with self._conn() as con:
            con.execute(
                "INSERT INTO homework_results(assignment_id, uid, item, correct,"
                " choice, ts) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(assignment_id, uid, item)"
                " DO UPDATE SET correct=excluded.correct,"
                " choice=excluded.choice, ts=excluded.ts",
                (aid, uid, item, int(bool(correct)), choice, time.time()))
