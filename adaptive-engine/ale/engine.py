"""AdaptiveEngine 门面：组合数据集、图、掌握度模型、诊断、规划、复习。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import diagnose as _diagnose
from . import planner as _planner
from . import review as _review
from .config import Config, DEFAULT
from .dataset import Dataset, KnowledgePoint
from .graph import KnowledgeGraph
from .mastery import create_model
from .state import Attempt, Student, StudentStore


@dataclass(slots=True)
class AttemptResult:
    kp_id: str
    kp_name: str
    correct: bool
    p_before: float
    p_after: float
    mastered: bool
    review_due_in_hours: float


class AdaptiveEngine:
    def __init__(self, config: Config | None = None):
        self.config = config or DEFAULT
        self.dataset = Dataset(self.config)
        self.store = StudentStore(self.config.students_dir)
        self._graphs: dict[tuple[str, str], KnowledgeGraph] = {}

    # ---------------- 图

    def graph(self, band: str, subject: str) -> KnowledgeGraph:
        key = (band, subject)
        if key not in self._graphs:
            kps, rels = self.dataset.group(band, subject)
            self._graphs[key] = KnowledgeGraph(kps, rels)
        return self._graphs[key]

    def graph_of_kp(self, kp_id: str) -> KnowledgeGraph:
        band, subject = self.dataset.locate(kp_id)
        return self.graph(band, subject)

    def model_for(self, student: Student):
        forget = (self.config.bkt_forget_per_day if student.model == "bkt"
                  else self.config.elo_forget_per_day)
        return create_model(student.model, forget)

    # ---------------- 学生

    def create_student(self, name: str, model: str = "bkt") -> Student:
        return self.store.create(name, model=model)

    def get_student(self, sid: str) -> Student:
        return self.store.get(sid)

    # ---------------- 核心动作

    def record_attempt(self, sid: str, kp_id: str, correct: bool,
                       now: float | None = None) -> AttemptResult:
        """记录一次作答，驱动掌握度 + 复习队列。"""
        now = now or time.time()
        student = self.get_student(sid)
        kp = self.dataset.get_kp(kp_id)
        model = self.model_for(student)

        state = student.state_of(kp_id)
        p_before = model.p(state, kp, now)
        new_state = model.update(state, kp, correct, now)
        p_after = new_state.p_mastery
        student.mastery[kp_id] = new_state
        student.attempts.append(Attempt(kp_id=kp_id, correct=correct, ts=now))
        # 历史截断，防止 JSON 无限膨胀（骨架策略）
        if len(student.attempts) > 5000:
            student.attempts = student.attempts[-3000:]

        item = _review.schedule_review(student, kp, correct, p_after, now)
        self.store.save(student)

        return AttemptResult(
            kp_id=kp_id, kp_name=kp.name, correct=correct,
            p_before=p_before, p_after=p_after,
            mastered=p_after >= self.config.mastered_threshold,
            review_due_in_hours=item.due_ts / 3600 - now / 3600,
        )

    def diagnose(self, sid: str, symptom_ids: list[str],
                 now: float | None = None) -> _diagnose.Diagnosis:
        now = now or time.time()
        student = self.get_student(sid)
        graph = self.graph_of_kp(symptom_ids[0]) if symptom_ids else None
        if graph is None:
            return _diagnose.Diagnosis(symptom_ids=[], root_causes=[])
        # 只保留同科目的症状
        local = [s for s in symptom_ids if s in graph.kps]
        return _diagnose.diagnose(graph, student, self.model_for(student),
                                  local, now, self.config)

    def diagnose_recent_failures(self, sid: str, window: int = 20,
                                 now: float | None = None) -> _diagnose.Diagnosis:
        """对最近 window 次作答中的错题自动归因。"""
        now = now or time.time()
        student = self.get_student(sid)
        recent = [a for a in student.attempts if a.correct is False][-window:]
        if not recent:
            return _diagnose.Diagnosis(symptom_ids=[], root_causes=[])
        # 按科目分桶，各自诊断后按分数合并
        by_subject: dict[tuple[str, str], list[str]] = {}
        for a in recent:
            loc = self.dataset.locate(a.kp_id)
            by_subject.setdefault(loc, []).append(a.kp_id)
        merged: list[_diagnose.RootCause] = []
        symptoms: list[str] = []
        for (band, subject), ids in by_subject.items():
            d = _diagnose.diagnose(self.graph(band, subject), student,
                                   self.model_for(student),
                                   list(dict.fromkeys(ids)), now, self.config)
            symptoms += d.symptom_ids
            merged += d.root_causes
        merged.sort(key=lambda c: -c.score)
        return _diagnose.Diagnosis(symptom_ids=symptoms,
                                   root_causes=merged[:self.config.diag_max_causes])

    def make_plan(self, sid: str, goal_id: str,
                  now: float | None = None) -> _planner.LearningPlan:
        now = now or time.time()
        student = self.get_student(sid)
        graph = self.graph_of_kp(goal_id)
        plan = _planner.plan(graph, student, self.model_for(student),
                             goal_id, now, self.config)
        # 记住目标，便于首页回访
        student.current_goals[f"{graph.kps[goal_id].grade_band}/{graph.kps[goal_id].subject}"] = goal_id
        self.store.save(student)
        return plan

    def recommend(self, sid: str, goal_id: str | None = None,
                  top_n: int = 5, now: float | None = None) -> list[_planner.Recommendation]:
        now = now or time.time()
        student = self.get_student(sid)
        if goal_id:
            graph = self.graph_of_kp(goal_id)
        else:
            # 无目标时用该学生最近练习的科目
            if student.attempts:
                band, subject = self.dataset.locate(student.attempts[-1].kp_id)
            else:
                groups = self.dataset.list_groups()
                band, subject = groups[0]["band"], groups[0]["subject"]
            graph = self.graph(band, subject)
            goal_id = None
        return _planner.next_best(graph, student, self.model_for(student),
                                  now, goal_id, top_n, self.config)

    def due_reviews(self, sid: str, now: float | None = None):
        now = now or time.time()
        student = self.get_student(sid)
        return _review.due_reviews(student, now)

    # ---------------- 概览

    def kp_detail(self, kp_id: str, sid: str | None = None,
                  now: float | None = None) -> dict:
        """知识点详情 +（可选）该学生的掌握状态。"""
        now = now or time.time()
        kp = self.dataset.get_kp(kp_id)
        graph = self.graph_of_kp(kp_id)
        out: dict = {
            "kp_id": kp.id, "name": kp.name, "subject": kp.subject,
            "grade_band": kp.grade_band, "grades": kp.display_grades,
            "kp_type": kp.kp_type, "kp_type_label": kp.kp_type_label, "difficulty": kp.difficulty,
            "confidence": kp.confidence, "summary": kp.summary,
            "aliases": kp.aliases,
            "learning_objectives": kp.learning_objectives,
            "misconceptions": kp.misconceptions,
            "assessability": kp.assessability,
            "textbooks": [{"title": t.get("textbook_title"),
                           "grade": t.get("grade"), "volume": t.get("volume"),
                           "chapter_path": t.get("chapter_path")}
                          for t in kp.textbooks],
            "chain_to_root": [graph.kps[i].name for i in graph.chain_to_root(kp_id)],
            "dependents": sorted(graph.kps[i].name
                                 for i in graph.dependents_of.get(kp_id, set())),
            "depth": graph.depth(kp_id),
        }
        if sid:
            student = self.get_student(sid)
            model = self.model_for(student)
            st = student.state_of(kp_id)
            out["mastery"] = {
                "p": round(model.p(st, kp, now), 3),
                "attempts": st.attempts, "correct": st.correct,
                "mastered": model.p(st, kp, now) >= self.config.mastered_threshold,
            }
        return out

    def simulate_learner(self, sid: str, kp_ids: list[str],
                         ability: float = 0.6, noise: float = 0.15,
                         now: float | None = None) -> list[AttemptResult]:
        """模拟学生练习（演示/压测用）：正确率 ~ ability - difficulty + noise。"""
        import random
        now = now or time.time()
        results = []
        for kid in kp_ids:
            kp = self.dataset.get_kp(kid)
            p_correct = min(max(ability - kp.difficulty * 0.6 + random.uniform(-noise, noise),
                                0.05), 0.95)
            results.append(self.record_attempt(sid, kid, random.random() < p_correct, now))
        return results
