"""精选题库（审题台数据层）+ 引擎「缓存 → 精选 → LLM/模板」复用链测试。"""

from __future__ import annotations

import json

import pytest

from ale.curated import CuratedStore
from ale.quiz import Question

KW = "欧姆定律"


def _q(kp_id="kp-x", seed=7, options=None, answer=1):
    return Question(
        kp_id=kp_id, kp_name=KW, seed=seed, kind="choice",
        prompt=f"题面 v{seed}", options=options or ["甲", "乙", "丙", "丁"],
        answer_index=answer, explain="由 V=IR 代入得", objective="会算电流",
        misconception="把电流电压比反了", source="llm", model="test-model")


@pytest.fixture()
def store(tmp_path):
    return CuratedStore(tmp_path / "app.db")


# ---------------- 数据层 ----------------

def test_upsert_and_get_roundtrip(store):
    q = _q()
    row = store.upsert(q, "approved", reviewer="王老师", note="好题")
    assert row["status"] == "approved" and row["kp_id"] == "kp-x"
    got = store.get(q.qhash)
    assert got["reviewer"] == "王老师" and got["status"] == "approved"
    # 同题再审（驳回）→ 覆盖状态,不炸主键
    store.upsert(q, "rejected", reviewer="王老师", note="选项含糊")
    assert store.get(q.qhash)["status"] == "rejected"
    assert store.counts() == {"approved": 0, "rejected": 1}
    # 非法状态拒绝
    with pytest.raises(ValueError):
        store.upsert(q, "maybe", reviewer="x")


def test_serve_at_is_deterministic_and_rotates(store):
    a, b = _q(seed=1, options=["A1", "A2", "A3", "A4"]), _q(seed=2, options=["B1", "B2", "B3", "B4"])
    store.upsert(a, "approved", "t")
    store.upsert(b, "approved", "t")
    s1 = store.serve_at("kp-x", 100)
    s2 = store.serve_at("kp-x", 100)
    assert s1.prompt == s2.prompt and s1.seed == 100      # 同 seed 同题
    assert s1.qhash != a.qhash                            # seed 改写 → 新 qhash
    assert sorted(o[:1] for o in s1.options) == sorted(
        o[:1] for o in (a.options if 100 % 2 == 0 else b.options))
    other = store.serve_at("kp-x", 101)
    assert other.prompt != s1.prompt                      # 换 seed 轮换另一道
    assert store.serve_at("没有/cache", 1) is None


def test_unapprove_keeps_record(store):
    q = _q()
    store.upsert(q, "approved", "t")
    row = store.unapprove(q.qhash, "t")
    assert row["status"] == "rejected"
    assert store.serve_at("kp-x", 0) is None              # 不再参与复用
    assert store.get(q.qhash) is not None                 # 记录保留
    assert store.unapprove(q.qhash, "t") is None          # 已下架 → None
    assert store.unapprove("不存在", "t") is None


def test_counts_scoped_by_kp(store):
    store.upsert(_q(kp_id="a"), "approved", "t")
    store.upsert(_q(kp_id="b", seed=3), "rejected", "t")
    assert store.counts("a") == {"approved": 1, "rejected": 0}
    assert store.counts("b") == {"approved": 0, "rejected": 1}
    assert store.counts() == {"approved": 1, "rejected": 1}


def test_recent_carries_payload(store):
    store.upsert(_q(), "approved", "王老师")
    rows = store.recent(status="approved")
    assert rows[0]["prompt"].startswith("题面")
    assert rows[0]["answer_index"] == 1 and rows[0]["explain"]
    assert "options" in json.dumps(rows[0], ensure_ascii=False)


# ---------------- 引擎复用链（真实数据集冒烟） ----------------

@pytest.fixture(scope="module")
def real_engine():
    from ale.config import Config
    from ale.engine import AdaptiveEngine
    cfg = Config()
    if not (cfg.data_dir / "index.json").exists():
        pytest.skip("数据集未就位")
    return AdaptiveEngine(cfg)


def test_curated_question_is_served_and_rebuildable(real_engine, tmp_path, monkeypatch):
    """入库后的题：带任意 seed 出题时应被优先复用,且判分重建(qhash)能对上。"""
    eng = real_engine
    kps, _ = eng.dataset.group("初中", "物理")
    kp_id = next(kid for kid, kp in kps.items()
                 if kp.assessability >= eng.config.quiz_min_assessability)

    # 精选库换到临时文件：真实库不进测试数据
    from ale.curated import CuratedStore
    monkeypatch.setattr(eng, "curated", CuratedStore(tmp_path / "t.db"))

    # 审定一道模板基准题入库（模板路径不写缓存，不会污染后续判定）
    base = eng.make_question(kp_id, seed=990001, force_template=True)
    eng.curated.upsert(base, "approved", "测试教师")

    # fresh seed → 缓存未命中 → 精选命中,题面一致
    q = eng.make_question(kp_id, seed=990777)
    assert q.source == "curated"
    assert q.prompt == base.prompt
    assert q.seed == 990777

    # 判分链:rebuild 模式从注入的缓存重建 → qhash 一致
    rb = eng.make_question(kp_id, seed=990777, for_answer=True)
    assert rb.qhash == q.qhash and rb.answer_index == q.answer_index
    assert eng._qbank.get(q.qhash) is not None and q.options == rb.options

    # 复用面向新知识点不串题：另一个 kp 的 fresh seed 也应能出题
    kp2 = next((kid for kid, kp in kps.items()
                if kid != kp_id and
                kp.assessability >= eng.config.quiz_min_assessability),
               None)
    if kp2:
        q2 = eng.make_question(kp2, seed=990778)
        assert q2.kp_id == kp2
        assert q2.qhash                       # 无论走哪条链,题都是完整的
