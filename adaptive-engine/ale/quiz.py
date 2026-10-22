"""题目生成器：把知识点的标注（正确说法 / 常见误区）变成可作答的题目。

数据支撑：`annotations` 里每个知识点都有
- `learningObjectives`：正确说法（可用作正确答案）
- `misconceptions`：常见误区（可用作干扰项 / 判断题的假命题）
- `assessability`：出题可测评性（0~1，太低的知识点不适合出题）

两种题型：

- **choice（选择正确的说法）**：题干问"下列哪项正确"，正确项取自
  learningObjectives，干扰项取自 misconceptions。答错时能精确指出
  学生踩中的是哪一条误区。
- **judge（判断正误）**：陈述可以是误区（答案：错误），也可以是正确目标
  （答案：正确），两种答案都会出现，避免"永远选'错'"的可猜模式。

题目由 (kp_id, seed) 确定性生成，服务端无需保存题目状态即可校验作答。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .dataset import KnowledgePoint


@dataclass(slots=True)
class Question:
    """一道题。answer_index 只在服务端内部使用，不随题目一起下发给前端。"""
    kp_id: str
    kp_name: str
    seed: int
    kind: str                       # "choice" | "judge"
    prompt: str
    options: list[str]
    answer_index: int
    explain: str
    objective: str | None = None    # 正确答案原文
    misconception: str | None = None  # 该题针对的误区（干扰项之一）
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def question_id(self) -> str:
        return f"{self.kp_id}@{self.seed}"

    def to_dict(self, reveal: bool = False) -> dict[str, Any]:
        out = {
            "question_id": self.question_id,
            "kp_id": self.kp_id,
            "kp_name": self.kp_name,
            "kind": self.kind,
            "prompt": self.prompt,
            "options": list(self.options),
        }
        if reveal:
            out.update({
                "answer_index": self.answer_index,
                "correct_option": self.options[self.answer_index],
                "explain": self.explain,
                "objective": self.objective,
                "misconception": self.misconception,
            })
        return out


class NoQuestionError(ValueError):
    """该知识点没有足够的标注素材出题。"""


def can_ask(kp: KnowledgePoint, min_assessability: float = 0.5) -> bool:
    """判断该知识点能否出题。"""
    if kp.assessability < min_assessability:
        return False
    return bool(kp.learning_objectives) and bool(kp.misconceptions)


def generate(kp: KnowledgePoint, seed: int = 0,
             min_assessability: float = 0.5,
             prefer: str | None = None) -> Question:
    """为某个知识点生成一道题。

    seed 相同 → 题目完全相同（选项顺序也一样），服务端可据此重新生成并校验。
    prefer 可强制指定 "choice" 或 "judge"，默认按素材情况自动选。
    """
    if kp.assessability < min_assessability:
        raise NoQuestionError(f"该知识点可测评性过低（{kp.assessability:.2f}），不适合出题")

    objectives = list(kp.learning_objectives)
    misconceptions = list(kp.misconceptions)
    rng = random.Random(f"{kp.id}:{seed}")

    kind = prefer
    if kind is None:
        # 有正确说法且有 ≥2 条误区 → 选择题更耐玩；
        # 但全是选择题也单调，留 30% 出判断题换换口味。
        if objectives and len(misconceptions) >= 2:
            kind = "choice" if rng.random() < 0.7 else "judge"
        else:
            kind = "judge"

    if kind == "choice":
        if not objectives or len(misconceptions) < 2:
            raise NoQuestionError("素材不足以生成选择题")
        return _choice(kp, seed, rng, objectives, misconceptions)
    return _judge(kp, seed, rng, objectives, misconceptions)

# ------------------------------------------------------------------ 选择题

def _choice(kp: KnowledgePoint, seed: int, rng: random.Random,
            objectives: list[str], misconceptions: list[str]) -> Question:
    correct = rng.choice(objectives)
    # 干扰项：优先取本知识点的误区，不够时用同科目的其他知识点补齐
    pool = list(misconceptions)
    rng.shuffle(pool)
    n_options = min(4, max(2, len(pool) + 1))
    distractors = pool[: n_options - 1]
    if len(distractors) < n_options - 1:
        raise NoQuestionError("误区数量不足以组成选项")

    options = distractors + [correct]
    rng.shuffle(options)
    answer_index = options.index(correct)

    explain = (f"正确说法：{correct}\n"
               f"其余选项都是常见误区：\n" +
               "\n".join(f"· {d}" for d in distractors))
    return Question(
        kp_id=kp.id, kp_name=kp.name, seed=seed, kind="choice",
        prompt=f"关于「{kp.name}」，下列哪项说法是正确的？",
        options=options, answer_index=answer_index, explain=explain,
        objective=correct,
        misconception=distractors[0] if distractors else None,
    )


# ------------------------------------------------------------------ 判断题

def _judge(kp: KnowledgePoint, seed: int, rng: random.Random,
           objectives: list[str], misconceptions: list[str]) -> Question:
    """判断正误。

    必须同时有误区和正确目标：只用误区则答案恒为"错误"，只用目标则答案恒为
    "正确"，两种都能被猜中，不成其为题。
    """
    if not objectives or not misconceptions:
        raise NoQuestionError("判断题需要同时具备正确目标与常见误区，否则答案可被猜中")

    # 一半概率用误区（正确答案=说法错误），一半用正确目标（正确答案=说法正确）
    if rng.random() < 0.5:
        statement = rng.choice(misconceptions)
        answer_text = "说法错误"
        explain = (f"这是常见误区：{statement}\n"
                   f"正确理解应当避免上述说法。")
        misconception = statement
        objective = None
    else:
        statement = rng.choice(objectives)
        answer_text = "说法正确"
        explain = f"该说法与学习目标一致：{statement}"
        misconception = None
        objective = statement

    options = ["说法正确", "说法错误"]
    if rng.random() < 0.5:
        options = ["说法错误", "说法正确"]
    answer_index = options.index(answer_text)

    return Question(
        kp_id=kp.id, kp_name=kp.name, seed=seed, kind="judge",
        prompt=f"判断正误：\n「{statement}」",
        options=options, answer_index=answer_index, explain=explain,
        objective=objective, misconception=misconception,
    )
