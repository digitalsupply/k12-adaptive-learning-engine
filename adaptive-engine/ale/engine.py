"""AdaptiveEngine 门面：组合数据集、图、掌握度模型、诊断、规划、复习。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import diagnose as _diagnose
from . import planner as _planner
from . import quiz as _quiz
from . import review as _review
from .config import Config, DEFAULT
from .dataset import Dataset
from .graph import KnowledgeGraph
from .llm import LLMClient, LLMConfig
from .mastery import create_model
from .state import Attempt, Student, StudentStore


class QHashMismatch(ValueError):
    """作答时重建的题目与当初下发的不一致（缓存被清/换了模型）。"""


@dataclass(slots=True)
class AttemptResult:
    kp_id: str
    kp_name: str
    correct: bool
    p_before: float
    p_after: float
    mastered: bool
    review_due_in_hours: float


def _make_store(config: Config):
    """按配置选择学生档案后端。

    sqlite（默认）：students 表存档，支持 owner_id 归属；同库还有账号表，由
    server 层的 Accounts 负责。首次启动若库里没有档案而旧 JSON 目录存在，
    自动把旧档案导入为演示档案（is_demo=1，对所有人只读可见）。
    file：JSON 文件版（state.StudentStore），K12_STORE=file 时启用。
    """
    if config.store_backend == "file":
        return StudentStore(config.students_dir)
    from .store_sqlite import StudentStore as _SQLiteStore
    store = _SQLiteStore(config.db_path)
    with store._conn() as con:                 # noqa: SLF001 - 初始化用
        n = con.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
    if n == 0:
        legacy = Path(config.students_dir)
        if legacy.exists():
            m = store.import_legacy_dir(legacy)
            if m:
                print(f"[store] 已从 JSON 目录导入 {m} 个旧档案（标记为演示档案）")
    return store


class _AnswerBank:
    """AI 题目的「答案索引」：qhash → 答案 / 解析 / 误区，落盘 data/question_bank.jsonl。

    LLM 题的答案只存在于生成那一次调用，出题缓存是唯一真相；万一缓存被清，
    判分重建就只能退回模板，qhash 必然失配，把学生答对的题判成「题目已刷新」。
    这里把每道 AI 题的判分所需信息按 qhash 持久化，判分时缓存缺失也能先查这里，
    保证学生答完一定看到解析而不是报错。
    """

    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("qhash"):
                        self.rows[row["qhash"]] = row
            except OSError:
                pass

    def put(self, q: _quiz.Question) -> None:
        row = {
            "qhash": q.qhash, "kp_id": q.kp_id, "seed": q.seed,
            "model": q.model, "answer_index": q.answer_index,
            "explain": q.explain, "objective": q.objective,
            "misconception": q.misconception, "kind": q.kind,
            "saved_ts": round(time.time()),
        }
        old = self.rows.get(row["qhash"])
        if old == row:
            return
        self.rows[row["qhash"]] = row
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[answer-bank] 写入失败：{e}", flush=True)

    def get(self, qhash: str) -> dict[str, Any] | None:
        return self.rows.get(qhash)


class AdaptiveEngine:
    def __init__(self, config: Config | None = None):
        self.config = config or DEFAULT
        self.dataset = Dataset(self.config)
        self.store = _make_store(self.config)
        self._qbank = _AnswerBank(self.config.db_path.parent / "question_bank.jsonl")
        self.llm = LLMClient(LLMConfig.from_env())
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
        kp = self.dataset.get_kp(kp_id)

        def _apply(student: Student) -> AttemptResult:
            model = self.model_for(student)
            state = student.state_of(kp_id)
            p_before = model.p(state, kp, now)
            new_state = model.update(state, kp, correct, now)
            p_after = new_state.p_mastery
            student.mastery[kp_id] = new_state
            student.attempts.append(Attempt(kp_id=kp_id, correct=correct, ts=now))
            self._truncate_history(student)
            item = _review.schedule_review(student, kp, correct, p_after, now)
            return AttemptResult(
                kp_id=kp_id, kp_name=kp.name, correct=correct,
                p_before=p_before, p_after=p_after,
                mastered=p_after >= self.config.mastered_for(model.name),
                review_due_in_hours=item.due_ts / 3600 - now / 3600,
            )

        # 锁内完成读-改-写：并发作答不会互相覆盖
        result, _ = self.store.mutate(sid, _apply)
        return result

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
        graph = self.graph_of_kp(goal_id)

        def _apply(student: Student) -> _planner.LearningPlan:
            plan = _planner.plan(graph, student, self.model_for(student),
                                 goal_id, now, self.config)
            # 记住目标，便于首页回访
            kp = graph.kps[goal_id]
            student.current_goals[f"{kp.grade_band}/{kp.subject}"] = goal_id
            return plan

        # 锁内读-改-写：目标记忆不会与并发作答互相覆盖
        plan, _ = self.store.mutate(sid, _apply)
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

    # ---------------- 出题 / 作答

    def make_question(self, kp_id: str, seed: int = 0,
                      prefer: str | None = None,
                      for_answer: bool = False,
                      force_template: bool = False) -> _quiz.Question:
        """出题：缓存 → LLM 命题 → 标注模板，三级回退。

        同 (kp_id, seed) 的题目永远相同（缓存保证），作答校验才能成立。
        for_answer=True 是作答校验模式：绝不调 LLM（重建必须是当初下发的那
        道，且不能产生第二次生成开销），但仍要带上 llm 客户端——缓存键含模型
        名，少了它会读不到当时的 LLM 缓存、错退回模板导致 qhash 失配 409。
        force_template=True 跳过 LLM，用于测试。
        """
        kp = self.dataset.get_kp(kp_id)
        llm = None if force_template or prefer else self.llm
        q = _quiz.generate_cached(
            kp, seed=seed, cache_dir=self.config.quiz_cache_dir, llm=llm,
            min_assessability=self.config.quiz_min_assessability,
            rebuild=for_answer or force_template)
        if prefer:                  # 强制题型（测试用）
            q = _quiz.generate(kp, seed=seed, prefer=prefer,
                               min_assessability=self.config.quiz_min_assessability)
        if q.source in ("llm", "llm-cache"):
            self._qbank.put(q)      # AI 题答案落库，判分兜底用
        return q

    def set_llm_config(self, config: LLMConfig) -> None:
        """运行时热切换 LLM 配置（前端「LLM 设置」保存后调用）。

        缓存键含模型名 → 换模型自动隔离旧缓存，不影响判分一致性
        （qhash 判分用的答案库与题目缓存都按各自键取，不互相污染）。
        """
        self.llm = LLMClient(config)

    def answer_question(self, sid: str, kp_id: str, seed: int, choice: int,
                        now: float | None = None,
                        qhash: str | None = None) -> dict[str, Any]:
        """提交作答：服务端重建题目 → 核对选项哈希 → 判分 → 写掌握度。

        题目不存服务端，靠 (kp_id, seed) 从出题缓存重建；qhash 由前端随答案
        带回，防止题目在两次请求之间被刷新却仍按旧题判分。
        重建失败（缓存被清/换了模型）时先查答案库（qhash → 答案 + 解析）；
        答案库也没有，才是真正的题目刷新，报 409 让前端重新抽题。
        """
        now = time.time() if now is None else now
        question = fallback = None
        if qhash is not None:
            fallback = self._qbank.get(qhash)
            # qhash 绑定 (kp_id, seed, options)，只要核对 kp_id 即可：
            # 有些客户端会漏发/错发 seed（默认为 0），若把 seed 也拿来
            # 校验，反而会把账面正确的作答拒成 409。
            if fallback is not None and fallback.get("kp_id") != kp_id:
                fallback = None
        try:
            question = self.make_question(kp_id, seed=seed, for_answer=True)
        except _quiz.NoQuestionError:
            if fallback is None:
                raise
        if fallback is not None and (question is None or question.qhash != qhash):
            # 缓存缺失/失配，但答案库有这道题 → 按库判分，解析照样给足
            kp = self.dataset.get_kp(kp_id)
            if not (0 <= choice < 4):
                raise ValueError(f"选项越界：{choice}")
            correct = choice == fallback["answer_index"]
            result = self.record_attempt(sid, kp_id, correct, now)
            out: dict[str, Any] = {
                "kp_id": kp_id,
                "kp_name": kp.name,
                "correct": correct,
                "choice": choice,
                "correct_index": fallback["answer_index"],
                "correct_option": None,      # 题面已不在，前端只标对错字母
                "explain": fallback.get("explain") or "（解析缺失）",
                "objective": fallback.get("objective"),
                "misconception": fallback.get("misconception"),
                "source": "llm-cache",
                "p_before": round(result.p_before, 3),
                "p_after": round(result.p_after, 3),
                "mastered": result.mastered,
                "kind": fallback.get("kind", "choice"),
            }
            if not correct:
                d = self.diagnose(sid, [kp_id], now)
                out["diagnosis"] = d.to_dict()["root_causes"][:3]
            return out
        if question is None:
            raise _quiz.NoQuestionError(f"该知识点无法重建题目：{kp_id}")
        if qhash is not None and question.qhash != qhash:
            print(f"[qhash-mismatch] sid={sid} kp={kp_id} seed={seed} "
                  f"给出={qhash} 重建={question.qhash}（答案库上/下线后均应查得）",
                  flush=True)
            raise QHashMismatch("题目已刷新，请重新获取再作答")
        if not (0 <= choice < len(question.options)):
            raise ValueError(f"选项越界：{choice}")

        correct = choice == question.answer_index
        result = self.record_attempt(sid, kp_id, correct, now)

        out: dict[str, Any] = {
            "kp_id": kp_id,
            "kp_name": question.kp_name,
            "correct": correct,
            "choice": choice,
            "correct_index": question.answer_index,
            "correct_option": question.options[question.answer_index],
            "explain": question.explain,
            "objective": question.objective,
            "misconception": question.misconception,
            "p_before": round(result.p_before, 3),
            "p_after": round(result.p_after, 3),
            "mastered": result.mastered,
            "kind": question.kind,
        }
        if not correct:
            d = self.diagnose(sid, [kp_id], now)
            out["diagnosis"] = d.to_dict()["root_causes"][:3]
        return out

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
                "mastered": model.p(st, kp, now) >= self.config.mastered_for(model.name),
            }
        return out

    def record_attempts(self, sid: str, attempts: list[tuple[str, bool]],
                        now: float | None = None) -> list[AttemptResult]:
        """批量记录作答：整个批次只写一次档案。

        逐条记录会让每次作答都触发一次全量 JSON 重写，批量导入时是 O(n) 次
        磁盘写；这里把写入合并到最后一次。
        """
        now = now or time.time()
        kps = {kp_id: self.dataset.get_kp(kp_id) for kp_id, _ in attempts}

        def _apply(student: Student) -> list[AttemptResult]:
            model = self.model_for(student)
            results: list[AttemptResult] = []
            for kp_id, correct in attempts:
                kp = kps[kp_id]
                state = student.state_of(kp_id)
                p_before = model.p(state, kp, now)
                new_state = model.update(state, kp, correct, now)
                p_after = new_state.p_mastery
                student.mastery[kp_id] = new_state
                student.attempts.append(Attempt(kp_id=kp_id, correct=correct, ts=now))
                item = _review.schedule_review(student, kp, correct, p_after, now)
                results.append(AttemptResult(
                    kp_id=kp_id, kp_name=kp.name, correct=correct,
                    p_before=p_before, p_after=p_after,
                    mastered=p_after >= self.config.mastered_for(model.name),
                    review_due_in_hours=item.due_ts / 3600 - now / 3600,
                ))
            self._truncate_history(student)
            return results

        results, _ = self.store.mutate(sid, _apply)
        return results

    @staticmethod
    def _truncate_history(student: Student) -> None:
        """历史截断，防止 JSON 无限膨胀（骨架策略）。"""
        if len(student.attempts) > 5000:
            student.attempts = student.attempts[-3000:]

    def simulate_learner(self, sid: str, kp_ids: list[str],
                         ability: float = 0.6, noise: float = 0.15,
                         seed: int | None = None,
                         now: float | None = None) -> list[AttemptResult]:
        """模拟学生练习（演示/压测用）：正确率 ~ ability - difficulty + noise。

        seed 固定随机流，使结果可复现；不传则使用独立随机源，不污染全局
        random 状态（此前全局 random 未播种，导致同一份数据每次跑出的
        作答记录不同，测试因此不稳定）。
        """
        import random
        rng = random.Random(seed)
        now = now or time.time()
        # 先算好每题的作答结果，再整批落盘（N 次磁盘写 → 1 次）
        pairs: list[tuple[str, bool]] = []
        for kid in kp_ids:
            kp = self.dataset.get_kp(kid)
            p_correct = min(max(ability - kp.difficulty * 0.6 + rng.uniform(-noise, noise),
                                0.05), 0.95)
            pairs.append((kid, rng.random() < p_correct))
        return self.record_attempts(sid, pairs, now=now)
