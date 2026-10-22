"""出题器测试：题目必须"可判分"——有唯一正确答案、干扰项是真误区、
且服务端能凭 (kp_id, seed) 重建同一道题。
"""

from __future__ import annotations

import pytest

from ale import quiz
from ale.config import Config
from ale.dataset import KnowledgePoint


def _kp(misconceptions=None, objectives=None, assessability=0.9, difficulty=0.5):
    return KnowledgePoint(
        id="k1", name="测试知识点", subject="测试", grade_band="初中",
        grades=["八年级"], confidence=0.95, difficulty=difficulty,
        learning_objectives=objectives if objectives is not None else [
            "能用等腰直角三角形推导这些值",
            "能熟记特殊角的精确值",
        ],
        misconceptions=misconceptions if misconceptions is not None else [
            "记混 tan30° 与 tan60° 的值",
            "认为 sin45°=1/2",
            "忽略 cos30°=√3/2 而误写为 1/2",
        ],
        assessability=assessability,
    )


# ---------------- 题型

def test_choice_question_has_unique_correct_answer():
    q = quiz.generate(_kp(), seed=1)
    assert q.kind == "choice"
    assert 0 <= q.answer_index < len(q.options)
    assert len(set(q.options)) == len(q.options), "选项不能重复"
    # 正确项必须来自 learning_objectives
    assert q.options[q.answer_index] in q.objective or q.objective in q.options


def test_choice_distractors_are_misconceptions():
    kp = _kp()
    q = quiz.generate(kp, seed=1)
    wrong = [o for i, o in enumerate(q.options) if i != q.answer_index]
    for o in wrong:
        assert o in kp.misconceptions, f"干扰项不是误区：{o}"


def test_judge_question_has_both_possible_answers():
    """判断题不能永远是"错"——否则可以靠猜。"""
    kp = _kp()
    answers = set()
    for seed in range(30):
        q = quiz.generate(kp, seed=seed, prefer="judge")
        answers.add(q.options[q.answer_index])
    assert len(answers) == 2, f"判断题答案只有 {answers}，可被猜中"


def test_judge_statement_is_either_objective_or_misconception():
    kp = _kp()
    for seed in range(20):
        q = quiz.generate(kp, seed=seed, prefer="judge")
        statement = q.prompt.split("「", 1)[1].rstrip("」")
        assert statement in kp.learning_objectives or statement in kp.misconceptions


def test_no_question_when_material_missing():
    kp = _kp(misconceptions=[], objectives=["只有目标"])
    with pytest.raises(quiz.NoQuestionError):
        quiz.generate(kp, seed=1)


def test_no_question_when_assessability_too_low():
    kp = _kp(assessability=0.2)
    with pytest.raises(quiz.NoQuestionError):
        quiz.generate(kp, seed=1)
    assert quiz.can_ask(kp) is False


def test_can_ask():
    assert quiz.can_ask(_kp()) is True


# ---------------- 确定性

def test_same_seed_same_question():
    kp = _kp()
    a = quiz.generate(kp, seed=7)
    b = quiz.generate(kp, seed=7)
    assert a.options == b.options
    assert a.answer_index == b.answer_index
    assert a.kind == b.kind


def test_different_seed_different_question():
    kp = _kp()
    variants = {tuple(quiz.generate(kp, seed=s).options) for s in range(20)}
    assert len(variants) > 1, "不同 seed 应该能出不同的题"


def test_auto_kind_has_variety():
    """自动出题不能清一色选择题——换换题型。"""
    kp = _kp()
    kinds = {quiz.generate(kp, seed=s).kind for s in range(60)}
    assert kinds == {"choice", "judge"}, f"题型只有 {kinds}"


def test_question_id_format():
    q = quiz.generate(_kp(), seed=3)
    assert q.question_id == "k1@3"


# ---------------- 输出契约

def test_to_dict_does_not_leak_answer():
    q = quiz.generate(_kp(), seed=1)
    d = q.to_dict()
    assert "answer_index" not in d
    assert "correct_option" not in d
    assert "explain" not in d
    assert len(d["options"]) == len(q.options)


def test_to_dict_reveal_has_answer():
    q = quiz.generate(_kp(), seed=1)
    d = q.to_dict(reveal=True)
    assert d["answer_index"] == q.answer_index
    assert d["correct_option"] == q.options[q.answer_index]
    assert d["explain"]


# ---------------- 真实数据集

@pytest.fixture(scope="module")
def real_engine():
    from ale.engine import AdaptiveEngine
    cfg = Config()
    if not (cfg.data_dir / "index.json").exists():
        pytest.skip("数据集未就位")
    return AdaptiveEngine(cfg)


def test_real_dataset_learning_objectives_are_loaded(real_engine):
    """回归：annotation 的 key 是驼峰 learningObjectives，早期按蛇形读导致
    「学习目标」对全部知识点都是空的。"""
    kps, _ = real_engine.dataset.group("初中", "数学")
    with_obj = [k for k in kps.values() if k.learning_objectives]
    assert len(with_obj) > len(kps) * 0.9, \
        f"只有 {len(with_obj)}/{len(kps)} 个知识点读到了学习目标"
    assert with_obj[0].learning_objectives[0]


def test_real_dataset_most_kps_can_be_asked(real_engine):
    kps, _ = real_engine.dataset.group("初中", "数学")
    askable = [k for k in kps.values() if quiz.can_ask(k)]
    assert len(askable) > len(kps) * 0.9, \
        f"只有 {len(askable)}/{len(kps)} 个知识点能出题"


def test_real_dataset_question_is_well_posed(real_engine):
    """真实数据出的题必须可判分。

    - 选择题：正确项在 learningObjectives 里，干扰项在 misconceptions 里；
    - 判断题：陈述来自 misconceptions（答案为错）或 learningObjectives（答案为对）。
    """
    kps, _ = real_engine.dataset.group("初中", "数学")
    kp = next(k for k in kps.values()
              if quiz.can_ask(k) and k.misconceptions and k.learning_objectives)
    seen_kinds = set()
    for seed in range(40):
        q = quiz.generate(kp, seed=seed)
        seen_kinds.add(q.kind)
        if q.kind == "choice":
            assert q.options[q.answer_index] in kp.learning_objectives
            for i, o in enumerate(q.options):
                if i != q.answer_index:
                    assert o in kp.misconceptions
        else:
            statement = q.prompt.split("「", 1)[1].rstrip("」")
            assert statement in kp.learning_objectives or statement in kp.misconceptions
            assert q.options[q.answer_index] in ("说法正确", "说法错误")
    assert seen_kinds == {"choice", "judge"}, f"只出现了 {seen_kinds}"


def test_answer_question_records_attempt(real_engine):
    """答完题要真的写入掌握度，且答对答错方向正确。"""
    eng = real_engine
    kps, _ = eng.dataset.group("初中", "数学")
    kp = next(k for k in kps.values() if quiz.can_ask(k))
    sid = eng.create_student("出题测试").id

    q = eng.make_question(kp.id, seed=1)
    r = eng.answer_question(sid, kp.id, 1, q.answer_index)
    assert r["correct"] is True
    assert r["p_after"] > r["p_before"], "答对后掌握度必须上升"

    # 选一个错误选项 → 掌握度下降，并给出归因
    wrong = 0 if q.answer_index != 0 else 1
    r2 = eng.answer_question(sid, kp.id, 1, wrong)
    assert r2["correct"] is False
    assert r2["p_after"] < r2["p_before"], "答错后掌握度必须下降"
    assert r2["explain"]
    assert r2["correct_option"]


def test_answer_question_rejects_out_of_range(real_engine):
    eng = real_engine
    kps, _ = eng.dataset.group("初中", "数学")
    kp = next(k for k in kps.values() if quiz.can_ask(k))
    sid = eng.create_student("越界测试").id
    with pytest.raises(ValueError):
        eng.answer_question(sid, kp.id, 1, 99)
