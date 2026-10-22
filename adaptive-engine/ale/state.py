"""学生状态：作答历史、掌握度状态、复习队列，JSON 文件持久化。

持久化策略（骨架阶段，但保证不丢数据）：
- 原子写：先写同目录临时文件，再 os.replace 覆盖，中途崩溃不会留下半截 JSON；
- 文件锁：读写都加 fcntl 独占锁，避免多进程（uvicorn workers）并发作答时
  互相覆盖——读-改-写会丢掉后写的那次更新；
- 损坏档案：get() 抛 KeyError（而不是 JSONDecodeError），API 层能给出
  "档案损坏"而不是 500。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .mastery import MasteryState

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows 兜底
    fcntl = None  # type: ignore[assignment]


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

    # 归属：None=未归属（演示/CLI 创建），账号体系建立后由 SQLite store 填写
    owner_id: str | None = None
    is_demo: bool = False


class _FileLock:
    """基于 fcntl.flock 的跨进程文件锁；fcntl 不可用时退化为空操作。"""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False


class StudentStore:
    """每个学生一个 JSON 文件；骨架阶段够用，后续可平移到 SQLite。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self.root / f"{sid}.json"

    def _lock_path(self, sid: str) -> Path:
        return self.root / f"{sid}.lock"

    def create(self, name: str, model: str = "bkt") -> Student:
        sid = uuid.uuid4().hex[:12]
        stu = Student(id=sid, name=name, model=model)
        self.save(stu)
        return stu

    def get(self, sid: str) -> Student:
        p = self._path(sid)
        if not p.exists():
            raise KeyError(f"学生不存在：{sid}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise KeyError(f"学生档案损坏，无法解析：{sid}（{p}）：{e}") from e
        if not isinstance(raw, dict):
            raise KeyError(f"学生档案格式非法：{sid}（{p}）")
        return Student(
            id=raw["id"], name=raw["name"], model=raw.get("model", "bkt"),
            created_ts=raw.get("created_ts", 0),
            mastery={k: MasteryState(**v) for k, v in raw.get("mastery", {}).items()},
            attempts=[Attempt(**a) for a in raw.get("attempts", [])],
            reviews={k: ReviewItem(**v) for k, v in raw.get("reviews", {}).items()},
            current_goals=raw.get("current_goals", {}),
            owner_id=raw.get("owner_id"), is_demo=bool(raw.get("is_demo")),
        )

    def save(self, stu: Student) -> None:
        """原子写入：临时文件 + os.replace。

        直接 write_text 会先截断原文件，写入过程中崩溃就只剩半截 JSON，
        学生档案从此无法读取。os.replace 在同一文件系统上是原子操作。
        """
        with _FileLock(self._lock_path(stu.id)):
            self._save_unlocked(stu)

    def _save_unlocked(self, stu: Student) -> None:
        """写入档案（调用方已持有锁，或处于单线程场景）。"""
        payload = {
            "id": stu.id, "name": stu.name, "model": stu.model,
            "created_ts": stu.created_ts,
            "mastery": {k: asdict(v) for k, v in stu.mastery.items()},
            "attempts": [asdict(a) for a in stu.attempts],
            "reviews": {k: asdict(v) for k, v in stu.reviews.items()},
            "current_goals": stu.current_goals,
            "owner_id": stu.owner_id, "is_demo": stu.is_demo,
        }
        target = self._path(stu.id)
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, target)
        except BaseException:
            # 失败时清掉半截临时文件；原档案因为走 os.replace 而毫发无损
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def mutate(self, sid: str, fn):
        """在文件锁内完成「读 → 改 → 写」，避免并发作答时丢更新。

        fn 接收 Student 对象并就地修改；返回 (fn 的返回值, 学生对象)。
        """
        with _FileLock(self._lock_path(sid)):
            stu = self.get(sid)
            result = fn(stu)
            self._save_unlocked(stu)
            return result, stu

    def list_all(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue          # 损坏/半写的档案直接跳过，不让列表接口整个挂掉
            out.append({"id": raw["id"], "name": raw["name"],
                        "model": raw.get("model", "bkt"),
                        "attempt_count": len(raw.get("attempts", []))})
        return out
