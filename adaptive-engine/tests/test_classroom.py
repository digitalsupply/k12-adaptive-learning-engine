"""班级/作业模块测试：建班、加入、权限、题目 seed 确定性、成绩矩阵。

全部落在 tmp SQLite，不碰真实数据集（kp 名由调用方传入快照）。
"""

from __future__ import annotations

import pytest

from ale.accounts import Accounts
from ale.classroom import Classroom, derive_seed


@pytest.fixture(scope="module")
def cr(tmp_path_factory):
    db = tmp_path_factory.mktemp("classdb") / "app.db"
    acc = Accounts(db, secret_path=db.parent / "secret.key")
    return Classroom(db, acc)


def _user(cr, name):
    return cr.accounts.register(name, "pass123456")


def test_create_and_join_and_roles(cr):
    t = _user(cr, "teacher胡")
    s1 = _user(cr, "student林")
    c = cr.create_class(t["id"], "初三 1 班数学")
    assert c["role"] == "owner" and c["members"] == 1 and c["join_code"]
    j = cr.join(c["join_code"], s1["id"])
    assert j["role"] == "member" and j["members"] == 2
    assert cr.join(c["join_code"], s1["id"])["members"] == 2     # 重复加入幂等
    assert cr.my_classes(s1["id"])[0]["name"] == "初三 1 班数学"
    # 学生看到的 join_code 为空（不泄露）；教师可见
    assert j["join_code"] is None
    assert cr.class_summary(c["id"], t["id"])["join_code"] == c["join_code"]
    # 非成员完全不可见
    s2 = _user(cr, "student陈")
    with pytest.raises(KeyError):
        cr.class_summary(c["id"], s2["id"])
    with pytest.raises(ValueError):
        cr.join("BOGUS", s2["id"])


def test_assignment_owner_only_and_deterministic_seeds(cr):
    t = _user(cr, "teacher华")
    s = _user(cr, "student赵")
    c = cr.create_class(t["id"], "初二 2 班物理")
    kps = [{"kp_id": "a", "kp_name": "欧姆定律"}, {"kp_id": "b", "kp_name": "串并联"}]
    with pytest.raises(PermissionError):
        cr.create_assignment(c["id"], s["id"], "抄作业偷试", kps)
    a1 = cr.create_assignment(c["id"], t["id"], "第 1 次：欧姆定律", kps)
    detail = cr.assignment_detail(a1["id"], t["id"])
    seeds = [it["seed"] for it in detail["items"]]
    # seed 由 (aid, 题号) 确定派生：重建稳定，不同作业不同
    assert derive_seed(a1["id"], 0) == seeds[0] == derive_seed(a1["id"], 0)
    a2 = cr.create_assignment(c["id"], t["id"], "第 2 次：欧姆定律", kps)
    assert seeds[0] != cr.assignment_detail(a2["id"], t["id"])["items"][0]["seed"]
    assert 0 <= seeds[0] < 1_000_000
    # 未加入班级的成员看不到详情
    s2 = _user(cr, "student钱")
    with pytest.raises(KeyError):
        cr.assignment_detail(a1["id"], s2["id"])


def test_create_assignment_validates(cr):
    t = _user(cr, "teacher孙")
    c = cr.create_class(t["id"], "七 3 班")
    with pytest.raises(ValueError):
        cr.create_assignment(c["id"], t["id"], "空作业", [])
    with pytest.raises(ValueError):
        cr.create_assignment(c["id"], t["id"], "", [{"kp_id": "a"}])
    dup = cr.create_assignment(c["id"], t["id"], "去重",
                               [{"kp_id": "a", "kp_name": "x"}] * 3)
    assert dup["items"] == 1


def test_results_matrix_and_recount(cr):
    t = _user(cr, "teacher周")
    s1, s2 = _user(cr, "student吴"), _user(cr, "student郑")
    c = cr.create_class(t["id"], "临时班")
    a = cr.create_assignment(c["id"], t["id"], "温度与内能",
                             [{"kp_id": "k1", "kp_name": "温度"},
                              {"kp_id": "k2", "kp_name": "内能"}])
    aid = a["id"]
    cr.join(c["join_code"], s1["id"])
    cr.join(c["join_code"], s2["id"])
    cr.record_result(aid, s1["id"], 0, True, 2)
    cr.record_result(aid, s1["id"], 1, False, 1)
    cr.record_result(aid, s2["id"], 0, False, 0)
    with pytest.raises(PermissionError):
        cr.results(aid, s1["id"])
    m = cr.results(aid, t["id"])
    by = {r["username"]: r for r in m["rows"]}
    assert by["student吴"]["per"] == [1, 0] and by["student吴"]["correct"] == 1
    assert by["student郑"]["per"] == [0, None] and by["student郑"]["done"] == 1
    assert m["per_item_done"] == [2, 1] and m["per_item_correct"] == [1, 0]
    # 越界题号拒绝
    with pytest.raises(ValueError):
        cr.record_result(aid, s1["id"], 9, True, 0)


def test_progress_tracking_in_list(cr):
    t = _user(cr, "teacher冯")
    s = _user(cr, "student卫")
    c = cr.create_class(t["id"], "进度班")
    a = cr.create_assignment(c["id"], t["id"], "递进作业",
                             [{"kp_id": "k1", "kp_name": "x"},
                              {"kp_id": "k2", "kp_name": "y"}])
    cr.join(c["join_code"], s["id"])
    lst = classroom_list(cr, c["id"], s["id"])
    assert lst[0]["my_done"] == 0 and lst[0]["total"] == 2
    cr.record_result(a["id"], s["id"], 0, True, 1)
    lst = classroom_list(cr, c["id"], s["id"])
    assert lst[0]["my_done"] == 1 and lst[0]["my_correct"] == 1


def classroom_list(cr, cid, uid):
    return cr.assignments_for(cid, uid)
