"""核心算法测试：用合成小图 + 真实数据集抽样验证。

合成图拓扑：
    A(掌握) ─→ B(薄弱) ─→ C(症状1)
                └─→ D(症状2)
    E(无关，已掌握)

预期：对 C、D 归因，B 应为根因（A 已掌握，排除）。
"""

from __future__ import annotations

import time

import pytest

from ale.config import Config
from ale.dataset import KnowledgePoint, Relation
from ale.diagnose import diagnose
from ale.graph import KnowledgeGraph
from ale.mastery import BKTModel, EloModel, MasteryState, create_model
from ale.planner import next_best, plan
from ale.state import Student


def _kp(kid: str, name: str, difficulty: float = 0.5,
        kp_type: str = "conceptual") -> KnowledgePoint:
    return KnowledgePoint(id=kid, name=name, subject="测试", grade_band="初中",
                          grades=["八年级"], confidence=0.95, difficulty=difficulty,
                          kp_type=kp_type)


def make_fixture() -> KnowledgeGraph:
    kps = {k.id: k for k in [
        _kp("A", "基础概念", 0.2), _kp("B", "关键方法", 0.5, "procedural"),
        _kp("C", "综合应用一", 0.7), _kp("D", "综合应用二", 0.6),
        _kp("E", "无关知识", 0.3),
    ]}
    rels = [
        Relation("r1", "A", "B", "prerequisite"),
        Relation("r2", "B", "C", "prerequisite"),
        Relation("r3", "B", "D", "prerequisite"),
    ]
    return KnowledgeGraph(kps, rels)


NOW = time.time()


# ---------------- 图

def test_topo_and_depth():
    g = make_fixture()
    order = g.topological_order()
    assert order.index("A") < order.index("B") < order.index("C")
    assert g.depth("A") == 0 and g.depth("B") == 1 and g.depth("C") == 2
    assert g.cycles() == []


def test_cycle_detection():
    kps = {k.id: k for k in [_kp("X", "x"), _kp("Y", "y")]}
    g = KnowledgeGraph(kps, [Relation("r", "X", "Y", "prerequisite"),
                             Relation("r2", "Y", "X", "prerequisite")])
    order = g.topological_order()
    assert set(order) == {"X", "Y"}          # 环被截断仍返回全序
    assert len(g.cycles()) == 1              # 但被记录


def test_ancestors_and_chain():
    g = make_fixture()
    anc = g.ancestors("C")
    assert anc == {"B": 1, "A": 2}
    chain = g.chain_to_root("C")
    assert chain == ["A", "B", "C"]


# ---------------- 掌握度

def test_bkt_update_direction():
    m = BKTModel()
    kp = _kp("K", "知识点", 0.5)
    st = MasteryState(p_mastery=m.prior(kp))
    up = m.update(st, kp, True, NOW)
    down = m.update(st, kp, False, NOW)
    assert up.p_mastery > st.p_mastery > down.p_mastery
    assert 0 < down.p_mastery < 1
    # 连续答对收敛到高掌握
    cur = st
    for i in range(10):
        cur = m.update(cur, kp, True, NOW + i * 60)
    assert cur.p_mastery > 0.95


def test_bkt_hard_kp_lower_prior():
    m = BKTModel()
    easy = m.prior(_kp("E", "e", 0.1))
    hard = m.prior(_kp("H", "h", 0.9))
    assert hard < easy


def test_elo_update_direction():
    m = EloModel()
    kp = _kp("K", "知识点", 0.6)
    st = MasteryState(p_mastery=m.prior(kp))
    up = m.update(st, kp, True, NOW)
    down = m.update(st, kp, False, NOW)
    assert up.p_mastery > st.p_mastery > down.p_mastery


def test_forget_decay():
    for m in (BKTModel(), EloModel()):
        kp = _kp("K", "k", 0.5)
        st = MasteryState(p_mastery=0.9, attempts=5, correct=5, last_ts=NOW)
        later = m.decay(st, kp, NOW + 30 * 86400)
        assert later.p_mastery < 0.9


def test_model_factory():
    assert create_model("bkt").name == "bkt"
    assert create_model("elo").name == "elo"
    with pytest.raises(ValueError):
        create_model("nope")


# ---------------- 诊断

def _student_with_mastered(*ids: str) -> Student:
    stu = Student(id="s", name="t")
    for kid in ids:
        stu.mastery[kid] = MasteryState(p_mastery=0.95, attempts=6, correct=6, last_ts=NOW)
    return stu


def test_diagnosis_finds_root_cause():
    g = make_fixture()
    stu = _student_with_mastered("A", "E")
    # B、C、D 都弱（从未练习，先验低）
    d = diagnose(g, stu, BKTModel(), ["C", "D"], NOW, Config())
    assert d.root_causes, "应产出根因"
    top = d.root_causes[0]
    assert top.kp.id == "B", f"根因应为 B，实际 {top.kp.id}"
    assert {e.symptom_id for e in top.explained} == {"C", "D"}
    assert top.explained[0].path_names[0] == "关键方法"


def test_diagnosis_excludes_mastered():
    g = make_fixture()
    stu = _student_with_mastered("A")
    stu.mastery["B"] = MasteryState(p_mastery=0.95, attempts=5, correct=5, last_ts=NOW)
    d = diagnose(g, stu, BKTModel(), ["C"], NOW, Config())
    assert all(c.kp.id != "A" for c in d.root_causes)


def test_diagnosis_symptom_itself_is_cause_when_no_weak_anc():
    g = make_fixture()
    stu = _student_with_mastered("A", "B")   # 先修都好，只有 C 弱
    d = diagnose(g, stu, BKTModel(), ["C"], NOW, Config())
    assert d.root_causes[0].kp.id == "C"


# ---------------- 规划

def test_plan_orders_and_skips():
    g = make_fixture()
    stu = _student_with_mastered("A")
    p = plan(g, stu, BKTModel(), "C", NOW, Config())
    ids = [n.kp.id for n in p.nodes]
    assert ids == ["B", "C"]                 # A 已掌握被剔除
    assert p.nodes[0].stage == 0 and p.nodes[1].stage == 1
    assert p.skipped_mastered == 1
    assert p.nodes[-1].kp.id == "C"          # 目标总在最后


def test_plan_truncation():
    cfg = Config()
    cfg.plan_max_nodes = 2
    g = make_fixture()
    stu = Student(id="s", name="t")
    p = plan(g, stu, BKTModel(), "C", NOW, cfg)
    assert len(p.nodes) <= 2
    assert p.truncated
    assert p.nodes[-1].kp.id == "C"          # 截断也保住目标


def test_next_best_respects_prereqs():
    g = make_fixture()
    stu = _student_with_mastered("A")
    recs = next_best(g, stu, BKTModel(), NOW, goal_id="C", top_n=5, config=Config())
    ids = [r.kp.id for r in recs]
    assert "B" in ids                        # 先修已满足
    assert ids[0] == "B"                     # 距目标最近
    assert "C" not in ids                    # B 未掌握前，C 的先修不满足，不可推荐
    assert "D" not in ids                    # 同理 D
    assert "E" in ids                        # 无前置的独立节点始终可学


def test_next_best_blocked_by_prereq():
    g = make_fixture()
    stu = Student(id="s", name="t")          # 全部未掌握
    recs = next_best(g, stu, BKTModel(), NOW, goal_id="C", top_n=10, config=Config())
    ids = [r.kp.id for r in recs]
    assert "C" not in ids                    # B 未掌握，C 不可推荐
    assert ids[0] == "A"                     # 无前置的先来


# ---------------- 真实数据集冒烟

@pytest.fixture(scope="module")
def real_engine():
    from ale.engine import AdaptiveEngine
    cfg = Config()
    if not (cfg.data_dir / "index.json").exists():
        pytest.skip("数据集未就位")
    return AdaptiveEngine(cfg)


def test_real_data_smoke(real_engine):
    eng = real_engine
    groups = eng.dataset.list_groups()
    assert len(groups) > 10
    g = eng.graph("初中", "数学")
    assert g.stats()["kp_count"] > 500
    assert g.stats()["max_depth"] >= 5
    # 端到端：建学生 → 模拟 → 归因 → 规划
    stu = eng.create_student("测试同学", model="bkt")
    kps, _ = eng.dataset.group("初中", "数学")
    # 找一个有先修链的深节点
    goal = max(kps, key=lambda k: g.depth(k))
    chain = g.chain_to_root(goal)
    eng.simulate_learner(stu.id, chain[:2], ability=0.3, noise=0.0)
    d = eng.diagnose_recent_failures(stu.id)
    assert d.root_causes
    p = eng.make_plan(stu.id, goal)
    assert p.nodes and p.nodes[-1].kp.id == goal


# ---------------- 环与脏数据回归

def test_cycle_depth_does_not_crash():
    """35 节点大环 + 正常链：depth/stats 必须可用（初中物理真实故障的回归）。"""
    kps, rels = {}, []
    n = 35
    for i in range(n):
        kps[f"c{i}"] = _kp(f"c{i}", f"环内{i}", 0.5)
    for i in range(n):
        rels.append(Relation(f"rc{i}", f"c{i}", f"c{(i + 1) % n}", "prerequisite"))
    # 链：A → B → 环入口
    for kid in ("A", "B"):
        kps[kid] = _kp(kid, f"链{kid}", 0.4)
    rels.append(Relation("ra", "A", "B", "prerequisite"))
    rels.append(Relation("rb", "B", "c0", "prerequisite"))

    g = KnowledgeGraph(kps, rels)
    assert g.stats()["kp_count"] == n + 2
    assert g.depth("A") == 0 and g.depth("B") == 1
    # 环内所有节点共享同一分量深度
    assert len({g.depth(f"c{i}") for i in range(n)}) == 1
    cycles = g.cycles()
    assert len(cycles) == 1 and len(cycles[0].nodes) == n


def test_null_confidence_is_tolerated(tmp_path):
    """confidence 显式为 null 不应导致加载崩溃（初中某科目真实故障的回归）。"""
    import json as _json
    from ale.dataset import Dataset

    (tmp_path / "初中").mkdir()
    (tmp_path / "index.json").write_text(_json.dumps({"files": [{
        "path": "初中/测试.json", "grade_band": "初中", "subject": "测试",
        "kp_count": 2, "relation_count": 0, "size_kb": 1}]}), encoding="utf-8")

    def raw(kid, conf):
        return {"id": kid, "canonical_name": kid, "subject": "测试",
                "grade_band": "初中", "confidence": conf, "aliases": "[]",
                "annotations": "{}", "grades": [], "textbooks": []}

    (tmp_path / "初中" / "测试.json").write_text(_json.dumps({
        "knowledge_points": [raw("a", None), raw("b", 0.9)],
        "relations": []}), encoding="utf-8")

    ds = Dataset(Config(data_dir=tmp_path))
    kps, _ = ds.group("初中", "测试")
    assert set(kps) == {"a", "b"}
    assert kps["a"].confidence == 1.0


def test_plan_on_cycle_does_not_crash_and_shares_stage():
    """环内节点必须共享同一阶段，规划不得崩溃（初中物理大环的回归）。"""
    kps = {k.id: k for k in [_kp("A", "基础", 0.3), _kp("X", "环一", 0.5),
                             _kp("Y", "环二", 0.5), _kp("Z", "环三", 0.5)]}
    rels = [Relation("r1", "A", "X", "prerequisite"),
            Relation("r2", "X", "Y", "prerequisite"),
            Relation("r3", "Y", "Z", "prerequisite"),
            Relation("r4", "Z", "X", "prerequisite")]   # X→Y→Z→X 成环
    g = KnowledgeGraph(kps, rels)
    stu = Student(id="s", name="t")
    p = plan(g, stu, BKTModel(), "Y", NOW, Config())
    ids = [n.kp.id for n in p.nodes]
    assert ids[-1] == "Y" or "Y" in ids
    stages = {n.kp.id: n.stage for n in p.nodes if n.kp.id in {"X", "Y", "Z"}}
    assert len(set(stages.values())) == 1, f"环内节点应同阶段，实际 {stages}"


def test_knowledge_type_parsed_from_annotations(tmp_path):
    """annotations.knowledgeType 的 value 是字符串时不能被二次拆包成 unknown。"""
    import json as _json
    from ale.dataset import Dataset

    (tmp_path / "初中").mkdir()
    (tmp_path / "index.json").write_text(_json.dumps({"files": [{
        "path": "初中/测试.json", "grade_band": "初中", "subject": "测试",
        "kp_count": 1, "relation_count": 0, "size_kb": 1}]}), encoding="utf-8")
    ann = {"knowledgeType": {"value": "procedural", "source": "llm"},
           "difficulty": {"value": 0.6}, "learningObjectives": {"value": ["掌握 X"]},
           "prerequisites": {"value": []}, "misconceptions": {"value": ["误用 Y"]},
           "assessability": {"value": 0.9}}
    (tmp_path / "初中" / "测试.json").write_text(_json.dumps({
        "knowledge_points": [{
            "id": "k1", "canonical_name": "测试点", "subject": "测试",
            "grade_band": "初中", "confidence": 0.9, "aliases": "[]",
            "annotations": _json.dumps(ann, ensure_ascii=False),
            "grades": ["八年级"], "textbooks": []}],
        "relations": []}), encoding="utf-8")

    ds = Dataset(Config(data_dir=tmp_path))
    kps, _ = ds.group("初中", "测试")
    kp = kps["k1"]
    assert kp.kp_type == "procedural"
    assert kp.kp_type_label == "程序性"
    assert kp.difficulty == 0.6
    assert kp.misconceptions == ["误用 Y"]


def test_real_data_knowledge_types_present():
    """真实数据 39 科中，类型标注不应整体退化为 unknown。"""
    import pytest as _pytest
    from ale.dataset import Dataset
    cfg = Config()
    if not (cfg.data_dir / "index.json").exists():
        _pytest.skip("数据集未就位")
    ds = Dataset(cfg)
    kps, _ = ds.group("初中", "物理")
    known = sum(1 for k in kps.values() if k.kp_type != "unknown")
    assert known / len(kps) > 0.95, f"类型标注缺失过多：{known}/{len(kps)}"
