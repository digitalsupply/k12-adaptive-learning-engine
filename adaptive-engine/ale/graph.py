"""先修知识图谱（DAG）。

边方向：from --prerequisite/leads-to--> to，即 from 是 to 的前置。

LLM 抽取的先修关系不保证无环（实测 39 个科目里 17 个存在环，个别环可达
数十个节点）。本模块以 SCC（强连通分量）缩点的方式处理环：
拓扑序、深度、环审计都基于缩点后的 DAG，保证任何输入都不会崩溃；
环内节点视为"相互依赖"，共享同一深度。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from .dataset import KnowledgePoint, Relation


@dataclass(slots=True)
class CycleInfo:
    """一个强连通分量（>1 节点，或自环）：环内知识点互相为先修。"""
    nodes: list[str]


class KnowledgeGraph:
    def __init__(self, kps: dict[str, KnowledgePoint], relations: list[Relation]):
        self.kps = kps
        self.relations = relations
        # 硬先修 DAG：只用 prerequisite 边（与诊断/规划的祖先回溯保持一致）。
        # leads-to 是较弱的"导向"边，保留在 relations 里供显式查询，但不进 DAG。
        self.prereq_of: dict[str, set[str]] = defaultdict(set)
        self.dependents_of: dict[str, set[str]] = defaultdict(set)
        for r in relations:
            if r.relation_type != "prerequisite":
                continue
            self.prereq_of[r.to_id].add(r.from_id)
            self.dependents_of[r.from_id].add(r.to_id)

        self._order: list[str] | None = None
        self._depth: dict[str, int] | None = None
        self._sccs: list[list[str]] | None = None
        self._comp_of: dict[str, int] | None = None

    # ---------------- 强连通分量（Kosaraju，迭代实现避免递归爆栈）

    def _compute_sccs(self) -> None:
        if self._sccs is not None:
            return
        nodes = list(self.kps)
        visited: set[str] = set()
        finish: list[str] = []

        # 第一遍 DFS：按完成时间排序（迭代）
        for start in nodes:
            if start in visited:
                continue
            stack = [(start, iter(sorted(self.dependents_of.get(start, ()))))]
            visited.add(start)
            while stack:
                node, it = stack[-1]
                advanced = False
                for nxt in it:
                    if nxt not in visited:
                        visited.add(nxt)
                        stack.append((nxt, iter(sorted(self.dependents_of.get(nxt, ())))))
                        advanced = True
                        break
                if not advanced:
                    stack.pop()
                    finish.append(node)

        # 第二遍：在反向图上按完成时间逆序 DFS，得到 SCC
        sccs: list[list[str]] = []
        assigned: set[str] = set()
        for start in reversed(finish):
            if start in assigned:
                continue
            comp: list[str] = []
            stack = [start]
            assigned.add(start)
            while stack:
                node = stack.pop()
                comp.append(node)
                for pre in self.prereq_of.get(node, ()):
                    if pre not in assigned:
                        assigned.add(pre)
                        stack.append(pre)
            sccs.append(comp)

        self._sccs = sccs
        self._comp_of = {n: i for i, comp in enumerate(sccs) for n in comp}

    def _comp(self, kp_id: str) -> int:
        assert self._comp_of is not None
        return self._comp_of[kp_id]

    # ---------------- 拓扑

    def topological_order(self) -> list[str]:
        """节点级拓扑序。环内节点（同一 SCC）按名称追加在所属分量位置。

        保证：返回所有节点各一次；DAG 部分的边顺序正确；环内顺序任意但稳定。
        """
        if self._order is not None:
            return self._order

        self._compute_sccs()
        assert self._sccs is not None
        comps = self._sccs
        comp_of = self._comp_of
        assert comp_of is not None

        # 分量级入度（忽略分量内部边）
        comp_pred: list[set[int]] = [set() for _ in comps]
        comp_succ: list[set[int]] = [set() for _ in comps]
        for to, pres in self.prereq_of.items():
            ct = comp_of[to]
            for p in pres:
                cp = comp_of[p]
                if cp != ct:
                    comp_pred[ct].add(cp)
                    comp_succ[cp].add(ct)

        indeg = [len(s) for s in comp_pred]
        queue = deque(sorted(c for c, d in enumerate(indeg) if d == 0))
        comp_order: list[int] = []
        while queue:
            c = queue.popleft()
            comp_order.append(c)
            for nxt in sorted(comp_succ[c]):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        # 理论上缩点后必为 DAG，这里兜底
        if len(comp_order) < len(comps):
            comp_order += [c for c in range(len(comps)) if c not in set(comp_order)]

        order: list[str] = []
        for c in comp_order:
            order.extend(sorted(comps[c]))
        self._order = order
        return order

    def components(self) -> list[list[str]]:
        """强连通分量列表（环内节点归为同一分量）。"""
        self._compute_sccs()
        assert self._sccs is not None
        return self._sccs

    def component_of(self, kp_id: str) -> int:
        """节点所属的强连通分量编号。"""
        self._compute_sccs()
        assert self._comp_of is not None
        return self._comp_of[kp_id]

    def cycles(self) -> list[CycleInfo]:
        """真正的环：SCC 大小 > 1 或自环。"""
        self._compute_sccs()
        assert self._sccs is not None
        out: list[CycleInfo] = []
        for comp in self._sccs:
            if len(comp) > 1:
                out.append(CycleInfo(nodes=sorted(comp)))
            else:
                n = comp[0]
                if n in self.prereq_of.get(n, ()):
                    out.append(CycleInfo(nodes=[n]))
        return out

    def depth(self, kp_id: str) -> int:
        """从最浅先修层算起的层级（0 = 无前置）。

        深度在 SCC 缩点后的 DAG 上计算：环内所有节点共享其分量的深度，
        因为环内节点无法排出先后。任何输入都不会抛异常。
        """
        if self._depth is None:
            self._compute_sccs()
            assert self._sccs is not None
            comp_of = self._comp_of
            assert comp_of is not None
            comps = self._sccs

            comp_pred: list[set[int]] = [set() for _ in comps]
            for to, pres in self.prereq_of.items():
                ct = comp_of[to]
                for p in pres:
                    cp = comp_of[p]
                    if cp != ct:
                        comp_pred[ct].add(cp)

            # 分量级拓扑（复用节点级拓扑序的分量顺序）
            seen: set[int] = set()
            comp_depth = [0] * len(comps)
            for node in self.topological_order():
                c = comp_of[node]
                if c in seen:
                    continue
                seen.add(c)
                comp_depth[c] = 1 + max((comp_depth[p] for p in comp_pred[c]), default=-1)

            self._depth = {n: comp_depth[comp_of[n]] for n in self.kps}
        return self._depth.get(kp_id, 0)

    # ---------------- 查询

    def prereqs(self, kp_id: str, relation_types: tuple[str, ...] = ("prerequisite",)) -> set[str]:
        """直接前置。默认只认硬前置（走预建索引）；请求 leads-to 时回退到全表扫描。"""
        if relation_types == ("prerequisite",):
            return set(self.prereq_of.get(kp_id, ()))
        result = set()
        for r in self.relations:
            if r.to_id == kp_id and r.relation_type in relation_types:
                result.add(r.from_id)
        return result

    def ancestors(self, kp_id: str, max_depth: int = 100,
                  relation_types: tuple[str, ...] = ("prerequisite",)) -> dict[str, int]:
        """全部祖先及其到 kp_id 的最短距离（层）。环内节点用 seen 去重，保证终止。"""
        out: dict[str, int] = {}
        frontier = [(kp_id, 0)]
        seen = {kp_id}
        while frontier:
            node, d = frontier.pop(0)
            if d >= max_depth:
                continue
            for p in self.prereqs(node, relation_types):
                if p not in seen:
                    seen.add(p)
                    out[p] = d + 1
                    frontier.append((p, d + 1))
        return out

    def chain_to_root(self, kp_id: str) -> list[str]:
        """沿着'最深先修'回溯出一条到根的链（用于展示典型依赖路径）。"""
        chain = [kp_id]
        seen = {kp_id}
        cur = kp_id
        while True:
            prereqs = [p for p in self.prereqs(cur) if p not in seen]
            if not prereqs:
                break
            cur = max(prereqs, key=self.depth)
            seen.add(cur)
            chain.append(cur)
        return list(reversed(chain))

    # ---------------- 统计

    def dependents(self, kp_id: str) -> set[str]:
        """直接以 kp_id 为先修的知识点（链视图的"下游"=end）。"""
        return set(self.dependents_of.get(kp_id, ()))

    def hubs(self, top_n: int = 10) -> list[tuple[str, int]]:
        """被依赖最多的前置知识点 = 教学枢纽。"""
        counted = {kp_id: len(deps) for kp_id, deps in self.dependents_of.items()}
        for kp_id in self.kps:
            counted.setdefault(kp_id, 0)
        return sorted(counted.items(), key=lambda x: -x[1])[:top_n]

    def stats(self) -> dict:
        depths = [self.depth(k) for k in self.kps]
        return {
            "kp_count": len(self.kps),
            "relation_count": len(self.relations),
            "max_depth": max(depths) if depths else 0,
            "cycles": [c.nodes for c in self.cycles()],
        }
