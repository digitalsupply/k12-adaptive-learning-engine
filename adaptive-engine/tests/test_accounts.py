"""账号体系与 SQLite 学生档案存储测试。全部用临时目录，不碰真实 data/。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from ale.accounts import (Accounts, check_token, hash_password, load_secret,
                          make_token)
from ale.state import Attempt, StudentStore as FileStore
from ale.store_sqlite import StudentStore


# ---------------- 口令与令牌 ----------------

def test_password_hash_salted_and_verified():
    s1, s2 = b"saber" * 4, b"lance" * 4
    h1, h2 = hash_password("秘密678", s1), hash_password("秘密678", s2)
    assert h1 != h2, "不同盐必须产生不同哈希"
    assert h1 == hash_password("秘密678", s1), "同盐同密码必须稳定"


def test_token_roundtrip_and_expiry(tmp_path):
    secret = load_secret(tmp_path / "secret.key")
    tok = make_token("u123", secret, days=1)
    assert check_token(tok, secret) == "u123"
    assert check_token(tok, b"other-secret") is None
    expired = make_token("u123", secret, days=-1)
    assert check_token(expired, secret) is None
    assert check_token("garbage", secret) is None


def test_secret_persisted_once(tmp_path):
    assert load_secret(tmp_path / "s.key") == load_secret(tmp_path / "s.key")


# ---------------- 账号 ----------------

@pytest.fixture()
def ac(tmp_path):
    return Accounts(tmp_path / "app.db")


def test_register_login_flow(ac):
    u = ac.register("林小满", "password123")
    assert u["is_guest"] is False
    again = ac.login("林小满", "password123")
    assert again["id"] == u["id"]


def test_login_wrong_password(ac):
    ac.register("陈知行", "password123")
    with pytest.raises(ValueError, match="不正确"):
        ac.login("陈知行", "wrong-password")
    with pytest.raises(ValueError, match="不正确"):
        ac.login("无此用户", "password123")


def test_register_duplicate_rejected(ac):
    ac.register("陈知行", "password123")
    with pytest.raises(ValueError, match="占用"):
        ac.register("陈知行", "password456")


def test_register_validation(ac):
    for bad_name in ("a", "名字" * 20, ""):            # 过短 / 超过 32 字 / 空
        with pytest.raises(ValueError):
            ac.register(bad_name, "password123")
    with pytest.raises(ValueError):
        ac.register("赵小白", "123")


def test_guest_cannot_have_login(ac):
    g = ac.create_guest()
    assert g["is_guest"] is True and g["username"] is None


def test_bind_student(ac):
    u = ac.register("学生1号", "password123")
    ac.bind_student(u["id"], "stu1")
    assert ac.require(u["id"])["student_id"] == "stu1"


def test_unknown_uid_raises(ac):
    with pytest.raises(KeyError):
        ac.require("no-such-uid")


# ---------------- SQLite 学生档案 ----------------

@pytest.fixture()
def store(tmp_path):
    return StudentStore(tmp_path / "app.db")


def test_create_get_roundtrip(store):
    stu = store.create("林小满", model="bkt", owner_id="uid1")
    got = store.get(stu.id)
    assert got.name == "林小满" and got.model == "bkt" and got.owner_id == "uid1"
    assert got.is_demo is False


def test_missing_student_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.get("no-such-sid")


def test_mutate_locks_and_persists(store):
    stu = store.create("并发测试", owner_id="uid1")

    def bump(s):
        s.attempts.append(Attempt(kp_id="k1", correct=True, ts=1.0))
        return "ok"

    result, _ = store.mutate(stu.id, bump)
    assert result == "ok"
    assert len(store.get(stu.id).attempts) == 1


def test_visit_visibility_demo_and_null_owner(store):
    mine = store.create("我的档案", owner_id="me")
    other = store.create("别人的正式档案", owner_id="other")
    demo = store.create("演示学生", is_demo=True)
    no_owner = store.create("CLI 建的档案")            # owner=None

    mine_ids = {s["id"] for s in store.list_all(owner_id="me")}
    assert mine.id in mine_ids and demo.id in mine_ids and no_owner.id in mine_ids
    assert other.id not in mine_ids, "别人的正式档案不能出现"

    all_ids = {s["id"] for s in store.list_all()}
    assert all_ids == {mine.id, other.id, demo.id, no_owner.id}


def test_delete(store):
    stu = store.create("会删的人", owner_id="me")
    store.delete(stu.id)
    with pytest.raises(KeyError):
        store.get(stu.id)


def test_import_legacy_dir(tmp_path):
    legacy = tmp_path / "students"
    fs = FileStore(legacy)
    expected = {fs.create(f"旧档案{c}").id for c in "甲乙丙"}

    db = StudentStore(tmp_path / "app.db")
    assert db.import_legacy_dir(legacy) == 3
    imported = {s["id"] for s in db.list_all()}
    assert imported == expected
    assert all(s["is_demo"] is True for s in db.list_all())

    # 再导一次不重复
    assert db.import_legacy_dir(legacy) == 0


def test_state_survives_reopen(tmp_path):
    db1 = StudentStore(tmp_path / "app.db")
    stu = db1.create("重启测试", model="elo", owner_id="uid7")
    again = StudentStore(tmp_path / "app.db").get(stu.id)
    assert again.model == "elo" and again.owner_id == "uid7"


def test_accounts_and_store_share_db(tmp_path):
    """两个模块连同一个库文件，WAL 下互不干扰。"""
    db_path = tmp_path / "app.db"
    Accounts(db_path)
    StudentStore(db_path).create("共存测试")


def test_state_column_is_valid_json(store):
    stu = store.create("原子写入测试")
    with sqlite3.connect(store.db_path) as con:
        raw = con.execute("SELECT state FROM students WHERE id=?",
                          (stu.id,)).fetchone()[0]
    assert json.loads(raw)["name"] == "原子写入测试"
