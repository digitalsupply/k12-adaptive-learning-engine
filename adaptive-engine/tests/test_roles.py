"""自选角色（师生平等）逻辑：注册带角色、切换、老账号回填。"""

from __future__ import annotations

import pytest

from ale.accounts import Accounts
from ale.classroom import Classroom


@pytest.fixture()
def accounts(tmp_path):
    db = tmp_path / "app.db"
    return Accounts(db, secret_path=db.parent / "secret.key")


def test_register_defaults_to_student_and_validates(accounts):
    s = accounts.register("小赵", "pass123456")
    assert s["role"] == "student"
    t = accounts.register("老李", "pass123456", role="teacher")
    assert t["role"] == "teacher"
    with pytest.raises(ValueError):
        accounts.register("校长", "pass123456", role="principal")
    with pytest.raises(ValueError):
        accounts.set_role(s["id"], "admin")


def test_set_role_roundtrip(accounts):
    u = accounts.register("小钱", "pass123456")
    assert accounts.set_role(u["id"], "teacher")["role"] == "teacher"
    assert accounts.require(u["id"])["role"] == "teacher"
    assert accounts.set_role(u["id"], "student")["role"] == "student"


def test_legacy_class_owners_backfilled_to_teacher(tmp_path):
    """老库没有 role 列:建过班的账号初始化班级表时自动升为教师角色。"""
    db = tmp_path / "app.db"
    acc = Accounts(db, secret_path=db.parent / "secret.key")
    t = acc.register("老教师", "pass123456", role="teacher")
    cr = Classroom(db, acc)
    c = cr.create_class(t["id"], "回填班")
    # 模拟旧版数据：role 列被重置/不存在,账号仍是 student
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE users SET role='student' WHERE id=?", (t["id"],))
    con.commit()
    con.close()
    assert acc.get(t["id"])["role"] == "student"
    # 重新初始化班级层 → 回填
    Classroom(db, acc)
    assert acc.get(t["id"])["role"] == "teacher"
    assert c["join_code"]      # 班级数据无损
