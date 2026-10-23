"""FastAPI 服务：把 AdaptiveEngine 暴露为 REST API，并托管 web/ 前端。

启动：python server.py  （或 uvicorn server:app --reload --port 8000）
"""

from __future__ import annotations

import os
import random
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ale.accounts import SESSION_COOKIE, Accounts
from ale.classroom import Classroom
from ale.config import FIGURES_DIR, WEB_DIR
from ale.engine import AdaptiveEngine, QHashMismatch
from ale.quiz import NoQuestionError as QNoQuestion
from ale.figures import FigureRegistry
from ale.llm import (KNOWN_PROVIDERS, LLMClient, LLMConfig, LLMError,
                     load_dotenv, probe_chat, save_llm_config)

load_dotenv()          # 项目根 .env（LLM 配置等），不覆盖进程已有环境变量

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
    # 出题响应里带回来的选项哈希；服务端核对，防"题目在两次请求间变掉"
    qhash: str | None = None


class LLMSettingsIn(BaseModel):
    """LLM 出题配置（前端「LLM 设置」面板的保存/探测请求体）。

    api_key 留空 = 保持现有 key 不变；base_url/model 全空 = 停用 AI 出题。
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_tokens: int | None = Field(default=None, ge=64, le=65536)


class ClassIn(BaseModel):
    name: str


class JoinIn(BaseModel):
    code: str


class AssignmentIn(BaseModel):
    """布置作业：教师把浏览时选中的知识点们打包成一页题。"""
    title: str
    kp_ids: list[str] = Field(..., min_length=1, max_length=20)


class HomeworkAnswerIn(BaseModel):
    item: int = Field(..., ge=0)
    choice: int = Field(..., ge=0, le=9)
    qhash: str | None = None


class ReviewPreviewIn(BaseModel):
    """抽题预览：seed 不传 = 服务端随机抽（审题台不是练习，不记掌握度）。"""
    kp_id: str
    seed: int | None = Field(default=None, ge=0, lt=1_000_000)


class ReviewDecideIn(BaseModel):
    kp_id: str
    seed: int = Field(..., ge=0, lt=1_000_000)
    qhash: str
    approve: bool
    note: str = ""


class ReviewUnapproveIn(BaseModel):
    qhash: str


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


@app.get("/api/kp/{kp_id}/chain")
def kp_chain(kp_id: str, student: str | None = None) -> dict[str, Any]:
    """知识链视图：该知识点的先修链（根→叶子）+ 直接下游。

    图谱全景在上万个知识点上必然变成一团线球，所以前端默认只展示这一条
    链，逐张卡片浮现；全景图降级为可选项。
    """
    try:
        kp = eng.dataset.get_kp(kp_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    try:
        g = eng.graph(kp.grade_band, kp.subject)
    except KeyError as e:
        raise HTTPException(404, str(e))

    stu = None
    if student:
        try:
            stu = eng.get_student(student)
        except KeyError:
            raise HTTPException(404, f"学生不存在：{student}")
    model = eng.model_for(stu) if stu else None
    now = time.time()

    def _node(k: Any) -> dict[str, Any]:
        item = {
            "id": k.id, "name": k.name,
            "depth": g.depth(k.id),
            "kp_type": k.kp_type, "difficulty": k.difficulty,
        }
        if model is not None and stu is not None:
            st = stu.state_of(k.id)
            p = model.p(st, k, now)
            thr = eng.config.mastered_for(model.name)
            item["p_mastery"] = round(p, 3)
            item["attempts"] = st.attempts
            item["level"] = ("mastered" if p >= thr
                             else "learning" if st.attempts else "new")
        return item

    # chain_to_root 已按 根→叶子 顺序返回
    chain = [_node(g.kps[_id]) for _id in g.chain_to_root(kp_id)]
    deps = sorted(g.dependents(kp.id),
                  key=lambda i: (-g.depth(i), g.kps[i].name))[:12]
    return {
        "kp_id": kp.id,
        "band": kp.grade_band, "subject": kp.subject,
        "chain": chain,
        "dependents": [{"id": g.kps[i].id, "name": g.kps[i].name,
                        "depth": g.depth(i)} for i in deps],
    }


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


# ---------------- 账号体系

class RegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)


class LoginIn(BaseModel):
    username: str
    password: str


accounts = Accounts(eng.config.db_path)
classroom = Classroom(eng.config.db_path, accounts)


def _set_session(resp: Response, uid: str) -> None:
    resp.set_cookie(SESSION_COOKIE, accounts.token_for(uid),
                    max_age=30 * 86400, httponly=True, samesite="lax",
                    path="/")


def require_user(request: Request) -> dict:
    """无 cookie → 401。前端据此弹登录框；游客走 /api/auth/guest。"""
    uid = accounts.uid_from_token(request.cookies.get(SESSION_COOKIE))
    if not uid:
        raise HTTPException(401, "尚未登录（可先注册，或以游客模式继续）")
    try:
        return accounts.require(uid)
    except KeyError:
        raise HTTPException(401, "会话已失效，请重新登录")


def _writable_student(sid: str, user: dict):
    """写操作前的归属校验：演示/未归属档案只读，别人的档案按不存在处理。

    归属规则：owner_id == user.id → 可写；owner_id 为空（演示或 CLI 建）→
    只读；是别人的 → 404（不暴露存在性）。
    """
    try:
        stu = eng.get_student(sid)
    except KeyError:
        raise HTTPException(404, "学生不存在")
    if stu.is_demo or stu.owner_id is None:
        raise HTTPException(403, "该档案是演示档案，只读。注册后用自己的档案做题")
    if stu.owner_id != user["id"]:
        raise HTTPException(404, "学生不存在")
    return stu


# ---------------- 班级与作业（教师布置 / 学生作答） ----------------

def _registered_user(user: dict) -> None:
    """班级是长期协作：数据要能保留，游客不参与。"""
    if user.get("is_guest"):
        raise HTTPException(403, "游客模式不能参与班级，请注册后再来（数据可长期保存）")


def _classroom_error(e: Exception) -> HTTPException:
    if isinstance(e, PermissionError):
        return HTTPException(403, str(e))
    if isinstance(e, KeyError):
        return HTTPException(404, "班级或作业不存在（可能已被移出）")
    return HTTPException(422, str(e))


@app.post("/api/classes")
def create_class(body: ClassIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    """建班：创建者即教师（owner），自动成为第一位成员。"""
    _registered_user(user)
    try:
        return classroom.create_class(user["id"], body.name)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/classes/join")
def join_class(body: JoinIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    _registered_user(user)
    try:
        return classroom.join(body.code, user["id"])
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/classes")
def my_classes(user: dict = Depends(require_user)) -> list[dict[str, Any]]:
    _registered_user(user)
    try:
        return classroom.my_classes(user["id"])
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/classes/{cid}/members")
def class_members(cid: str, user: dict = Depends(require_user)) -> list[dict[str, Any]]:
    _registered_user(user)
    try:
        return classroom.members(cid, user["id"])
    except Exception as e:
        raise _classroom_error(e)


@app.post("/api/classes/{cid}/assignments")
def create_assignment(cid: str, body: AssignmentIn,
                      user: dict = Depends(require_user)) -> dict[str, Any]:
    """布置作业：server 把 kp_id 解析成名称快照，保证教师删图也不受影响。"""
    _registered_user(user)
    kp_items = []
    for kid in body.kp_ids:
        try:
            kp = eng.dataset.get_kp(kid)
        except KeyError:
            raise HTTPException(404, f"知识点不存在：{kid}")
        kp_items.append({"kp_id": kid, "kp_name": kp.name})
    try:
        return classroom.create_assignment(cid, user["id"], body.title, kp_items)
    except Exception as e:
        raise _classroom_error(e)


@app.get("/api/classes/{cid}/assignments")
def list_assignments(cid: str, user: dict = Depends(require_user)) -> list[dict[str, Any]]:
    _registered_user(user)
    try:
        return classroom.assignments_for(cid, user["id"])
    except Exception as e:
        raise _classroom_error(e)


@app.get("/api/classes/{cid}/assignments/{aid}")
def assignment_detail(cid: str, aid: str,
                      user: dict = Depends(require_user)) -> dict[str, Any]:
    _registered_user(user)
    try:
        d = classroom.assignment_detail(aid, user["id"])
        if d["class_id"] != cid:
            raise KeyError("作业不属于该班级")
        return d
    except Exception as e:
        raise _classroom_error(e)


@app.post("/api/classes/{cid}/assignments/{aid}/answer")
def answer_homework(cid: str, aid: str, body: HomeworkAnswerIn,
                    user: dict = Depends(require_user)) -> dict[str, Any]:
    """作业题作答：判分复用 eng.answer_question（掌握度照常更新、qhash 照常核对），
    额外把结果登记进班级作业进度表。

    注意：成员学号即「自己注册时绑定/新建的那个学习档案」，所有人只能给
    自己的档案交作业，不存在代写入口。
    """
    _registered_user(user)
    sid = user.get("student_id")
    if not sid:
        raise HTTPException(422, "该账号还没有绑定学习档案，请先在档案下拉里新建一个")
    try:
        detail = classroom.assignment_detail(aid, user["id"])   # 内含成员资格校验
        if detail["class_id"] != cid:
            raise KeyError("作业不属于该班级")
        items = detail["items"]
        if not (0 <= body.item < len(items)):
            raise ValueError(f"题号越界：{body.item}")
        r = eng.answer_question(sid, items[body.item]["kp_id"],
                                items[body.item].get("seed", 0),
                                body.choice, qhash=body.qhash)
        classroom.record_result(aid, user["id"], body.item, r["correct"], body.choice)
    except Exception as e:
        if isinstance(e, QHashMismatch):
            raise HTTPException(409, str(e))
        raise _classroom_error(e)
    done = sum(1 for it in detail["items"] if it["done"]) + 1
    return {**r,
            "assignment": {"id": aid, "title": detail["title"],
                           "item": body.item, "total": len(items),
                           "done": min(done, len(items))}}


@app.get("/api/classes/{cid}/assignments/{aid}/results")
def assignment_results(cid: str, aid: str,
                       user: dict = Depends(require_user)) -> dict[str, Any]:
    _registered_user(user)
    try:
        d = classroom.results(aid, user["id"])
        if d["assignment"]["class_id"] != cid:
            raise KeyError("作业不属于该班级")
        return d
    except Exception as e:
        raise _classroom_error(e)


# ---------------- 审题台：AI 题审定入精选题库 ----------------

def _require_reviewer(user: dict) -> None:
    """审题台面向教师：以「创建过班级」为教师身份的判定依据。

    精选题库是全局公共资源，学生（或游客）如果也能往里放题，
    质量闸门就形同虚设。
    """
    _registered_user(user)
    if not classroom.owned_classes(user["id"]):
        raise HTTPException(403, "审题台面向教师：请先在「🏫 班级」面板新建一个班级")


@app.post("/api/review/preview")
def review_preview(body: ReviewPreviewIn,
                   user: dict = Depends(require_user)) -> dict[str, Any]:
    """抽题预览：走正常出题链（缓存 → 精选 → LLM → 模板），带答案下发，
    但不写掌握度不记作答——审题不是练习。
    """
    _require_reviewer(user)
    try:
        eng.dataset.get_kp(body.kp_id)
    except KeyError:
        raise HTTPException(404, f"知识点不存在：{body.kp_id}")
    seed = body.seed if body.seed is not None else random.randrange(1_000_000)
    try:
        q = eng.make_question(body.kp_id, seed=seed)
    except QNoQuestion as e:
        raise HTTPException(422, str(e))
    out = q.to_dict(reveal=True)
    out["model"] = q.model
    out["curated_counts"] = eng.curated.counts(body.kp_id)
    return out


@app.post("/api/review/decide")
def review_decide(body: ReviewDecideIn,
                  user: dict = Depends(require_user)) -> dict[str, Any]:
    """审定：服务端按 (kp_id, seed) 只读缓存重建，核对 qhash 一致后才
    接受决定——教师审的和入库的必须是同一道题。
    """
    _require_reviewer(user)
    try:
        q = eng.make_question(body.kp_id, seed=body.seed, for_answer=True)
    except QNoQuestion as e:
        raise HTTPException(422, f"无法重建该题：{e}")
    if q.qhash != body.qhash:
        raise HTTPException(409, "预览的题目已刷新（缓存变动），请重新抽题再决定")
    status = "approved" if body.approve else "rejected"
    row = eng.curated.upsert(q, status, reviewer=user.get("username") or "",
                             note=body.note[:200])
    if body.approve:
        eng._qbank.put(q)        # 精选题也进答案库，换模型/清缓存仍能判分
    return {**row, "counts": eng.curated.counts(body.kp_id)}


@app.get("/api/review/curated")
def review_curated(kp_id: str | None = None, limit: int = 30,
                   user: dict = Depends(require_user)) -> dict[str, Any]:
    """精选题库浏览（教师端点，含答案与解析）。"""
    _require_reviewer(user)
    limit = max(1, min(100, limit))
    return {"items": eng.curated.recent(status="approved", kp_id=kp_id,
                                        limit=limit),
            "counts": eng.curated.counts(kp_id)}


@app.post("/api/review/unapprove")
def review_unapprove(body: ReviewUnapproveIn,
                     user: dict = Depends(require_user)) -> dict[str, Any]:
    """下架：精选题不再参与出题复用。"""
    _require_reviewer(user)
    row = eng.curated.unapprove(body.qhash, reviewer=user.get("username") or "")
    if row is None:
        raise HTTPException(404, "未找到该精选题（可能已下架）")
    return {"qhash": row["qhash"], "status": row["status"],
            "counts": eng.curated.counts(row["kp_id"])}


# ---------------- LLM 出题设置（前端可视化配置） ----------------

def _require_registered(user: dict) -> None:
    """改 LLM 配置是全局操作，游客一律拒绝。"""
    if user.get("is_guest"):
        raise HTTPException(403, "游客模式不能修改 LLM 设置，请先注册")


def _llm_settings_out(cfg: LLMConfig) -> dict[str, Any]:
    """配置摘要。密钥永不回传，只给「是否已配置 + 末 4 位」。"""
    return {
        "enabled": cfg.enabled,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "key_set": bool(cfg.api_key),
        "key_hint": cfg.api_key[-4:] if cfg.api_key else "",
        "max_tokens": cfg.max_tokens,
        "providers": [{"key": k, "base_url": v["base_url"], "model": v["model"]}
                      for k, v in KNOWN_PROVIDERS.items()],
    }


@app.get("/api/llm")
def llm_read(user: dict = Depends(require_user)) -> dict[str, Any]:
    """当前 LLM 配置（登录即可看；游客只能看不能改）。"""
    return _llm_settings_out(eng.llm.config)


@app.post("/api/llm/save")
def llm_save(body: LLMSettingsIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    """保存 LLM 配置：热切换运行中的客户端 + 原子写回 .env（重启不变）。

    api_key 留空 = 沿用现有 key（面板显示已配置时通常留空）；三项全空 = 停用。
    """
    _require_registered(user)
    old = eng.llm.config
    new = LLMConfig(
        base_url=body.base_url.strip().rstrip("/"),
        api_key=body.api_key.strip() or old.api_key,
        model=body.model.strip(),
        max_tokens=body.max_tokens or old.max_tokens,
        # 会话 ID 保持进程级稳定；换服务商也无害（对方会忽略）
        session_id=old.session_id,
    )
    if not new.enabled and (new.base_url or new.api_key or new.model):
        raise HTTPException(
            422, "配置不完整：base_url / api_key / model 需要三项齐备才会启用")
    eng.set_llm_config(new)
    try:
        save_llm_config(new.base_url, new.api_key, new.model, new.max_tokens)
    except OSError as e:
        raise HTTPException(500, f"配置已生效但写盘失败：{e}")
    return _llm_settings_out(eng.llm.config)


@app.post("/api/llm/test")
def llm_test(body: LLMSettingsIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    """连通性探测：用面板里填的值（key 留空则用现有 key）试发一条短消息。

    探测不改配置、不落盘。返回 ok / 耗时 / 模型回复，或错误摘要。
    """
    _require_registered(user)
    old = eng.llm.config
    probe_cfg = LLMConfig(
        base_url=body.base_url.strip().rstrip("/"),
        api_key=body.api_key.strip() or old.api_key,
        model=body.model.strip(),
        max_tokens=body.max_tokens or 512,
        session_id=old.session_id,
        timeout=min(60.0, old.timeout if old.timeout > 60 else 60.0),
    )
    if not probe_cfg.enabled:
        raise HTTPException(422, "三项配置不完整，无法探测")
    t0 = time.time()
    try:
        reply = probe_chat(LLMClient(probe_cfg))
    except LLMError as e:
        return {"ok": False, "error": str(e), "latency_ms": round((time.time() - t0) * 1000)}
    return {"ok": True, "reply": reply[:80],
            "model": probe_cfg.model,
            "latency_ms": round((time.time() - t0) * 1000)}


@app.post("/api/auth/register")
def auth_register(body: RegisterIn, resp: Response) -> dict[str, Any]:
    try:
        user = accounts.register(body.username, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    stu = eng.store.create(f"{body.username}的学档", owner_id=user["id"])
    accounts.bind_student(user["id"], stu.id)
    user = accounts.require(user["id"])
    _set_session(resp, user["id"])
    return {"user": user, "student_id": stu.id}


@app.post("/api/auth/login")
def auth_login(body: LoginIn, resp: Response) -> dict[str, Any]:
    try:
        user = accounts.login(body.username, body.password)
    except ValueError as e:
        raise HTTPException(401, str(e))
    if not user["student_id"]:      # 没绑档案的旧账号，绑到自己的第一个档案
        own = [s for s in eng.store.list_all(user["id"]) if s["owner_id"] == user["id"]]
        if own:
            accounts.bind_student(user["id"], own[0]["id"])
            user = accounts.require(user["id"])
    _set_session(resp, user["id"])
    return {"user": user, "student_id": user["student_id"]}


@app.post("/api/auth/guest")
def auth_guest(resp: Response) -> dict[str, Any]:
    """游客模式：不用注册即可做题。游客档案留在库里，但不可再次登录。"""
    user = accounts.create_guest()
    stu = eng.store.create("游客", owner_id=user["id"])
    accounts.bind_student(user["id"], stu.id)
    _set_session(resp, user["id"])
    return {"user": user, "student_id": stu.id}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    uid = accounts.uid_from_token(request.cookies.get(SESSION_COOKIE))
    if not uid:
        return {"user": None}
    try:
        return {"user": accounts.require(uid)}
    except KeyError:
        return {"user": None}


@app.post("/api/auth/logout")
def auth_logout(resp: Response) -> dict[str, Any]:
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# ---------------- 学生

@app.post("/api/students")
def create_student(body: StudentIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    if body.model not in ("bkt", "elo"):
        raise HTTPException(400, "model 只能是 bkt 或 elo")
    stu = eng.store.create(body.name, model=body.model, owner_id=user["id"])
    if not user["student_id"]:
        accounts.bind_student(user["id"], stu.id)
    return {"id": stu.id, "name": stu.name, "model": stu.model}


@app.get("/api/students")
def list_students(request: Request) -> list[dict[str, Any]]:
    """公开全部演示/未归属档案 + 当前登录用户自己的档案。

    每项带 mine / is_demo / owner_id，前端据此区分可编辑与否。
    """
    uid = accounts.uid_from_token(request.cookies.get(SESSION_COOKIE))
    out = []
    for s in eng.store.list_all(owner_id=uid):
        item = dict(s)
        item["mine"] = bool(uid) and s["owner_id"] == uid
        out.append(item)
    return out


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
def record_attempt(sid: str, body: AttemptIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    _writable_student(sid, user)
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
    """出一道题。响应里不含正确答案——判分在 POST /api/students/{sid}/answer 里做。

    配了 LLM（K12_LLM_*）时走 AI 命题（首次生成需要几秒，结果落缓存），
    否则/失败时走标注模板，响应里的 source 字段标明来源。
    """
    try:
        q = eng.make_question(kp_id, seed=seed)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return q.to_dict()


@app.post("/api/students/{sid}/answer")
def answer_quiz(sid: str, body: AnswerIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    """提交作答：服务端重建题目判分 → 写入掌握度 → 答错时自动归因。"""
    _writable_student(sid, user)
    try:
        return eng.answer_question(sid, body.kp_id, body.seed, body.choice,
                                   qhash=body.qhash)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        # 题目哈希不一致（缓存被清/换了模型）→ 重新拉一道题就能恢复
        if isinstance(e, QHashMismatch):
            raise HTTPException(409, str(e))
        raise HTTPException(422, str(e))


@app.post("/api/students/{sid}/attempts")
def record_attempts(sid: str, body: AttemptsIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    """批量记录作答：整个批次只写一次档案（逐条记录是 O(n) 次磁盘写）。"""
    _writable_student(sid, user)
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
def simulate(sid: str, body: SimulateIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    _writable_student(sid, user)
    try:
        results = eng.simulate_learner(sid, body.kp_ids, ability=body.ability,
                                       noise=body.noise, seed=body.seed)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"count": len(results),
            "correct": sum(1 for r in results if r.correct),
            "mastered": sum(1 for r in results if r.mastered)}


@app.post("/api/students/{sid}/diagnose")
def diagnose(sid: str, body: DiagnoseIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    _writable_student(sid, user)
    try:
        return eng.diagnose(sid, body.kp_ids).to_dict()
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/students/{sid}/diagnose/recent")
def diagnose_recent(sid: str) -> dict[str, Any]:
    return eng.diagnose_recent_failures(sid).to_dict()


@app.post("/api/students/{sid}/plan")
def make_plan(sid: str, body: PlanIn, user: dict = Depends(require_user)) -> dict[str, Any]:
    _writable_student(sid, user)
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
