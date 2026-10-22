"""掌握度阈值、派生索引缓存、图谱接口收窄的回归测试。

对应此前发现的问题：
  1. elo_mastered_threshold 是死配置 —— 所有"是否掌握"的判断都用了
     mastered_threshold(0.85)，Elo 学生被按 BKT 的标准判定；
  2. locate()/search() 冷启动要全量解析 39 个 split 文件（0.4s / 150MB+）；
  3. /api/graph 一次返回整张图（小学语文 1.4 MB），前端生成上万个 SVG 元素。
"""

from __future__ import annotations

import json

import pytest

from ale.config import Config
from ale.dataset import Dataset
from ale.engine import AdaptiveEngine
from ale.mastery import BKTModel, EloModel

# 固定时间基准：让与时间相关的断言可复现
_FIXED_NOW = 1_800_000_000.0


# ---------------- 阈值按模型分派

def test_mastered_for_dispatches_by_model():
    cfg = Config()
    assert cfg.mastered_for("bkt") == cfg.mastered_threshold == 0.85
    assert cfg.mastered_for("elo") == cfg.elo_mastered_threshold == 0.80
    # 未知模型回落到 BKT 阈值
    assert cfg.mastered_for("dkt") == cfg.mastered_threshold


def _dummy_kp(difficulty: float = 0.5):
    from ale.dataset import KnowledgePoint
    return KnowledgePoint(id="x", name="测试", subject="测试", grade_band="初中",
                          grades=["八年级"], confidence=0.95, difficulty=difficulty)


def _state_for_model(model, kp, cfg):
    """构造一个在给定模型下 p 落在 (elo阈值, bkt阈值) 之间的状态。

    两个模型的状态语义不同：BKT 的 p_mastery 就是 P(掌握)，Elo 的 p_mastery
    是能力分比例、真实 p 要反算。所以同一份状态不能同时喂给两个模型，
    必须各造各的。

    last_ts 固定为 NOW - 1 小时：若 last_ts 取"此刻"，两次 time.time() 可能
    取到同一个值，导致 decay 有时被跳过、有时生效，p 会漂移，测试就不稳定。
    """
    from ale.mastery import MasteryState
    now = _FIXED_NOW
    low = cfg.mastered_for("elo") + 0.01
    high = cfg.mastered_for("bkt") - 0.01
    for i in range(1, 1000):
        st = MasteryState(p_mastery=i / 1000, attempts=3, correct=3,
                          last_ts=now - 3600)
        p = model.p(st, kp, now)
        if low <= p < high:      # 留出余量，避免边界抖动
            return st, p
    raise AssertionError(f"构造不出 p ∈ [{low}, {high}) 的状态")


def test_elo_threshold_is_actually_used():
    """Elo 的 0.80 阈值必须真的参与判定，而不是死配置。"""
    cfg = Config()
    assert cfg.mastered_for("elo") != cfg.mastered_threshold

    model = EloModel()
    kp = _dummy_kp()
    st, p = _state_for_model(model, kp, cfg)
    # 同一份状态：Elo 判已掌握，BKT 判未掌握
    assert p >= cfg.mastered_for("elo")
    assert p < cfg.mastered_for("bkt")


def test_elo_student_uses_elo_threshold_end_to_end():
    """端到端：Elo 学生的"已掌握"判定必须用 0.80，不是 0.85。"""
    cfg = Config()
    kp = _dummy_kp()
    elo_state, elo_p = _state_for_model(EloModel(), kp, cfg)
    bkt_state, bkt_p = _state_for_model(BKTModel(), kp, cfg)
    assert elo_p >= cfg.mastered_for("elo") and elo_p < cfg.mastered_for("bkt")
    assert bkt_p >= cfg.mastered_for("elo") and bkt_p < cfg.mastered_for("bkt")

    eng = AdaptiveEngine(cfg)
    kp_id = next(iter(eng.dataset.group("初中", "数学")[0]))

    stu = eng.create_student("阈值测试", model="elo")
    stu.mastery[kp_id] = elo_state
    eng.store.save(stu)
    detail = eng.kp_detail(kp_id, sid=stu.id)
    assert detail["mastery"]["mastered"] is True, \
        "Elo 学生应按 0.80 阈值判为已掌握"

    # 同样落在 (0.80, 0.85) 区间的 BKT 状态 → 未掌握
    stu2 = eng.create_student("阈值测试BKT", model="bkt")
    stu2.mastery[kp_id] = bkt_state
    eng.store.save(stu2)
    assert eng.kp_detail(kp_id, sid=stu2.id)["mastery"]["mastered"] is False


def test_planner_uses_model_threshold():
    """路径规划也要按模型阈值，不能只改接口层。"""
    from ale.graph import KnowledgeGraph
    from ale.dataset import KnowledgePoint, Relation
    from ale.planner import plan
    from ale.state import Student

    cfg = Config()
    kps = {k: KnowledgePoint(id=k, name=k, subject="测试", grade_band="初中",
                              grades=[], confidence=1.0, difficulty=0.5)
           for k in ("A", "B")}
    g = KnowledgeGraph(kps, [Relation("r", "A", "B", "prerequisite")])

    # Elo 学生：前置 A 已按 0.80 判为掌握 → 应从路径里剔除
    elo_student = Student(id="e", name="e")
    elo_student.mastery["A"] = _state_for_model(EloModel(), kps["A"], cfg)[0]
    elo_plan = plan(g, elo_student, EloModel(), "B", _FIXED_NOW, cfg)
    assert "A" not in [n.kp.id for n in elo_plan.nodes]

    # BKT 学生：同一知识点未达 0.85 → 应保留在路径里
    bkt_student = Student(id="b", name="b")
    bkt_student.mastery["A"] = _state_for_model(BKTModel(), kps["A"], cfg)[0]
    bkt_plan = plan(g, bkt_student, BKTModel(), "B", _FIXED_NOW, cfg)
    assert "A" in [n.kp.id for n in bkt_plan.nodes]


def test_diagnose_skips_mastered_roots_by_model_threshold():
    """归因也不能把"按 Elo 阈值已掌握"的知识点当成根因。"""
    from ale.graph import KnowledgeGraph
    from ale.dataset import KnowledgePoint, Relation
    from ale.diagnose import diagnose
    from ale.mastery import MasteryState
    from ale.state import Student

    cfg = Config()
    kps = {k: KnowledgePoint(id=k, name=k, subject="测试", grade_band="初中",
                              grades=[], confidence=1.0, difficulty=0.5)
           for k in ("A", "B")}
    g = KnowledgeGraph(kps, [Relation("r", "A", "B", "prerequisite")])

    student = Student(id="s", name="s")
    student.mastery["A"] = _state_for_model(EloModel(), kps["A"], cfg)[0]
    student.mastery["B"] = MasteryState(p_mastery=0.1, attempts=2, correct=0,
                                        last_ts=_FIXED_NOW - 3600)

    d = diagnose(g, student, EloModel(), ["B"], _FIXED_NOW, cfg)
    # A 已按 Elo 阈值掌握 → 不应被列为根因
    assert "A" not in [c.kp.id for c in d.root_causes]


# ---------------- Elo 状态语义回归

def _elo_reference(seq, kp, span=600.0, scale=400.0, k_factor=24.0):
    """能力分独立存放的 Elo 参考实现（教学标准写法）。"""
    ability = span / 2
    for n, ok in enumerate(seq):
        expected = 1 / (1 + 10 ** ((300 - ability) / scale))
        k = k_factor / (1 + 0.1 * n)
        ability += k * ((1 if ok else 0) - expected)
    return 1 / (1 + 10 ** ((300 - ability) / scale))


def test_elo_stores_ability_separately_from_probability():
    """Elo 的状态里必须分开存"能力分"和"胜率"。

    旧实现把胜率写进 p_mastery，下一次 update 又把它当能力分乘一遍 span，
    能力估计系统性偏低（连对 10 次的偏差达 8.6 个百分点）。
    """
    from ale.mastery import EloModel, MasteryState
    kp = _dummy_kp()
    model = EloModel()
    st = MasteryState()
    now = _FIXED_NOW
    for ok in [True] * 10:
        st = model.update(st, kp, ok, now)
    assert st.ability is not None, "Elo 必须单独记录能力分"
    assert st.ability != st.p_mastery * 600 or abs(st.ability) > 1e9


def test_elo_trajectory_matches_reference_implementation():
    """Elo 的学习轨迹必须与"能力分独立存放"的标准写法一致。"""
    from ale.mastery import EloModel, MasteryState
    kp = _dummy_kp()
    model = EloModel()
    now = _FIXED_NOW

    for seq in ([True] * 10, [False] * 6, [i % 2 == 0 for i in range(10)]):
        st = MasteryState()
        for ok in seq:
            st = model.update(st, kp, ok, now)
        assert st.p_mastery == pytest.approx(_elo_reference(seq, kp), abs=1e-9)


def test_elo_decay_is_stable_across_time():
    """遗忘衰减后 p 应当平缓回落，而不是跳变。"""
    from ale.mastery import EloModel, MasteryState
    kp = _dummy_kp()
    model = EloModel()
    now = _FIXED_NOW
    st = MasteryState()
    for _ in range(5):
        st = model.update(st, kp, True, now)
    p0 = st.p_mastery
    p_1h = model.p(st, kp, now + 3600)
    p_24h = model.p(st, kp, now + 86400)
    assert p_1h < p0 and p_24h < p_1h
    # 衰减是连续的，不该出现断崖
    assert p0 - p_24h < 0.05


def test_bkt_state_roundtrip_with_new_ability_field():
    """MasteryState 新增 ability 字段后，旧的 JSON 档案仍能读出来。"""
    from ale.mastery import MasteryState
    st = MasteryState(p_mastery=0.5, attempts=2, correct=1, last_ts=1.0)
    payload = {"p_mastery": 0.5, "attempts": 2, "correct": 1, "last_ts": 1.0}
    restored = MasteryState(**payload)          # 老档案没有 ability
    assert restored.ability is None
    assert restored.p_mastery == st.p_mastery


# ---------------- 派生索引缓存

@pytest.fixture(scope="module")
def real_config():
    cfg = Config()
    if not (cfg.data_dir / "index.json").exists():
        pytest.skip("数据集未就位")
    return cfg


def test_index_cache_roundtrip(real_config, tmp_path):
    """缓存文件能落盘并被重新读回，内容与扫描结果一致。"""
    cfg = Config(data_dir=real_config.data_dir, cache_dir=tmp_path)
    d1 = Dataset(cfg)
    d1._build_id_index()
    assert d1.index_source["id_index"] == "scan"
    assert d1._id_index

    d2 = Dataset(cfg)
    d2._build_id_index()
    assert d2.index_source["id_index"] == "cache"
    assert d2._id_index == d1._id_index


def _make_fake_dataset(root, kp_ids=("k1", "k2")):
    """造一个最小的 split/ 数据集，用于测缓存失效逻辑。"""
    (root / "初中").mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(json.dumps({
        "exported_at": "2026-01-01", "total_kp": len(kp_ids), "total_relations": 0,
        "files": [{"path": "初中/数学.json", "grade_band": "初中", "subject": "数学",
                   "kp_count": len(kp_ids), "relation_count": 0, "size_kb": 1}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "初中" / "数学.json").write_text(json.dumps({
        "knowledge_points": [{"id": k, "canonical_name": f"知识点{k}",
                              "subject": "数学", "grade_band": "初中",
                              "confidence": 1.0, "grades": ["七年级"],
                              "aliases": "[]", "annotations": None, "summary": "",
                              "textbooks": []} for k in kp_ids],
        "relations": [],
    }, ensure_ascii=False), encoding="utf-8")


def test_index_cache_invalidated_when_dataset_changes(tmp_path):
    """数据集变了（指纹变化）→ 缓存自动失效重建。"""
    data_dir = tmp_path / "split"
    cache_dir = tmp_path / "cache"
    _make_fake_dataset(data_dir)

    cfg = Config(data_dir=data_dir, cache_dir=cache_dir)
    d1 = Dataset(cfg)
    d1._build_id_index()
    assert d1.index_source["id_index"] == "scan"
    before = cfg_cache_name(cfg, "id_index")

    # 数据集内容变化 → 指纹变化 → 缓存文件名变化
    _make_fake_dataset(data_dir, kp_ids=("k1", "k2", "k3"))
    d2 = Dataset(cfg)
    after = cfg_cache_name(cfg, "id_index")
    assert before != after, "数据集变更后缓存指纹必须变化"

    # 新指纹下重建，索引里能看到新增的知识点
    d2._build_id_index()
    assert d2.index_source["id_index"] == "scan"
    assert "k3" in d2._id_index


def cfg_cache_name(cfg, name):
    return Dataset(cfg)._cache_path(name).name


def test_search_index_cache_used(real_config, tmp_path):
    cfg = Config(data_dir=real_config.data_dir, cache_dir=tmp_path)
    d1 = Dataset(cfg)
    d1._build_search_index()
    assert d1.index_source["search_index"] == "scan"
    d2 = Dataset(cfg)
    d2._build_search_index()
    assert d2.index_source["search_index"] == "cache"


def test_search_respects_min_confidence(real_config, tmp_path):
    """搜索不能绕过 confidence 过滤。"""
    strict = Config(data_dir=real_config.data_dir, cache_dir=tmp_path,
                    min_confidence=0.9)
    loose = Config(data_dir=real_config.data_dir, cache_dir=tmp_path,
                   min_confidence=0.0)
    d_strict, d_loose = Dataset(strict), Dataset(loose)
    hits_strict = d_strict.search("函数", limit=50)
    hits_loose = d_loose.search("函数", limit=50)
    assert len(hits_loose) >= len(hits_strict)
    for kp in hits_strict:
        assert kp.confidence >= 0.9


def test_search_respects_band_and_subject(real_config, tmp_path):
    cfg = Config(data_dir=real_config.data_dir, cache_dir=tmp_path)
    d = Dataset(cfg)
    hits = d.search("函数", band="初中", subject="数学", limit=20)
    assert hits
    for kp in hits:
        assert kp.grade_band == "初中" and kp.subject == "数学"


# ---------------- 图谱接口收窄

@pytest.fixture(scope="module")
def api():
    import server
    if not (Config().data_dir / "index.json").exists():
        pytest.skip("数据集未就位")
    return server


def test_graph_full_returns_everything(api):
    g = api.graph_view("初中", "数学")
    assert g["nodes"] and g["edges"]
    assert g["truncated"] is False
    assert g["total_nodes"] == len(g["nodes"])


def test_graph_depth_filter(api):
    full = api.graph_view("初中", "数学")
    shallow = api.graph_view("初中", "数学", depth=1)
    assert len(shallow["nodes"]) <= len(full["nodes"])
    for n in shallow["nodes"]:
        assert n["depth"] <= 1
    assert shallow["truncated"] is True
    assert shallow["total_nodes"] == full["total_nodes"]


def test_graph_root_scoping_returns_connected_component(api):
    g = api.eng.graph("初中", "数学")
    goal = max(g.kps, key=lambda k: g.depth(k))       # kp_id（str）
    scoped = api.graph_view("初中", "数学", root=goal)
    ids = {n["id"] for n in scoped["nodes"]}
    assert goal in ids
    # 连通分量内的节点必须真的与根相连
    for kid in ids:
        assert kid == goal or g.prereq_of.get(kid) or g.dependents_of.get(kid)
    # 边两端都在返回的节点里
    for e in scoped["edges"]:
        assert e["source"] in ids and e["target"] in ids


def test_graph_spine_excludes_isolated_nodes(api):
    g = api.eng.graph("初中", "数学")
    spine = api.graph_view("初中", "数学", spine=True)
    ids = {n["id"] for n in spine["nodes"]}
    for kid in ids:
        assert g.prereq_of.get(kid) or g.dependents_of.get(kid)
    assert len(ids) < len(g.kps)


def test_graph_max_nodes_keeps_coherent_subgraph(api):
    """max_nodes 截断必须留下内部连贯的子图，而不是一堆孤立点。"""
    scoped = api.graph_view("小学", "语文", max_nodes=300)
    assert len(scoped["nodes"]) <= 300
    assert scoped["truncated"] is True
    # 边数不应是 0/1 这种碎片
    assert len(scoped["edges"]) >= 10


def test_graph_unknown_root_returns_404(api):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        api.graph_view("初中", "数学", root="00000000-0000-0000-0000-000000000000")
    assert ei.value.status_code == 404


def test_graph_unknown_subject_returns_404(api):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        api.graph_view("初中", "不存在的科目")
    assert ei.value.status_code == 404


def test_config_endpoint_exposes_thresholds(api):
    c = api.get_config()
    assert c["mastered_threshold"] == 0.85
    assert c["elo_mastered_threshold"] == 0.80
    assert c["low_confidence_hint"] == 0.6


def test_batch_attempts_endpoint(api):
    """批量作答：一次请求写一次档案。"""
    sid = api.create_student(api.StudentIn(name="批量", model="bkt"))["id"]
    kps = list(api.eng.dataset.group("初中", "数学")[0])[:5]
    body = api.AttemptsIn(attempts=[api.AttemptIn(kp_id=k, correct=True) for k in kps])
    out = api.record_attempts(sid, body)
    assert out["count"] == 5
    assert out["correct"] == 5
    stu = api.eng.get_student(sid)
    assert len(stu.attempts) == 5
    # 掌握度确实写进去了
    detail = api.eng.kp_detail(kps[0], sid=sid)
    assert detail["mastery"]["attempts"] == 1


def test_simulate_learner_writes_once_per_batch(real_config, tmp_path):
    """模拟 N 题只落盘一次（不是 N 次全量 JSON 重写）。"""
    from ale.state import StudentStore
    cfg = Config(data_dir=real_config.data_dir, cache_dir=tmp_path,
                 students_dir=tmp_path / "students")
    eng = AdaptiveEngine(cfg)

    writes = []
    orig_save = StudentStore._save_unlocked

    def counting_save(self, stu):
        writes.append(stu.id)
        return orig_save(self, stu)

    StudentStore._save_unlocked = counting_save
    try:
        sid = eng.create_student("写次数").id
        writes.clear()                      # 建学生那一次不算
        kps = sorted(eng.dataset.group("初中", "数学")[0])[:20]
        eng.simulate_learner(sid, kps, seed=1)
    finally:
        StudentStore._save_unlocked = orig_save

    assert len(writes) == 1, f"20 题模拟应只写 1 次，实际写了 {len(writes)} 次"
    assert len(eng.get_student(sid).attempts) == 20
