"""FastAPI 服务：把 AdaptiveEngine 暴露为 REST API，并托管 web/ 前端。

启动：python server.py  （或 uvicorn server:app --reload --port 8000）
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ale.config import FIGURES_DIR, WEB_DIR
from ale.engine import AdaptiveEngine
from ale.figures import FigureRegistry

app = FastAPI(title="K12 自适应学习引擎", version="0.1.0")
# CORS 默认放开（本地演示）；部署到公网时用 K12_CORS_ORIGINS 指定来源列表。
_cors = os.environ.get("K12_CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors.split(",") if o.strip()],
    allow_methods=["*"], allow_headers=["*"],
)

eng = AdaptiveEngine()
figures = FigureRegistry()


# ---------------- 请求体

class AttemptIn(BaseModel):
    kp_id: str
    correct: bool


class AttemptsIn(BaseModel):
    attempts: list[AttemptIn] = Field(..., min_length=1, max_length=5000)


class AnswerIn(BaseModel):
    kp_id: str
    seed: int = 0
    choice: int = Field(..., ge=0, le=9)


class DiagnoseIn(BaseModel):
    kp_ids: list[str] = Field(..., min_length=1)


class PlanIn(BaseModel):
    goal_id: str


class StudentIn(BaseModel):
    name: str
    model: str = "bkt"


class SimulateIn(BaseModel):
    kp_ids: list[str]
    ability: float = 0.6
    noise: float = 0.15
    seed: int | None = None


# ---------------- 元数据

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """把阈值下发给前端，避免前后端各写一份魔法数字。"""
    cfg = eng.config
    return {
        "mastered_threshold": cfg.mastered_for("bkt"),
        "elo_mastered_threshold": cfg.mastered_for("elo"),
        "low_confidence_hint": cfg.low_confidence_hint,
        "diag_max_depth": cfg.diag_max_depth,
        "diag_decay": cfg.diag_decay,
        "plan_max_nodes": cfg.plan_max_nodes,
    }


@app.get("/api/groups")
def list_groups() -> list[dict[str, Any]]:
    return eng.dataset.list_groups()


@app.get("/api/search")
def search(q: str, band: str | None = None, subject: str | None = None,
           limit: int = 20) -> list[dict[str, Any]]:
    return [{"kp_id": kp.id, "name": kp.name, "subject": kp.subject,
             "grade_band": kp.grade_band, "grades": kp.display_grades,
             "difficulty": kp.difficulty, "kp_type": kp.kp_type,
             "kp_type_label": kp.kp_type_label}
            for kp in eng.dataset.search(q, band, subject, limit)]


@app.get("/api/kp/{kp_id}")
def kp_detail(kp_id: str, student: str | None = None) -> dict[str, Any]:
    try:
        out = eng.kp_detail(kp_id, sid=student)
    except KeyError as e:
        raise HTTPException(404, str(e))
    figure = figures.for_kp(kp_id)
    if figure:
        out["figure"] = figure
    return out


@app.get("/api/figures")
def list_figures() -> list[dict[str, Any]]:
    return figures.all()


@app.get("/api/graph/{band}/{subject}")
def graph_view(band: str, subject: str, student: str | None = None,
               depth: int | None = None, root: str | None = None,
               max_nodes: int | None = None,
               spine: bool = False) -> dict[str, Any]:
    """图谱可视化数据：节点（含掌握度着色）+ 边。学生指定时节点带 p。

    大科目（小学语文 4851 个知识点）整图返回会产生 1.4 MB 响应和上万个
    SVG 元素，前端明显卡顿。因此提供几个收窄参数：
    - depth: 只保留深度 ≤ N 的节点；
    - root: 只保留该节点所在连通分量（先修 + 后继双向可达）；
    - spine: 只保留参与先修关系的节点（丢掉没有先修也没有后继的孤立条目）；
    - max_nodes: 节点数硬上限，优先保留参与先修关系的节点。
    响应里的 truncated / total_nodes / total_edges 告诉前端是否被裁剪。
    """
    try:
        g = eng.graph(band, subject)
    except KeyError as e:
        raise HTTPException(404, str(e))
    now = time.time()
    student_obj = None
    if student:
        try:
            student_obj = eng.get_student(student)
        except KeyError:
            raise HTTPException(404, f"学生不存在：{student}")
    model = eng.model_for(student_obj) if student_obj else None

    total_nodes, total_edges = len(g.kps), len(g.relations)

    def has_rel(kid: str) -> bool:
        return bool(g.prereq_of.get(kid)) or bool(g.dependents_of.get(kid))

    # ---- 收窄：先修 + 后继双向可达的连通分量
    keep: set[str] | None = None
    if root:
        if root not in g.kps:
            raise HTTPException(404, f"知识点不存在：{root}")
        keep = _connected_component(g, root)

    # ---- 收窄：深度上限
    if depth is not None:
        depth = max(0, depth)
        dset = {kid for kid in g.kps if g.depth(kid) <= depth}
        keep = dset if keep is None else (keep & dset)

    # ---- 收窄：只保留有先修关系的主干
    if spine:
        sset = {kid for kid in g.kps if has_rel(kid)}
        keep = sset if keep is None else (keep & sset)

    # ---- 收窄：节点上限
    truncated = False
    if max_nodes is not None and max_nodes >= 0:
        pool = [k for k in g.topological_order() if keep is None or k in keep]
        if len(pool) > max_nodes:
            truncated = True
            # 逐个取完整的连通分量：从大到小装，装不下的整块跳过。
            # 按拓扑序截断会切出"一堆孤立点 + 一条边"的碎片，没有教学意义。
            keep = _greedy_components(g, pool, max_nodes)
        elif keep is None:
            keep = set(pool)

    nodes = []
    for kid, kp in g.kps.items():
        if keep is not None and kid not in keep:
            continue
        node: dict[str, Any] = {
            "id": kid, "name": kp.name, "depth": g.depth(kid),
            "difficulty": kp.difficulty, "kp_type": kp.kp_type,
            "grades": kp.display_grades,
        }
        if student_obj and model:
            st = student_obj.state_of(kid)
            p = model.p(st, kp, now)
            node["p"] = round(p, 3)
            node["attempts"] = st.attempts
            node["mastered"] = p >= eng.config.mastered_for(model.name)
        node["confidence"] = kp.confidence
        node["low_confidence"] = kp.confidence < eng.config.low_confidence_hint
        nodes.append(node)

    edges = [{"source": r.from_id, "target": r.to_id, "type": r.relation_type}
             for r in g.relations
             if keep is None or (r.from_id in keep and r.to_id in keep)]
    if len(nodes) < total_nodes:
        truncated = True
    return {"band": band, "subject": subject, "nodes": nodes, "edges": edges,
            "stats": g.stats(), "hubs": g.hubs(8),
            "total_nodes": total_nodes, "total_edges": total_edges,
            "truncated": truncated}


def _connected_component(g, root: str) -> set[str]:
    """root 所在的连通分量（先修边 + 后继边都算，双向可达）。"""
    seen = {root}
    frontier = [root]
    while frontier:
        node = frontier.pop()
        neighbours = g.prereq_of.get(node, set()) | g.dependents_of.get(node, set())
        for nxt in neighbours:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


def _connected_components(g) -> list[set[str]]:
    """图的所有连通分量（先修边 + 后继边双向可达），按大小降序。"""
    seen: set[str] = set()
    comps: list[set[str]] = []
    for kid in g.kps:
        if kid in seen:
            continue
        comp = _connected_component(g, kid)
        seen |= comp
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


def _greedy_components(g, pool: list[str], max_nodes: int) -> set[str]:
    """在 pool 里按连通分量从大到小取，直到装满 max_nodes。

    装不下的整块跳过（宁缺毋滥），保证留下的子图内部是连贯的。
    """
    allowed = set(pool)
    chosen: set[str] = set()
    for comp in _connected_components(g):
        part = comp & allowed          # 分量可能已被 depth/spine 裁过，只取交集
        if not part:
            continue
        if len(chosen) + len(part) > max_nodes:
            continue
        chosen |= part
        if len(chosen) >= max_nodes:
            break
    # 所有分量都装不下时，退回按拓扑序取（至少给用户一些内容）
    if not chosen:
        chosen = set(pool[:max_nodes])
    return chosen


# ---------------- 学生

@app.post("/api/students")
def create_student(body: StudentIn) -> dict[str, Any]:
    if body.model not in ("bkt", "elo"):
        raise HTTPException(400, "model 只能是 bkt 或 elo")
    stu = eng.create_student(body.name, model=body.model)
    return {"id": stu.id, "name": stu.name, "model": stu.model}


@app.get("/api/students")
def list_students() -> list[dict[str, Any]]:
    return eng.store.list_all()


@app.get("/api/students/{sid}")
def student_overview(sid: str) -> dict[str, Any]:
    try:
        stu = eng.get_student(sid)
    except KeyError:
        raise HTTPException(404, "学生不存在")
    model = eng.model_for(stu)
    now = time.time()
    mastered = 0
    touched = 0
    for kid, st in stu.mastery.items():
        touched += 1
        try:
            kp = eng.dataset.get_kp(kid)
        except KeyError:
            continue
        if model.p(st, kp, now) >= eng.config.mastered_for(model.name):
            mastered += 1
    return {"id": stu.id, "name": stu.name, "model": stu.model,
            "attempt_count": len(stu.attempts), "touched_kp": touched,
            "mastered_kp": mastered,
            "due_review_count": len(eng.due_reviews(sid, now)),
            "current_goals": stu.current_goals}


# ---------------- 核心动作

@app.post("/api/students/{sid}/attempt")
def record_attempt(sid: str, body: AttemptIn) -> dict[str, Any]:
    try:
        r = eng.record_attempt(sid, body.kp_id, body.correct)
    except KeyError as e:
        raise HTTPException(404, str(e))
    out = {"kp_id": r.kp_id, "kp_name": r.kp_name, "correct": r.correct,
           "p_before": round(r.p_before, 3), "p_after": round(r.p_after, 3),
           "mastered": r.mastered,
           "review_due_in_hours": round(r.review_due_in_hours, 1)}
    if not r.correct:
        d = eng.diagnose(sid, [r.kp_id])
        out["diagnosis"] = d.to_dict()["root_causes"][:3]
    return out


@app.get("/api/quiz/{kp_id}")
def make_quiz(kp_id: str, seed: int = 0) -> dict[str, Any]:
    """出一道题。响应里不含正确答案——判分在 POST /api/students/{sid}/answer 里做。"""
    try:
        q = eng.make_question(kp_id, seed=seed)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return q.to_dict()


@app.post("/api/students/{sid}/answer")
def answer_quiz(sid: str, body: AnswerIn) -> dict[str, Any]:
    """提交作答：服务端重建题目判分 → 写入掌握度 → 答错时自动归因。"""
    try:
        return eng.answer_question(sid, body.kp_id, body.seed, body.choice)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/students/{sid}/attempts")
def record_attempts(sid: str, body: AttemptsIn) -> dict[str, Any]:
    """批量记录作答：整个批次只写一次档案（逐条记录是 O(n) 次磁盘写）。"""
    try:
        results = eng.record_attempts(sid, [(a.kp_id, a.correct) for a in body.attempts])
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"count": len(results),
            "correct": sum(1 for r in results if r.correct),
            "mastered": sum(1 for r in results if r.mastered),
            "results": [{"kp_id": r.kp_id, "p_before": round(r.p_before, 3),
                         "p_after": round(r.p_after, 3), "correct": r.correct,
                         "mastered": r.mastered} for r in results]}


@app.post("/api/students/{sid}/simulate")
def simulate(sid: str, body: SimulateIn) -> dict[str, Any]:
    try:
        results = eng.simulate_learner(sid, body.kp_ids, ability=body.ability,
                                       noise=body.noise, seed=body.seed)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"count": len(results),
            "correct": sum(1 for r in results if r.correct),
            "mastered": sum(1 for r in results if r.mastered)}


@app.post("/api/students/{sid}/diagnose")
def diagnose(sid: str, body: DiagnoseIn) -> dict[str, Any]:
    try:
        return eng.diagnose(sid, body.kp_ids).to_dict()
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/students/{sid}/diagnose/recent")
def diagnose_recent(sid: str) -> dict[str, Any]:
    return eng.diagnose_recent_failures(sid).to_dict()


@app.post("/api/students/{sid}/plan")
def make_plan(sid: str, body: PlanIn) -> dict[str, Any]:
    try:
        return eng.make_plan(sid, body.goal_id).to_dict()
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/students/{sid}/recommend")
def recommend(sid: str, goal: str | None = None, top_n: int = 5) -> dict[str, Any]:
    try:
        recs = eng.recommend(sid, goal_id=goal, top_n=top_n)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"recommendations": [{
        "kp_id": r.kp.id, "name": r.kp.name, "subject": r.kp.subject,
        "grade_band": r.kp.grade_band, "grades": r.kp.display_grades,
        "p_mastery": round(r.p_mastery, 3), "score": round(r.score, 3),
        "rationale": r.rationale,
    } for r in recs]}


@app.get("/api/students/{sid}/reviews")
def reviews(sid: str) -> dict[str, Any]:
    items = eng.due_reviews(sid)
    out = []
    for r in items:
        try:
            kp = eng.dataset.get_kp(r.kp_id)
            out.append({"kp_id": r.kp_id, "name": kp.name,
                        "interval_days": round(r.interval_days, 1), "reps": r.reps})
        except KeyError:
            continue
    return {"due": out}


# ---------------- 前端静态资源

@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/figures", StaticFiles(directory=FIGURES_DIR), name="figures")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
