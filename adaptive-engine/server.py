"""FastAPI 服务：把 AdaptiveEngine 暴露为 REST API，并托管 web/ 前端。

启动：python server.py  （或 uvicorn server:app --reload --port 8000）
"""

from __future__ import annotations

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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

eng = AdaptiveEngine()
figures = FigureRegistry()


# ---------------- 请求体

class AttemptIn(BaseModel):
    kp_id: str
    correct: bool


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


# ---------------- 元数据

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
def graph_view(band: str, subject: str, student: str | None = None) -> dict[str, Any]:
    """图谱可视化数据：节点（含掌握度着色）+ 边。学生指定时节点带 p。"""
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
    nodes = []
    for kid, kp in g.kps.items():
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
            node["mastered"] = p >= eng.config.mastered_threshold
        node["confidence"] = kp.confidence
        node["low_confidence"] = kp.confidence < eng.config.low_confidence_hint
        nodes.append(node)
    edges = [{"source": r.from_id, "target": r.to_id, "type": r.relation_type}
             for r in g.relations]
    return {"band": band, "subject": subject, "nodes": nodes, "edges": edges,
            "stats": g.stats(), "hubs": g.hubs(8)}


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
        if model.p(st, kp, now) >= eng.config.mastered_threshold:
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


@app.post("/api/students/{sid}/simulate")
def simulate(sid: str, body: SimulateIn) -> dict[str, Any]:
    try:
        results = eng.simulate_learner(sid, body.kp_ids, ability=body.ability, noise=body.noise)
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
