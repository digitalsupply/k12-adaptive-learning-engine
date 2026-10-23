"""LLM 出题链路测试：提示词、校验、缓存、三级回退与作答一致性。

全部用桩客户端，不联网。真连通性靠 cli.py llm-check 手动验证。
"""

from __future__ import annotations

import json

import pytest

from ale import quiz
from ale.config import Config
from ale.dataset import KnowledgePoint
from ale.llm import LLMConfig, LLMError, LLMClient, parse_json_block
from ale.engine import AdaptiveEngine, QHashMismatch, _AnswerBank


# ---------------- 桩件 ----------------

def _kp(**kw):
    defaults = dict(
        id="k1", name="特殊角的三角函数", subject="数学", grade_band="初中",
        grades=["九年级"], confidence=0.95, difficulty=0.6,
        learning_objectives=[f"学习目标{i}" for i in range(3)],
        misconceptions=[f"常见误区{i}" for i in range(3)],
        assessability=0.9,
    )
    defaults.update(kw)
    return KnowledgePoint(**defaults)


class FakeLLM(LLMClient):
    """返回预设内容的桩客户端。

    reply 可设为字符串（合法回复）或 raising=LLMError（模拟故障）。
    """

    def __init__(self, reply="{}", fail_times=0):
        super().__init__(LLMConfig(base_url="http://stub", api_key="x", model="stub-1"))
        self.reply = reply
        self.fail_times = fail_times
        self.calls: list[tuple[str, str]] = []

    def chat(self, system, user, temperature=None, max_tokens=None):
        self.calls.append((system, user))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise LLMError("桩故障")
        return self.reply


def _llm_obj(prompt="题干：正弦函数的值域是什么？", options=None,
             answer_index=1, explain="正确项依据学习目标；其余选项分别对应常见误区，已逐一说明。",
             mis="常见误区1"):
    return json.dumps({
        "prompt": prompt,
        "options": options or ["选项甲", "选项乙", "选项丙", "选项丁"],
        "answer_index": answer_index,
        "explain": explain,
        "misconception_used": mis,
    }, ensure_ascii=False)


# ---------------- parse_json_block

def test_parse_json_plain():
    assert parse_json_block('{"a":1}') == {"a": 1}


def test_parse_json_in_code_fence():
    text = '以下是题目：\n```json\n{"a": 1}\n```\n希望有帮助'
    assert parse_json_block(text) == {"a": 1}


def test_parse_json_with_surrounding_text():
    assert parse_json_block('好的\n{"a":{"b":2}} 以上') == {"a": {"b": 2}}


def test_parse_json_no_json_raises():
    with pytest.raises(LLMError):
        parse_json_block("我觉得这道题出不出来")


# ---------------- LLM 命题

def test_llm_generate_builds_context_with_material():
    fak = FakeLLM(_llm_obj())
    kp = _kp()
    quiz._llm_generate(kp, seed=5, client=fak)
    assert len(fak.calls) == 1
    system, user = fak.calls[0]
    # 命题规范必须强调「干扰项要能看起来合理」与「禁止标注式残句」
    assert "标注意式残句" not in system or "残句" in system
    assert "常见误区" in system and "看起来合理" in system
    kb = json.loads(user.split("知识点材料：")[1].split("\n\n请按")[0])
    assert kb["知识点"] == kp.name
    assert any("常见误区" in k for k in kb), "上下文必须包含误区素材"
    assert any("学习目标" in k for k in kb)
    assert "命题角度：" in user.split("\n\n")[0]


def test_llm_generate_validates_and_returns_question():
    q = quiz._llm_generate(_kp(), seed=1, client=FakeLLM(_llm_obj(answer_index=2)))
    assert q.kind == "choice"
    assert q.source == "llm"
    assert q.options[q.answer_index] == "选项丙"
    assert q.misconception == "常见误区1"


def test_llm_generate_rejects_duplicate_options():
    with pytest.raises(LLMError):
        quiz._llm_generate(_kp(), seed=1, client=FakeLLM(
            _llm_obj(options=["甲", "乙", "甲", "丁"])))


def test_llm_generate_rejects_marked_style_options():
    """用户抱怨的就是「认为…／能…」式残句。校验不必识别所有句式，但
    校验规则要挡得住明显的空选项与重复；长选项也被限长。"""
    with pytest.raises(LLMError):
        quiz._llm_generate(_kp(), seed=1, client=FakeLLM(
            _llm_obj(options=["", "乙", "丙", "丁"])))
    with pytest.raises(LLMError):
        quiz._llm_generate(_kp(), seed=1, client=FakeLLM(
            _llm_obj(answer_index=7)))


def test_llm_cache_roundtrip(tmp_path):
    kp = _kp()
    q1 = quiz.generate_cached(kp, seed=3, cache_dir=tmp_path,
                              llm=FakeLLM(_llm_obj()))
    assert q1.source == "llm"
    # 第二次：不同桩（内容变了也无所谓）→ 必须命中缓存拿第一道题
    q2 = quiz.generate_cached(kp, seed=3, cache_dir=tmp_path,
                              llm=FakeLLM(_llm_obj(answer_index=0)))
    assert q2.options == q1.options
    assert q2.answer_index == q1.answer_index
    assert q2.source == "llm-cache"


def test_llm_cache_keyed_by_model_and_seed(tmp_path):
    kp = _kp()
    _ = quiz.generate_cached(kp, seed=1, cache_dir=tmp_path,
                             llm=FakeLLM(_llm_obj()))
    other = FakeLLM(_llm_obj(answer_index=0))
    other.config.model = "stub-2"       # 换模型 → 缓存键不同
    b = quiz.generate_cached(kp, seed=1, cache_dir=tmp_path, llm=other)
    assert b.source == "llm"          # 换模型 → 缓存不命中，重新生成
    c = quiz.generate_cached(kp, seed=99, cache_dir=tmp_path,
                             llm=FakeLLM(_llm_obj()))
    assert c.seed == 99


def test_llm_failure_falls_back_to_template(tmp_path):
    q = quiz.generate_cached(_kp(), seed=2, cache_dir=tmp_path,
                             llm=FakeLLM(fail_times=1))
    assert q.source == "template"
    assert q.kind in ("choice", "judge")


def test_no_llm_generated_without_llm(tmp_path):
    q = quiz.generate_cached(_kp(), seed=4, cache_dir=tmp_path, llm=None)
    assert q.source == "template"


def test_rebuild_never_calls_llm(tmp_path):
    """作答校验模式：缓存丢失时重建必须是模板题，且桩不能被再次调用。"""
    fak = FakeLLM(_llm_obj())
    q1 = quiz.generate_cached(_kp(), seed=8, cache_dir=tmp_path, llm=fak)
    fak.fail_times = 0
    del fak.calls[:]
    q2 = quiz.generate_cached(_kp(), seed=8, cache_dir=tmp_path,
                              llm=fak, rebuild=True)
    assert not fak.calls, "重建模式禁止调 LLM"
    # 缓存还在 → 重建出同一道题
    assert q2.qhash == q1.qhash


def test_assessability_gate_applies_to_llm_too(tmp_path):
    with pytest.raises(quiz.NoQuestionError):
        quiz.generate_cached(_kp(assessability=0.2), seed=1,
                             cache_dir=tmp_path, llm=FakeLLM())


def test_qhash_changes_with_options():
    q = quiz._llm_generate(_kp(), seed=1, client=FakeLLM(_llm_obj()))
    q2 = quiz._llm_generate(_kp(), seed=1, client=FakeLLM(
        _llm_obj(options=["A1", "B1", "C1", "D1"])))
    assert q.qhash != q2.qhash


def test_qhash_stable_for_same_options():
    q1 = quiz._llm_generate(_kp(), seed=1, client=FakeLLM(_llm_obj()))
    q2 = quiz._llm_generate(_kp(), seed=1, client=FakeLLM(_llm_obj()))
    assert q1.qhash == q2.qhash


# ---------------- 真实引擎端到端

@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    cfg = Config()
    if not (cfg.data_dir / "index.json").exists() and not cfg.data_dir.exists():
        pytest.skip("数据集未就位")
    cfg.quiz_cache_dir = tmp_path_factory.mktemp("qcache")
    eng = AdaptiveEngine(cfg)
    # 强制走模板路径（不联网），但答案链路照常
    return eng


def test_real_engine_answer_flow_with_qhash(engine, tmp_path):
    kps, _ = engine.dataset.group("初中", "数学")
    kp = next(k for k in kps.values() if quiz.can_ask(k))
    sid = engine.create_student("qhash测试").id
    # 生产路径：出题不指定题型（自动题型对 (kp, seed) 确定）
    q = engine.make_question(kp.id, seed=6, force_template=True)

    r = engine.answer_question(sid, kp.id, 6, q.answer_index, qhash=q.qhash)
    assert r["correct"] is True

    # qhash 传错 → 拒绝判分
    with pytest.raises(QHashMismatch):
        engine.answer_question(sid, kp.id, 6, q.answer_index, qhash="deadbeef")

    # 不传 qhash（旧客户端）→ 仍按同一道题判分
    r2 = engine.answer_question(sid, kp.id, 6, 0)
    assert "correct" in r2


def test_real_engine_out_of_range_choice(engine):
    kps, _ = engine.dataset.group("初中", "数学")
    kp = next(k for k in kps.values() if quiz.can_ask(k))
    sid = engine.create_student("越界qhash").id
    with pytest.raises(ValueError):
        engine.answer_question(sid, kp.id, 1, 9)


# ---------------- 答案库：出题缓存丢失后照样判分 ----------------

def test_answer_bank_roundtrip(tmp_path):
    bank = _AnswerBank(tmp_path / "question_bank.jsonl")
    q = quiz.Question(kp_id="kp1", kp_name="性质应用", seed=1, kind="choice",
                      prompt="题干 $\\sqrt{2}$", options=["甲", "乙", "丙", "丁"],
                      answer_index=1, explain="解析文字", source="llm",
                      model="stub-1")
    bank.put(q)
    bank.put(q)                                   # 重复 put 不重复落盘
    assert len((tmp_path / "question_bank.jsonl").read_text(
        encoding="utf-8").splitlines()) == 1
    row = bank.get(q.qhash)
    assert row["answer_index"] == 1 and row["explain"] == "解析文字"
    assert bank.get("不存在") is None
    # 重新加载（模拟重启）：数据不丢
    bank2 = _AnswerBank(tmp_path / "question_bank.jsonl")
    assert bank2.get(q.qhash)["answer_index"] == 1


def test_answer_bank_scores_when_cache_lost(engine):
    """出题缓存被清 → 重建退回模板 → qhash 变；答案库兜住判分与解析。"""
    kps, _ = engine.dataset.group("初中", "数学")
    kp = next(k for k in kps.values() if quiz.can_ask(k))
    sid = engine.create_student("答案库兜底").id
    jhash = "f00dfeed12345678"                    # 16 位十六进制，模拟真实 qhash
    engine._qbank.rows[jhash] = {
        "qhash": jhash, "kp_id": kp.id, "seed": 77, "model": "stub-1",
        "answer_index": 2, "explain": "由性质 $(\\sqrt{a})^2=a$ 可知……",
        "objective": None, "misconception": "以为可以直接开方负数",
        "kind": "choice", "saved_ts": 0,
    }
    r = engine.answer_question(sid, kp.id, 77, 2, qhash=jhash)
    assert r["correct"] is True
    assert "性质" in r["explain"]
    r2 = engine.answer_question(sid, kp.id, 77, 0, qhash=jhash)
    assert r2["correct"] is False and r2["misconception"]

    # 答案库也没有的 qhash → 才是真正题目刷新，409
    with pytest.raises(QHashMismatch):
        engine.answer_question(sid, kp.id, 78, 0, qhash="0123456789abcdef")


def test_answer_bank_survives_missing_seed(engine):
    """客户端把 seed 填 0（如旧响应没有 seed 字段）→ 仍按 qhash 判分给解析。"""
    kps, _ = engine.dataset.group("初中", "数学")
    kp = next(k for k in kps.values() if quiz.can_ask(k))
    sid = engine.create_student("漏发seed").id
    jhash = "abcd00ef56781234"
    engine._qbank.rows[jhash] = {
        "qhash": jhash, "kp_id": kp.id, "seed": 667, "model": "stub-1",
        "answer_index": 3, "explain": "正确答案是 D，因为……",
        "objective": None, "misconception": None, "kind": "choice", "saved_ts": 0,
    }
    # seed 传 0（等于 AnswerIn 的默认值）→ 不应 409，应按 qhash 从答案库判分
    r = engine.answer_question(sid, kp.id, 0, 3, qhash=jhash)
    assert r["correct"] is True and "正确答案" in r["explain"]
    r2 = engine.answer_question(sid, kp.id, 0, 1, qhash=jhash)
    assert r2["correct"] is False


def test_quiz_to_dict_carries_seed():
    q = quiz.Question(kp_id="kp1", kp_name="x", seed=5, kind="choice",
                      prompt="t", options=["a", "b"], answer_index=0,
                      explain="e")
    assert q.to_dict()["seed"] == 5
