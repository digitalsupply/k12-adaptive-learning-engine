"""掌握度模型：统一 MasteryModel 接口下的 BKT 与 Elo 实现。

两个模型都把「学生 × 知识点」的状态收敛为一个 0~1 的掌握概率，
供诊断、路径规划、复习调度统一消费。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .dataset import KnowledgePoint


@dataclass(slots=True)
class MasteryState:
    """模型无关的状态容器，per (student, kp)。"""
    p_mastery: float = 0.0        # 当前掌握概率 0~1
    attempts: int = 0
    correct: int = 0
    last_ts: float = 0.0          # 最后一次练习的 unix 秒


class MasteryModel(ABC):
    name: str = "base"

    @abstractmethod
    def prior(self, kp: KnowledgePoint) -> float:
        """冷启动先验：新知识点的初始掌握概率。"""

    @abstractmethod
    def update(self, state: MasteryState, kp: KnowledgePoint,
               correct: bool, ts: float) -> MasteryState:
        """一次作答后更新状态（返回新状态，输入状态不被修改）。"""

    @abstractmethod
    def decay(self, state: MasteryState, kp: KnowledgePoint, now: float) -> MasteryState:
        """按时间遗忘，返回应用衰减后的状态快照（不持久化）。"""

    def p(self, state: MasteryState, kp: KnowledgePoint, now: float | None = None) -> float:
        if not state.attempts:
            # 冷启动：未作答时返回模型先验，而非状态默认值 0
            return self.prior(kp)
        st = self.decay(state, kp, now) if now is not None else state
        return min(max(st.p_mastery, 0.0), 1.0)


# ---------------------------------------------------------------- BKT

@dataclass(slots=True)
class BKTParams:
    p_init: float = 0.10    # P(L0)：学生对新知识点的初始掌握概率
    p_transit: float = 0.30 # P(T)：一次练习学会的概率
    p_slip: float = 0.08    # P(S)：掌握了却做错
    p_guess: float = 0.20   # P(G)：没掌握却蒙对
    forget_per_day: float = 0.02


class BKTModel(MasteryModel):
    """经典四参数贝叶斯知识追踪。

    difficulty 用作参数调制：越难的知识点，先验越低、学会率越低、蒙对率略低。
    """

    name = "bkt"

    def __init__(self, params: BKTParams | None = None):
        self.params = params or BKTParams()

    def _tuned(self, kp: KnowledgePoint) -> tuple[float, float, float, float]:
        p = self.params
        d = min(max(kp.difficulty, 0.0), 1.0)
        p_init = p.p_init * (1.0 - 0.5 * d)
        p_transit = p.p_transit * (1.0 - 0.6 * d)
        p_slip = p.p_slip * (1.0 + 0.5 * d)
        p_guess = p.p_guess * (1.0 - 0.3 * d)
        # 数值安全
        return (min(max(p_init, 0.01), 0.6),
                min(max(p_transit, 0.02), 0.8),
                min(max(p_slip, 0.01), 0.3),
                min(max(p_guess, 0.02), 0.5))

    def prior(self, kp: KnowledgePoint) -> float:
        return self._tuned(kp)[0]

    def update(self, state: MasteryState, kp: KnowledgePoint,
               correct: bool, ts: float) -> MasteryState:
        p_l = state.p_mastery if state.attempts else self.prior(kp)
        _, p_t, p_s, p_g = self._tuned(kp)

        if correct:
            post = p_l * (1 - p_s) / (p_l * (1 - p_s) + (1 - p_l) * p_g)
            # 学习转移只在答对后发生：经典 BKT 对答错也套转移，
            # 低先验时会出现"答错反而涨掌握度"的反直觉结果，这里做了修正
            post = post + (1 - post) * p_t
        else:
            post = p_l * p_s / (p_l * p_s + (1 - p_l) * (1 - p_g))
        p_after = post

        return MasteryState(
            p_mastery=min(max(p_after, 0.001), 0.999),
            attempts=state.attempts + 1,
            correct=state.correct + (1 if correct else 0),
            last_ts=ts,
        )

    def decay(self, state: MasteryState, kp: KnowledgePoint, now: float) -> MasteryState:
        if not state.attempts or now <= state.last_ts:
            return state
        days = (now - state.last_ts) / 86400.0
        p0 = self.prior(kp)
        # 指数回落到先验
        p = p0 + (state.p_mastery - p0) * math.exp(-self.params.forget_per_day * days)
        return MasteryState(p, state.attempts, state.correct, state.last_ts)


# ---------------------------------------------------------------- Elo

@dataclass(slots=True)
class EloParams:
    k_factor: float = 24.0
    scale: float = 400.0     # 评分差 → 概率的缩放
    ability_span: float = 600.0  # 难度 0~1 映射到能力区间
    forget_per_day: float = 0.01


class EloModel(MasteryModel):
    """Elo 式掌握度：学生对每个知识点维护一个"能力分"，
    知识点按难度占据一条能力标尺，掌握概率 = 胜率。

    与 BKT 的区别：Elo 对练习次数更敏感、区分度更细，适合与题库成绩联动；
    BKT 参数可解释性更强，适合教学诊断输出。
    """

    name = "elo"

    def __init__(self, params: EloParams | None = None):
        self.params = params or EloParams()

    def _item_rating(self, kp: KnowledgePoint) -> float:
        """知识点"题目分"：难度线性映射到 0~ability_span。"""
        return min(max(kp.difficulty, 0.0), 1.0) * self.params.ability_span

    def prior(self, kp: KnowledgePoint) -> float:
        # 冷启动：学生能力取量表中点，按胜率公式得先验
        p = 1.0 / (1.0 + 10 ** ((self._item_rating(kp) - self.params.ability_span / 2)
                                / self.params.scale))
        return p

    def update(self, state: MasteryState, kp: KnowledgePoint,
               correct: bool, ts: float) -> MasteryState:
        p = self.params
        ability = state.p_mastery * p.ability_span if state.attempts \
            else p.ability_span / 2.0
        expected = 1.0 / (1.0 + 10 ** ((self._item_rating(kp) - ability) / p.scale))
        # K 随练习次数收敛，避免反复横跳
        k = p.k_factor / (1.0 + 0.1 * state.attempts)
        ability += k * ((1.0 if correct else 0.0) - expected)

        prob = 1.0 / (1.0 + 10 ** ((self._item_rating(kp) - ability) / p.scale))
        return MasteryState(
            p_mastery=min(max(prob, 0.001), 0.999),
            attempts=state.attempts + 1,
            correct=state.correct + (1 if correct else 0),
            last_ts=ts,
        )

    def decay(self, state: MasteryState, kp: KnowledgePoint, now: float) -> MasteryState:
        if not state.attempts or now <= state.last_ts:
            return state
        days = (now - state.last_ts) / 86400.0
        p = self.params
        midpoint = p.ability_span / 2.0
        ability = state.p_mastery * p.ability_span
        ability = midpoint + (ability - midpoint) * math.exp(-p.forget_per_day * days)
        prob = 1.0 / (1.0 + 10 ** ((self._item_rating(kp) - ability) / p.scale))
        return MasteryState(prob, state.attempts, state.correct, state.last_ts)


# ---------------------------------------------------------------- 工厂

MODELS: dict[str, type[MasteryModel]] = {"bkt": BKTModel, "elo": EloModel}


def create_model(name: str, forget_per_day: float | None = None) -> MasteryModel:
    if name not in MODELS:
        raise ValueError(f"未知模型：{name}（可选 {list(MODELS)}）")
    model = MODELS[name]()
    if forget_per_day is not None:
        if isinstance(model, BKTModel):
            model.params.forget_per_day = forget_per_day
        elif isinstance(model, EloModel):
            model.params.forget_per_day = forget_per_day
    return model
