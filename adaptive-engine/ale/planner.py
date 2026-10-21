"""学习路径规划：

- plan()：给定目标知识点，取其先修闭包，剔除已掌握节点，
  按拓扑序分阶段（stages）输出补救/学习路径。
- next_best()：无明确目标时，推荐"当前最值得学"的下一个知识点——
  在所有先修已满足的候选中，选 (到目标距离, 难度, 枢纽度) 综合最优。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config, DEFAULT
from .dataset import KnowledgePoint
from .graph import KnowledgeGraph
from .mastery import MasteryModel
from .state import Student


@dataclass(slots=True)
class PlanNode:
    kp: KnowledgePoint
    p_mastery: float
    stage: int              # 0 起步，越大越靠后
    est_minutes: int        # 粗估：难度越高估时越长（骨架级启发式）
    reason: str             # 为什么它在路径里：直接前置目标 / 为 X 打基础


@dataclass(slots=True)
class LearningPlan:
    goal: KnowledgePoint
    nodes: list[PlanNode] = field(default_factory=list)
    skipped_mastered: int = 0    # 因已掌握被剔除的节点数
    truncated: bool = False

    @property
    def total_minutes(self) -> int:
        return sum(n.est_minutes for n in self.nodes)

    def to_dict(self) -> dict:
        return {
            "goal": {"kp_id": self.goal.id, "name": self.goal.name,
                     "subject": self.goal.subject, "grade_band": self.goal.grade_band},
            "stages": [
                {"stage": s, "nodes": [{
                    "kp_id": n.kp.id, "name": n.kp.name,
                    "grades": n.kp.display_grades,
                    "p_mastery": round(n.p_mastery, 3),
                    "difficulty": n.kp.difficulty,
                    "kp_type": n.kp.kp_type,
                    "est_minutes": n.est_minutes,
                    "reason": n.reason,
                    "misconceptions": n.kp.misconceptions[:3],
                } for n in self.nodes if n.stage == s]}
                for s in sorted({n.stage for n in self.nodes})
            ],
            "total_nodes": len(self.nodes),
            "total_minutes": self.total_minutes,
            "skipped_mastered": self.skipped_mastered,
            "truncated": self.truncated,
        }


def plan(
    graph: KnowledgeGraph,
    student: Student,
    model: MasteryModel,
    goal_id: str,
    now: float,
    config: Config = DEFAULT,
) -> LearningPlan:
    if goal_id not in graph.kps:
        raise KeyError(f"目标知识点不在当前科目图中：{goal_id}")

    goal = graph.kps[goal_id]
    # 先修闭包：祖先 + 目标自身
    closure = {goal_id} | set(graph.ancestors(goal_id, max_depth=100))

    # 掌握度过滤
    weak: dict[str, float] = {}
    skipped = 0
    for kid in closure:
        kp = graph.kps[kid]
        p = model.p(student.state_of(kid), kp, now)
        if kid == goal_id or p < config.mastered_threshold:
            weak[kid] = p
        else:
            skipped += 1

    # 拓扑排序（继承图的全序）+ 分层：stage = 弱节点子图内的最长前置深度
    order = [k for k in graph.topological_order() if k in weak]
    stage_of = _stages(graph, order, weak)

    truncated = len(order) > config.plan_max_nodes
    if truncated:
        # 截断只决定"保留哪些节点"（离目标近的优先），不改变学习顺序
        kept = set(_prioritize(graph, order, goal_id, weak)[:config.plan_max_nodes])
        kept.add(goal_id)  # 目标必须在路径里
        order = [k for k in graph.topological_order() if k in kept]
        stage_of = _stages(graph, order, weak)

    nodes = []
    for kid in order:
        kp = graph.kps[kid]
        nodes.append(PlanNode(
            kp=kp, p_mastery=weak[kid], stage=stage_of[kid],
            est_minutes=_estimate_minutes(kp),
            reason=_node_reason(graph, kid, goal_id, weak),
        ))
    return LearningPlan(goal=goal, nodes=nodes, skipped_mastered=skipped,
                        truncated=truncated)


def _stages(graph: KnowledgeGraph, order: list[str],
            weak: dict[str, float]) -> dict[str, int]:
    """阶段号 = 弱节点子图在 SCC 缩点后 DAG 上的最长前置深度。

    环内节点互相依赖、无法排序，必须共享同一阶段；否则 35 节点的大环会被
    排成 35 个"阶段"（物理科目真实存在的环）。
    """
    comp_nodes: dict[int, list[str]] = {}
    for kid in order:
        comp_nodes.setdefault(graph.component_of(kid), []).append(kid)

    comp_stage: dict[int, int] = {}
    for kid in order:
        c = graph.component_of(kid)
        if c in comp_stage:
            continue
        pred_comps: set[int] = set()
        for member in comp_nodes[c]:
            for p in graph.prereqs(member):
                if p in weak:
                    pc = graph.component_of(p)
                    if pc != c:
                        pred_comps.add(pc)
        comp_stage[c] = 1 + max(
            (comp_stage[p] for p in pred_comps if p in comp_stage), default=-1)

    return {kid: comp_stage[graph.component_of(kid)] for kid in order}


def _prioritize(graph: KnowledgeGraph, order: list[str], goal_id: str,
                weak: dict[str, float]) -> list[str]:
    """截断时的取舍：离目标跳数近的优先，同跳数按图的拓扑逆序（越靠近目标越晚学）。"""
    dist = graph.ancestors(goal_id, max_depth=1000)
    d_of = {goal_id: 0, **dist}
    return sorted(order, key=lambda k: (d_of.get(k, 99), -graph.depth(k)))


def _estimate_minutes(kp: KnowledgePoint) -> int:
    """骨架级估时：基础 8 分钟 + 难度加成，程序性知识略久。"""
    base = 8 + round(kp.difficulty * 14)
    if kp.kp_type == "procedural":
        base += 4
    elif kp.kp_type == "metacognitive":
        base += 2
    return base


def _node_reason(graph: KnowledgeGraph, kid: str, goal_id: str,
                 weak: dict[str, float]) -> str:
    if kid == goal_id:
        return "🎯 目标知识点"
    # 它支撑的路径上下一个弱节点
    for nxt in sorted(graph.dependents_of.get(kid, ())):
        if nxt in weak:
            return f"为「{graph.kps[nxt].name}」打基础"
    return "目标的先修知识点"


# ---------------------------------------------------------------- 推荐下一个

@dataclass(slots=True)
class Recommendation:
    kp: KnowledgePoint
    p_mastery: float
    score: float
    rationale: str


def next_best(
    graph: KnowledgeGraph,
    student: Student,
    model: MasteryModel,
    now: float,
    goal_id: str | None = None,
    top_n: int = 5,
    config: Config = DEFAULT,
) -> list[Recommendation]:
    """推荐下一步学什么。

    候选 = 先修全部达标（或无先修）且自身未掌握的知识点。
    打分（goal_id 提供时）：score = 2.0/(1+到目标跳数) + 0.5*(1-p) + 0.1*枢纽度
    无目标时：按课程深度浅→深，鼓励顺着课本来。
    """
    hub = dict(graph.hubs(top_n=len(graph.kps)))
    dist_to_goal = graph.ancestors(goal_id, max_depth=1000) if goal_id in graph.kps else {}

    recs: list[Recommendation] = []
    for kid, kp in graph.kps.items():
        p = model.p(student.state_of(kid), kp, now)
        if p >= config.mastered_threshold:
            continue
        prereqs = {pr for pr in graph.prereqs(kid) if pr in graph.kps}
        if any(model.p(student.state_of(pr), graph.kps[pr], now)
               < config.mastered_threshold for pr in prereqs):
            continue  # 先修未满足，暂时不可学

        if goal_id and kid in dist_to_goal:
            d = dist_to_goal[kid]
            score = 2.0 / (1 + d) + 0.5 * (1 - p) + 0.1 * hub.get(kid, 0)
            rationale = (f"距目标 {d} 跳；掌握度 {p:.0%}；"
                         f"被 {hub.get(kid, 0)} 个知识点依赖")
        elif goal_id:
            # 有目标但不在目标先修链上：只给较低的"顺带学习"分
            score = 0.4 / (1 + graph.depth(kid)) + 0.3 * (1 - p)
            rationale = f"不在目标路径上；课程第 {graph.depth(kid)} 层，掌握度 {p:.0%}"
        else:
            score = 1.0 / (1 + graph.depth(kid)) + 0.3 * (1 - p)
            rationale = f"课程第 {graph.depth(kid)} 层；掌握度 {p:.0%}，适合当前进度"
        recs.append(Recommendation(kp=kp, p_mastery=p, score=score, rationale=rationale))

    recs.sort(key=lambda r: -r.score)
    return recs[:top_n]
