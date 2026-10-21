"""复习调度：SM-2 变体，按掌握度模型给出的当前 p(掌握) 调整间隔增长。"""

from __future__ import annotations

from .dataset import KnowledgePoint
from .mastery import MasteryModel
from .state import ReviewItem, Student

_MIN_EASE = 1.3
_DEFAULT_EASE = 2.3


def schedule_review(student: Student, kp: KnowledgePoint, correct: bool,
                    p_mastery: float, now: float) -> ReviewItem:
    """一次作答后更新复习项。调用方负责持久化。"""
    item = student.reviews.get(kp.id) or ReviewItem(kp_id=kp.id)

    if not correct or p_mastery < 0.5:
        # 答错或掌握很低：次日重来
        item.reps = 0
        item.ease = max(_MIN_EASE, item.ease - 0.2)
        item.interval_days = 1.0
    else:
        item.reps += 1
        if item.reps == 1:
            item.interval_days = 1.0
        elif item.reps == 2:
            item.interval_days = 3.0
        else:
            # 掌握越高，间隔放大越快；越生疏则压缩
            factor = item.ease * (0.6 + p_mastery)
            item.interval_days = min(item.interval_days * factor, 180.0)
        # 做对但掌握度一般：难度因子温和下调
        if p_mastery < 0.7:
            item.ease = max(_MIN_EASE, item.ease - 0.05)

    item.due_ts = now + item.interval_days * 86400.0
    student.reviews[kp.id] = item
    return item


def due_reviews(student: Student, now: float, limit: int = 20) -> list[ReviewItem]:
    """到期复习队列：间隔短的排前面。"""
    due = [r for r in student.reviews.values() if r.due_ts <= now]
    due.sort(key=lambda r: r.due_ts)
    return due[:limit]
