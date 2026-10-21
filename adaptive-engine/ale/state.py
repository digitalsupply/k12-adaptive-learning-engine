"""学生状态：作答历史、掌握度状态、复习队列，JSON 文件持久化。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .mastery import MasteryState


@dataclass(slots=True)
class Attempt:
    kp_id: str
    correct: bool
    ts: float


@dataclass(slots=True)
class ReviewItem:
    kp_id: str
    ease: float = 2.3          # SM-2 难度因子
    interval_days: float = 0.0
    reps: int = 0
    due_ts: float = 0.0


@dataclass(slots=True)
class Student:
    id: str
    name: str
    model: str = "bkt"                 # 使用哪个掌握度模型
    created_ts: float = field(default_factory=time.time)
    mastery: dict[str, MasteryState] = field(default_factory=dict)   # kp_id -> state
    attempts: list[Attempt] = field(default_factory=list)
    reviews: dict[str, ReviewItem] = field(default_factory=dict)     # kp_id -> item
    current_goals: dict[str, str] = field(default_factory=dict)      # (band,subject) -> kp_id

    def state_of(self, kp_id: str) -> MasteryState:
        return self.mastery.get(kp_id) or MasteryState()


class StudentStore:
    """每个学生一个 JSON 文件；骨架阶段够用，后续可平移到 SQLite。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self.root / f"{sid}.json"

    def create(self, name: str, model: str = "bkt") -> Student:
        sid = uuid.uuid4().hex[:12]
        stu = Student(id=sid, name=name, model=model)
        self.save(stu)
        return stu

    def get(self, sid: str) -> Student:
        p = self._path(sid)
        if not p.exists():
            raise KeyError(f"学生不存在：{sid}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        return Student(
            id=raw["id"], name=raw["name"], model=raw.get("model", "bkt"),
            created_ts=raw.get("created_ts", 0),
            mastery={k: MasteryState(**v) for k, v in raw.get("mastery", {}).items()},
            attempts=[Attempt(**a) for a in raw.get("attempts", [])],
            reviews={k: ReviewItem(**v) for k, v in raw.get("reviews", {}).items()},
            current_goals=raw.get("current_goals", {}),
        )

    def save(self, stu: Student) -> None:
        payload = {
            "id": stu.id, "name": stu.name, "model": stu.model,
            "created_ts": stu.created_ts,
            "mastery": {k: asdict(v) for k, v in stu.mastery.items()},
            "attempts": [asdict(a) for a in stu.attempts],
            "reviews": {k: asdict(v) for k, v in stu.reviews.items()},
            "current_goals": stu.current_goals,
        }
        self._path(stu.id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    def list_all(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            raw = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": raw["id"], "name": raw["name"],
                        "model": raw.get("model", "bkt"),
                        "attempt_count": len(raw.get("attempts", []))})
        return out
