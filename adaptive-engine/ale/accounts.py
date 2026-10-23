"""账号体系：注册 / 登录 / 会话令牌，SQLite 落库。

设计要点：

- 口令加密存储：PBKDF2-HMAC-SHA256 + 每用户随机盐（hashlib 标准库实现，
  不引第三方依赖；迁移到 Postgres 时换 passlib 只动这一个模块）。
- 会话令牌：无状态 HMAC 签名 `uid.到期.签名`，放在 HttpOnly Cookie 里。
  无需会话表，服务重启不掉登录态；改密钥（data/secret.key）即全员下线。
- 游客：无需注册即可获得一个游客账号（无口令、不可再登录），保证"点开
  就能玩"；注册后拥有自己的正式档案。

数据库文件：data/app.db（与学生档案、出题缓存互不冲突；表结构见 Accounts._init_db）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import uuid

PBKDF2_ITER = 120_000          # ~0.1s/次，可接受且远超裸 SHA
TOKEN_DAYS = 30
SESSION_COOKIE = "k12_session"
_SALT_BYTES = 16


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITER).hex()


def load_secret(path) -> bytes:
    """会话签名密钥：首次自动生成并落盘（0600）。"""
    path = str(path)
    try:
        data = open(path, "rb").read()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return data


def make_token(uid: str, secret: bytes, days: int = TOKEN_DAYS) -> str:
    exp = int(time.time()) + days * 86400
    body = f"{uid}.{exp}"
    sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{body}.{sig}"


def check_token(token: str, secret: bytes) -> str | None:
    """通过则返回 uid，否则 None。"""
    try:
        uid, exp, sig = token.split(".")
        body = f"{uid}.{exp}"
        good = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, good):
            return None
        if int(exp) < time.time():
            return None
        return uid
    except (ValueError, TypeError):
        return None


class Accounts:
    """users 表的访问层。调用方（server）不需直接碰 SQL。"""

    def __init__(self, db_path, secret_path=None):
        self.db_path = str(db_path)
        self.secret = load_secret(secret_path or
                                  os.path.join(os.path.dirname(self.db_path),
                                               "secret.key"))
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _init_db(self) -> None:
        with self._conn() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    id            TEXT PRIMARY KEY,
                    username      TEXT UNIQUE,
                    password_hash TEXT,
                    salt          TEXT,
                    is_guest      INTEGER NOT NULL DEFAULT 0,
                    student_id    TEXT,
                    created_ts    REAL
                )""")
            # 角色自选（师生平等）：老库补列,默认学生
            cols = [r[1] for r in con.execute("PRAGMA table_info(users)")]
            if "role" not in cols:
                con.execute("ALTER TABLE users ADD COLUMN role TEXT"
                            " NOT NULL DEFAULT 'student'")

    # ---------------- 注册 / 登录 ----------------

    def register(self, username: str, password: str,
                 role: str = "student") -> dict:
        """注册并返回 user 行。用户名已存在抛 ValueError。角色自选："""
        if role not in ("teacher", "student"):
            raise ValueError("角色只能是 teacher（教师）或 student（学生）")
        username = (username or "").strip()
        if not (2 <= len(username) <= 32):
            raise ValueError("用户名长度需在 2~32 字之间")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        uid = uuid.uuid4().hex[:12]
        salt = secrets.token_bytes(_SALT_BYTES)
        phash = hash_password(password, salt)
        try:
            with self._conn() as con:
                con.execute(
                    "INSERT INTO users(id, username, password_hash, salt,"
                    " is_guest, role, created_ts) VALUES(?,?,?,?,0,?,?)",
                    (uid, username, phash, salt.hex(), role, time.time()))
        except sqlite3.IntegrityError:
            raise ValueError(f"用户名已被占用：{username}")
        return self.require(uid)

    def login(self, username: str, password: str) -> dict:
        with self._conn() as con:
            row = con.execute(
                "SELECT id, salt, password_hash FROM users WHERE username=?",
                ((username or "").strip(),)).fetchone()
        if not row:
            raise ValueError("用户名或密码不正确")
        uid, salt_hex, stored = row
        calc = hash_password(password or "", bytes.fromhex(salt_hex))
        if not hmac.compare_digest(calc, stored):
            raise ValueError("用户名或密码不正确")
        return self.require(uid)

    def create_guest(self) -> dict:
        """游客：无用户名无口令，仅保证有归属档案。前端拿到 cookie 即可做题。"""
        uid = "guest_" + uuid.uuid4().hex[:10]
        with self._conn() as con:
            con.execute(
                "INSERT INTO users(id, username, is_guest, created_ts)"
                " VALUES(?,NULL,1,?)", (uid, time.time()))
        return self.require(uid)

    # ---------------- 查询 ----------------

    def get(self, uid: str) -> dict | None:
        with self._conn() as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM users WHERE id=?",
                              (uid,)).fetchone()
        return self._row(row) if row else None

    def require(self, uid: str) -> dict:
        u = self.get(uid)
        if not u:
            raise KeyError(f"用户不存在：{uid}")
        return u

    def bind_student(self, uid: str, student_id: str) -> None:
        with self._conn() as con:
            con.execute("UPDATE users SET student_id=? WHERE id=?",
                        (student_id, uid))

    def set_role(self, uid: str, role: str) -> dict:
        """切换自选角色（师生平等，随时可换）。返回更新后的 user。"""
        if self.get(uid) is None:
            raise KeyError(f"用户不存在：{uid}")
        if role not in ("teacher", "student"):
            raise ValueError("角色只能是 teacher（教师）或 student（学生）")
        with self._conn() as con:
            con.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        return self.require(uid)

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"], "username": row["username"],
            "is_guest": bool(row["is_guest"]),
            "student_id": row["student_id"],
            "role": row["role"] if "role" in row.keys() else "student",
            "created_ts": row["created_ts"],
        }

    def token_for(self, uid: str) -> str:
        return make_token(uid, self.secret)

    def uid_from_token(self, token: str | None) -> str | None:
        if not token:
            return None
        return check_token(token, self.secret)
