"""题目生成器：LLM 命题为主，标注素材兑底。

数据支撑：`annotations` 里每个知识点都带
- `learningObjectives`：正确说法（LLM 命题的"标准答案"素材）
- `misconceptions`：常见误区（干扰项素材——这是让选项"看起来对"的关键）
- `assessability`：出题可测评性（0~1，太低的知识点不出题）

生成路径（`generate_cached`）：
1. 命中缓存 → 直接返回（同一 (kp_id, seed) 的题目永远一样，作答校验靠这个）；
2. 配了 LLM（K12_LLM_* 三项环境变量）→ 带完整知识点上下文让 LLM 按命题
   规范出 4 选 1，强校验后落缓存；
3. LLM 不可用/失败 → 标注模板兑底（选项文字直接来自标注，读起来生硬，
   所以只作兜底）。

服务端不存"题目状态"，只存"生成结果缓存"——下发接口永不返回 answer_index，
判分时重建同一道题并核对选项哈希（qhash），客户端没法拿缓存当答案。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dataset import KnowledgePoint
from .llm import LLMError, LLMClient, parse_json_block

PROMPT_VERSION = "v3-latex"    # 提示词改动时递增，旧缓存自动失效

# seed → 命题角度：让不同次抽题考的不是同一个点
_ANGLES = [
    "概念辨析：围绕「正确理解与常见错误理解的差别」命题",
    "情境应用：设计一个简短情境，让学生用该知识点作判断",
    "正误辨析：四个选项围绕同一判断点，只有一项站得住",
    "关系推理：围绕该知识点与其先修概念的关系命题",
]

_SYSTEM_PROMPT = rf"""你是一名资深 K12 命题教师，为中国义务教育阶段学生出选择题。

你会收到一个知识点的结构化材料（学习目标、常见误区等）。请按以下规范出 4 选 1：

1. 题干有具体判断点，不复读知识点名称，不把「关于X下列哪项正确」当题干。
2. 四个选项都必须是完整、通顺的自然句子，能独立成立；禁止出现
   「认为…」「能…」「混淆…」这类动词开头的标注式残句。
3. 至少一个干扰项来自材料里的常见误区，但要**改写成看起来合理的说法**——
   误区之所以叫常见误区，就是因为很多学生真心这么以为，选项要能骗到他们，
   而不是一眼假。
4. 正确项表述必须与该学段课程标准一致，具体可判断，不是空泛口号。
5. 不出「以上都对/都不对」；选项之间不互相包含；正确项不比干扰项更长。

【公式排版（最重要）】
- 所有数学式、物理量、化学式一律用 LaTeX 表示，行内公式两端用单个 $ 包住，
  例如 $\sqrt{{2}}$、$\frac{{a}}{{b}}$、$(2\sqrt{{5}})^2=20$、$\Delta t$、$F=ma$、
  $H_2O$、$30°$。
- 禁止用字符替代公式：不许出现 √、²、³、π 等符号的直接拼写；
  也不许把这些字符混进 LaTeX 命令里。
- 上下标、分式、根号、希腊字母必须走 LaTeX：$x^2$、$a_1$、$\pi$、$v_0 t$。
- 纯文字照常写；每个公式段独立成 $...$，不要一整段全包进一个公式。

【解析（explain）】
- 必须贴合该知识点所在学科/章节的固有方法：数学题讲清步步推导（先做什么
  再做什么，关键等式用 $...$ 给出），物理题讲清对应定律/公式与代入计算，
  化学题讲清方程式与守恒关系。不许只说「根据定义选 A」。
- 说清两层：正确项为什么对（给推理过程）；每个错误项错在哪一条误区上。
  面向学生口吻，80~160 字。

只输出一个 JSON 对象（不要 markdown 代码块以外的文字）：
{{"prompt": "题干", "options": ["A文字","B文字","C文字","D文字"],
  "answer_index": 0到3的整数, "explain": "解析",
  "misconception_used": "你把哪条常见误区改写成了干扰项（原文或改写后）"}}
提示词版本：{PROMPT_VERSION}
"""


@dataclass(slots=True)
class Question:
    """一道题。answer_index 只在服务端内部使用，不随题目一起下发给前端。"""
    kp_id: str
    kp_name: str
    seed: int
    kind: str                       # "choice" | "judge"
    prompt: str
    options: list[str]
    answer_index: int
    explain: str
    objective: str | None = None    # 正确答案原文/出处
    misconception: str | None = None  # 该题针对的误区（干扰项之一）
    source: str = "template"        # "llm" | "template"
    model: str = ""                 # LLM 命题用的模型名
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def question_id(self) -> str:
        return f"{self.kp_id}@{self.seed}"

    @property
    def qhash(self) -> str:
        """选项列表的哈希。作答时服务端重算核对，防止题目在两次请求间变掉。"""
        return _qhash(self.kp_id, self.seed, self.options)

    def to_dict(self, reveal: bool = False) -> dict[str, Any]:
        out = {
            "question_id": self.question_id,
            "kp_id": self.kp_id,
            "seed": self.seed,          # 前端答题回传用；qhash 绑定 (kp|seed|options)
            "kp_name": self.kp_name,
            "kind": self.kind,
            "prompt": self.prompt,
            "options": list(self.options),
            "qhash": self.qhash,
            "source": self.source,
        }
        if reveal:
            out.update({
                "answer_index": self.answer_index,
                "correct_option": self.options[self.answer_index],
                "explain": self.explain,
                "objective": self.objective,
                "misconception": self.misconception,
            })
        return out


def _qhash(kp_id: str, seed: int, options: list[str]) -> str:
    raw = json.dumps([kp_id, str(seed), options], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class NoQuestionError(ValueError):
    """该知识点没有足够的素材出题。"""


def can_ask(kp: KnowledgePoint, min_assessability: float = 0.5) -> bool:
    """判断该知识点能否出题。"""
    if kp.assessability < min_assessability:
        return False
    return bool(kp.learning_objectives) and bool(kp.misconceptions)


# ------------------------------------------------------------------ 入口

def generate_cached(kp: KnowledgePoint, seed: int = 0,
                    cache_dir: Path | None = None,
                    llm: LLMClient | None = None,
                    min_assessability: float = 0.5,
                    rebuild: bool = False) -> Question:
    """出题总入口：缓存 → LLM → 模板，三级回退。

    rebuild=True 用于作答校验：只读缓存，绝不调 LLM——保证重建的就是当初
    下发的那道题，且不产生第二次生成开销。
    """
    if kp.assessability < min_assessability:
        raise NoQuestionError(f"该知识点可测评性过低（{kp.assessability:.2f}），不适合出题")

    if cache_dir is not None:
        cached = _cache_get(cache_dir, kp, seed, llm)
        if cached is not None:
            return cached

    if rebuild:
        return generate(kp, seed=seed)

    if llm is not None and llm.config.enabled:
        try:
            q = _llm_generate(kp, seed, llm)
            if cache_dir is not None:
                _cache_put(cache_dir, kp, seed, llm, q)
            return q
        except LLMError as e:
            # 兜底到模板，但把失败原因打进日志，便于排查 key/额度/截断问题
            print(f"[llm-fallback] {type(e).__name__}: {e}", flush=True)          # 兜底到模板；失败原因由调用方按需记录

    return generate(kp, seed=seed)


def generate(kp: KnowledgePoint, seed: int = 0,
             min_assessability: float = 0.5,
             prefer: str | None = None) -> Question:
    """模板生成（兑底路径，不依赖 LLM）。

    seed 相同 → 题目完全相同（选项顺序也一样），服务端可据此重新生成并校验。
    """
    if kp.assessability < min_assessability:
        raise NoQuestionError(f"该知识点可测评性过低（{kp.assessability:.2f}），不适合出题")

    objectives = list(kp.learning_objectives)
    misconceptions = list(kp.misconceptions)
    rng = random.Random(f"{kp.id}:{seed}")

    kind = prefer
    if kind is None:
        # 有正确说法且有 ≥2 条误区 → 选择题更耐玩；
        # 但全是选择题也单调，留 30% 出判断题换换口味。
        if objectives and len(misconceptions) >= 2:
            kind = "choice" if rng.random() < 0.7 else "judge"
        else:
            kind = "judge"

    if kind == "choice":
        if not objectives or len(misconceptions) < 2:
            raise NoQuestionError("素材不足以生成选择题")
        return _choice(kp, seed, rng, objectives, misconceptions)
    return _judge(kp, seed, rng, objectives, misconceptions)


# ------------------------------------------------------------------ LLM 命题

def _build_context(kp: KnowledgePoint, seed: int) -> str:
    """把知识点材料打包成 LLM 的上下文。seed 决定命题角度，保证可复现。"""
    rng = random.Random(f"angle:{kp.id}:{seed}")
    angle = rng.choice(_ANGLES)
    material = {
        "知识点": kp.name,
        "学科": kp.subject,
        "学段": kp.grade_band,
        "年级": kp.grades,
        "类型": kp.kp_type,
        "难度(0~1)": kp.difficulty,
        "别名": kp.aliases[:5],
        "教材位置": kp.textbooks[:3],
        "学习目标（标准答案素材）": kp.learning_objectives,
        "常见误区（干扰项素材，可选其改写）": kp.misconceptions,
    }
    return ("命题角度：" + angle + "\n\n知识点材料：\n"
            + json.dumps(material, ensure_ascii=False, indent=1)
            + "\n\n请按系统提示中的规范出题，只输出 JSON。")


def _llm_generate(kp: KnowledgePoint, seed: int, client: LLMClient) -> Question:
    obj = parse_json_block(client.chat(_SYSTEM_PROMPT,
                                       _build_context(kp, seed),
                                       temperature=0.8))
    prompt = str(obj.get("prompt") or "").strip()
    options = obj.get("options")
    answer_index = obj.get("answer_index")
    explain = str(obj.get("explain") or "").strip()
    mis = obj.get("misconception_used")

    # ---- 强校验：LLM 的话不能照单全收，不合格直接抛错走兜底
    if not (5 <= len(prompt) <= 200):
        raise LLMError(f"题干长度非法：{len(prompt)} 字")
    if not isinstance(options, list) or len(options) != 4:
        raise LLMError("选项必须恰好 4 个")
    options = [str(o).strip() for o in options]
    if any(not (2 <= len(o) <= 120) for o in options):
        raise LLMError("选项长度非法")
    if len(set(options)) != 4:
        raise LLMError("四个选项必须互不相同")
    if not isinstance(answer_index, int) or not (0 <= answer_index <= 3):
        raise LLMError(f"answer_index 非法：{answer_index!r}")
    if len(explain) < 10:
        raise LLMError("解析过短")

    return Question(kp_id=kp.id, kp_name=kp.name, seed=seed, kind="choice",
                    prompt=prompt, options=options, answer_index=answer_index,
                    explain=explain,
                    objective=options[answer_index],
                    misconception=(str(mis)[:120] if mis else None),
                    source="llm", model=client.config.model)


# ------------------------------------------------------------------ 缓存

def _cache_path(cache_dir: Path, kp: KnowledgePoint, seed: int,
                model: str = "", version: str = PROMPT_VERSION) -> Path:
    """缓存键包含模型名与提示词版本：换模型/改提示词 = 新的一批题。"""
    key = hashlib.sha1(f"{kp.id}|{seed}|{model}|{version}".encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.json"


def _cache_get(cache_dir: Path, kp: KnowledgePoint, seed: int,
               llm: LLMClient | None) -> Question | None:
    return read_cache(cache_dir, kp, seed,
                      llm.config.model if llm else "")


def read_cache(cache_dir: Path, kp: KnowledgePoint, seed: int,
               model: str = "", version: str = PROMPT_VERSION) -> Question | None:
    """按显式模型键读缓存（供引擎的精选复用链使用）。"""
    p = _cache_path(cache_dir, kp, seed, model, version)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Question(kp_id=raw["kp_id"], kp_name=raw["kp_name"],
                        seed=raw["seed"], kind=raw["kind"], prompt=raw["prompt"],
                        options=raw["options"], answer_index=raw["answer_index"],
                        explain=raw["explain"], objective=raw.get("objective"),
                        misconception=raw.get("misconception"),
                        source="llm-cache" if raw.get("source") == "llm"
                               else raw.get("source", "template"),
                        model=raw.get("model", ""))
    except (KeyError, TypeError):
        return None


def _cache_put(cache_dir: Path, kp: KnowledgePoint, seed: int,
               llm: LLMClient, q: Question) -> None:
    """原子写入，避免中途崩溃留下半截 JSON。"""
    write_cache(cache_dir, kp, seed, llm.config.model if llm else "", q)


def write_cache(cache_dir: Path, kp: KnowledgePoint, seed: int,
                model: str, q: Question,
                version: str = PROMPT_VERSION) -> None:
    """按显式模型键原子写缓存（供引擎把精选题注入判分重建链使用）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = _cache_path(cache_dir, kp, seed, model, version)
    payload = {
        "kp_id": q.kp_id, "kp_name": q.kp_name, "seed": q.seed,
        "kind": q.kind, "prompt": q.prompt, "options": q.options,
        "answer_index": q.answer_index, "explain": q.explain,
        "objective": q.objective, "misconception": q.misconception,
        "source": q.source, "model": q.model, "saved_ts": time.time(),
    }
    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ 模板兜底

def _choice(kp: KnowledgePoint, seed: int, rng: random.Random,
            objectives: list[str], misconceptions: list[str]) -> Question:
    correct = rng.choice(objectives)
    pool = list(misconceptions)
    rng.shuffle(pool)
    n_options = min(4, max(2, len(pool) + 1))
    distractors = pool[: n_options - 1]
    if len(distractors) < n_options - 1:
        raise NoQuestionError("误区数量不足以组成选项")

    options = distractors + [correct]
    rng.shuffle(options)
    answer_index = options.index(correct)

    explain = (f"正确说法：{correct}\n"
               f"其余选项都是常见误区：\n" +
               "\n".join(f"· {d}" for d in distractors))
    return Question(
        kp_id=kp.id, kp_name=kp.name, seed=seed, kind="choice",
        prompt=f"关于「{kp.name}」，下列哪项说法是正确的？",
        options=options, answer_index=answer_index, explain=explain,
        objective=correct, misconception=distractors[0] if distractors else None,
        source="template")


def _judge(kp: KnowledgePoint, seed: int, rng: random.Random,
           objectives: list[str], misconceptions: list[str]) -> Question:
    """判断正误。

    必须同时有误区和正确目标：只用误区则答案恒为"错误"，只用目标则答案恒为
    "正确"，两种都能被猜中，不成其为题。
    """
    if not objectives or not misconceptions:
        raise NoQuestionError("判断题需要同时具备正确目标与常见误区，否则答案可被猜中")

    if rng.random() < 0.5:
        statement = rng.choice(misconceptions)
        answer_text = "说法错误"
        explain = (f"这是常见误区：{statement}\n"
                   f"正确理解应当避免上述说法。")
        misconception = statement
        objective = None
    else:
        statement = rng.choice(objectives)
        answer_text = "说法正确"
        explain = f"该说法与学习目标一致：{statement}"
        misconception = None
        objective = statement

    options = ["说法正确", "说法错误"]
    if rng.random() < 0.5:
        options = ["说法错误", "说法正确"]
    answer_index = options.index(answer_text)

    return Question(
        kp_id=kp.id, kp_name=kp.name, seed=seed, kind="judge",
        prompt=f"判断正误：\n「{statement}」",
        options=options, answer_index=answer_index, explain=explain,
        objective=objective, misconception=misconception,
        source="template")
