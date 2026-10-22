"""错题归因：从做错的知识点沿先修链向上游回溯，找真正的"病根"。

算法：
1. 收集症状集 S（近期做错的知识点，带掌握概率）。
2. 对每个症状，沿 prerequisite 边向上 BFS，距离为 d 的祖先获得
   证据权重 w = (1 - p(祖先)) × decay^(d-1)——祖先越弱、离症状越近，证据越强。
3. 贪心集合覆盖：每次选能"解释"最多未覆盖症状的祖先作为根因，
   已被解释的症状从池中移除，直到症状全部覆盖或达到根因上限。

输出的每个根因附带解释路径（根因 → 症状的一条先修链）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .config import Config, DEFAULT
from .dataset import KnowledgePoint
from .graph import KnowledgeGraph
from .mastery import MasteryModel
from .state import Student


@dataclass(slots=True)
class Explanation:
    """根因如何导致某个症状。"""
    symptom_id: str
    symptom_name: str
    path: list[str]          # [root_id, ..., symptom_id]
    path_names: list[str]
    distance: int


@dataclass(slots=True)
class RootCause:
    kp: KnowledgePoint
    score: float
    p_mastery: float
    explained: list[Explanation] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class Diagnosis:
    symptom_ids: list[str]
    root_causes: list[RootCause]
    uncovered: list[str] = field(default_factory=list)   # 无法归因的症状（图上无弱前置）

    def to_dict(self) -> dict:
        return {
            "symptoms": self.symptom_ids,
            "root_causes": [{
                "kp_id": c.kp.id, "name": c.kp.name, "subject": c.kp.subject,
                "grade_band": c.kp.grade_band, "score": round(c.score, 3),
                "p_mastery": round(c.p_mastery, 3),
                "difficulty": c.kp.difficulty, "kp_type": c.kp.kp_type,
                "reason": c.reason,
                "explanations": [{
                    "symptom": e.symptom_name,
                    "path": e.path_names,
                    "distance": e.distance,
                } for e in c.explained],
            } for c in self.root_causes],
            "uncovered": self.uncovered,
        }


def diagnose(
    graph: KnowledgeGraph,
    student: Student,
    model: MasteryModel,
    symptom_ids: list[str],
    now: float,
    config: Config = DEFAULT,
) -> Diagnosis:
    symptoms = [s for s in symptom_ids if s in graph.kps]
    if not symptoms:
        return Diagnosis(symptom_ids=[], root_causes=[])

    # ---- 1. 证据收集：祖先 -> [(症状, 距离, 证据权重)]
    evidence: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
    for sid in symptoms:
        anc = graph.ancestors(sid, max_depth=config.diag_max_depth)
        # 症状自身也参与（可能病根就是它本身）
        candidates = {sid: 0, **anc}
        for aid, d in candidates.items():
            kp = graph.kps[aid]
            p = model.p(student.state_of(aid), kp, now)
            w = (1.0 - p) * (config.diag_decay ** d)
            evidence[aid].append((sid, d, w))
        # 隔离弱前置：若某祖先的所有直接前置都掌握良好，它就是干净的"断点"
        # （无需特殊处理，贪心时用 score 已能体现）

    # ---- 2. 打分：弱掌握 + 高证据的节点优先
    mastered = config.mastered_for(model.name)
    scores: dict[str, float] = {}
    for aid, items in evidence.items():
        kp = graph.kps[aid]
        p = model.p(student.state_of(aid), kp, now)
        if p >= mastered:
            continue  # 掌握良好的节点不可能是根因
        scores[aid] = sum(w for _, _, w in items)

    # ---- 3. 贪心集合覆盖
    remaining = set(symptoms)
    causes: list[RootCause] = []
    while remaining and len(causes) < config.diag_max_causes:
        best_id, best_cover = None, set()
        for aid, sc in scores.items():
            if any(c.kp.id == aid for c in causes):
                continue
            cover = {sid for sid, _, _ in evidence[aid] if sid in remaining}
            # 覆盖数优先，分数打散平局
            if len(cover) > len(best_cover) or (
                    len(cover) == len(best_cover) and cover
                    and sc > scores.get(best_id, 0)):
                best_id, best_cover = aid, cover
        if not best_id or not best_cover:
            break

        kp = graph.kps[best_id]
        p = model.p(student.state_of(best_id), kp, now)
        st = student.state_of(best_id)
        reason = _human_reason(kp, p, st.attempts, st.correct)
        cause = RootCause(kp=kp, score=scores[best_id], p_mastery=p, reason=reason)
        # 构造解释路径
        for sid, d, _ in sorted(evidence[best_id], key=lambda x: x[1]):
            if sid not in best_cover:
                continue
            path = _find_path(graph, best_id, sid, config.diag_max_depth)
            if path:
                cause.explained.append(Explanation(
                    symptom_id=sid, symptom_name=graph.kps[sid].name,
                    path=path, path_names=[graph.kps[i].name for i in path],
                    distance=d,
                ))
        causes.append(cause)
        remaining -= best_cover

    causes.sort(key=lambda c: -c.score)
    return Diagnosis(symptom_ids=symptoms, root_causes=causes,
                     uncovered=sorted(remaining))


def _find_path(graph: KnowledgeGraph, ancestor: str, symptom: str,
               max_depth: int) -> list[str] | None:
    """找 ancestor → symptom 的一条最短先修路径（沿边 from→to 走）。"""
    if ancestor == symptom:
        return [symptom]
    frontier = [(ancestor, [ancestor])]
    seen = {ancestor}
    while frontier:
        node, path = frontier.pop(0)
        if len(path) > max_depth + 1:
            continue
        for nxt in sorted(graph.dependents_of.get(node, ())):
            if nxt == symptom:
                return path + [nxt]
            if nxt not in seen and len(path) <= max_depth:
                seen.add(nxt)
                frontier.append((nxt, path + [nxt]))
    return None


def _human_reason(kp: KnowledgePoint, p: float, attempts: int, correct: int) -> str:
    if attempts == 0:
        return f"从未练习过「{kp.name}」，掌握概率仅 {p:.0%}（先验）"
    acc = correct / attempts
    return (f"「{kp.name}」练习 {attempts} 次正确率 {acc:.0%}，"
            f"当前掌握概率 {p:.0%}")
