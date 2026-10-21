"""数据加载层：按学段×科目懒加载 split/ 下的数据集，构建全局索引。

数据集字段见仓库 README：knowledge_points 与 relations 两个数组，
annotations 是 JSON 字符串，内含 knowledgeType / difficulty / learningObjectives /
prerequisites / misconceptions / assessability 六个标注维度。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, DEFAULT


# ---------------------------------------------------------------- 数据模型

KP_TYPE_LABELS = {
    "conceptual": "概念性",
    "procedural": "程序性",
    "factual": "事实性",
    "metacognitive": "元认知",
    "unknown": "未标注",
}


@dataclass(slots=True)
class KnowledgePoint:
    id: str
    name: str
    subject: str
    grade_band: str
    grades: list[str]
    confidence: float
    aliases: list[str] = field(default_factory=list)
    summary: str = ""
    # annotations 解析结果
    kp_type: str = "unknown"          # conceptual / procedural / factual / metacognitive
    difficulty: float = 0.5           # 0~1
    learning_objectives: list[str] = field(default_factory=list)
    misconceptions: list[str] = field(default_factory=list)
    assessability: float = 0.5        # 0~1，出题可测评性
    textbooks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def display_grades(self) -> str:
        if self.grades:
            return "、".join(self.grades)
        # 高中模块制教材 grades 常为空，回退到 volume（模块名）
        vols = {tb.get("volume") for tb in self.textbooks if tb.get("volume")}
        return "、".join(sorted(vols)) or self.grade_band

    @property
    def kp_type_label(self) -> str:
        return KP_TYPE_LABELS.get(self.kp_type, self.kp_type)


@dataclass(slots=True)
class Relation:
    id: str
    from_id: str
    to_id: str
    relation_type: str      # prerequisite / leads-to / related
    evidence: str = ""


# ---------------------------------------------------------------- 加载器

class Dataset:
    """懒加载 split/<学段>/<科目>.json，并维护 kp_id → (学段, 科目) 全局索引。"""

    def __init__(self, config: Config = DEFAULT):
        self.config = config
        self.root = Path(config.data_dir)
        self._groups: dict[tuple[str, str], tuple[dict[str, KnowledgePoint], list[Relation]]] = {}
        self._id_index: dict[str, tuple[str, str]] = {}      # kp_id -> (band, subject)
        self._index_loaded = False

    # -------- 索引（读取 split/index.json，不载入正文）

    def list_groups(self) -> list[dict[str, Any]]:
        """列出所有可用的 学段×科目 组。"""
        idx = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        return sorted(
            ({"band": f["grade_band"], "subject": f["subject"],
              "kp_count": f["kp_count"], "relation_count": f["relation_count"],
              "path": f["path"]} for f in idx["files"]),
            key=lambda g: (["小学", "初中", "高中"].index(g["band"]), g["subject"]),
        )

    def _build_id_index(self) -> None:
        """首次查询任意 kp_id 时，快速扫描各分组文件的 id 字段建立映射。"""
        if self._index_loaded:
            return
        for g in self.list_groups():
            data = json.loads((self.root / g["path"]).read_text(encoding="utf-8"))
            for kp in data["knowledge_points"]:
                self._id_index[kp["id"]] = (g["band"], g["subject"])
        self._index_loaded = True

    # -------- 分组加载

    def group(self, band: str, subject: str) -> tuple[dict[str, KnowledgePoint], list[Relation]]:
        key = (band, subject)
        if key in self._groups:
            return self._groups[key]

        path = self.root / f"{band}/{subject}.json"
        if not path.exists():
            raise KeyError(f"不存在的分组：{band}/{subject}")

        raw = json.loads(path.read_text(encoding="utf-8"))
        min_conf = self.config.min_confidence

        kps: dict[str, KnowledgePoint] = {}
        for item in raw["knowledge_points"]:
            # confidence 可能显式为 null，不能直接用 get 的默认值
            conf = item.get("confidence")
            if conf is not None and conf < min_conf:
                continue
            kp = self._parse_kp(item)
            kps[kp.id] = kp

        relations: list[Relation] = []
        for item in raw["relations"]:
            # 关系两端必须都保留（任一端被置信度过滤掉则整条边丢弃）
            if item["from_kp_id"] not in kps or item["to_kp_id"] not in kps:
                continue
            if item["relation_type"] == "related":
                continue  # related 为弱关联，不参与先修/路径推理
            relations.append(Relation(
                id=item["id"], from_id=item["from_kp_id"], to_id=item["to_kp_id"],
                relation_type=item["relation_type"], evidence=item.get("source_evidence", ""),
            ))

        self._groups[key] = (kps, relations)
        return kps, relations

    @staticmethod
    def _parse_kp(item: dict[str, Any]) -> KnowledgePoint:
        ann: dict[str, Any] = {}
        if item.get("annotations"):
            try:
                ann = json.loads(item["annotations"])
            except (json.JSONDecodeError, TypeError):
                ann = {}

        def val(key: str, default: Any) -> Any:
            node = ann.get(key)
            return node.get("value", default) if isinstance(node, dict) else default

        aliases = item.get("aliases") or "[]"
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except json.JSONDecodeError:
                aliases = []

        ktype = val("knowledgeType", "unknown")
        # 兼容两种形态：value 是字符串（当前数据），或 value 本身还是对象
        if isinstance(ktype, dict):
            ktype = ktype.get("value", "unknown")
        ktype = str(ktype) if ktype else "unknown"

        return KnowledgePoint(
            id=item["id"],
            name=item["canonical_name"],
            subject=item["subject"],
            grade_band=item["grade_band"],
            grades=list(item.get("grades") or []),
            confidence=float(item.get("confidence") or 1.0),
            aliases=[a for a in aliases if isinstance(a, str)],
            summary=item.get("summary") or "",
            kp_type=str(ktype),
            difficulty=float(val("difficulty", 0.5) or 0.5),
            learning_objectives=[s for s in val("learning_objectives", []) or [] if isinstance(s, str)],
            misconceptions=[s for s in val("misconceptions", []) or [] if isinstance(s, str)],
            assessability=float(val("assessability", 0.5) or 0.5),
            textbooks=list(item.get("textbooks") or []),
        )

    # -------- kp_id 反查

    def locate(self, kp_id: str) -> tuple[str, str]:
        """由 kp_id 找到它所属的 (学段, 科目)。先查已加载分组，未命中再建全局索引。"""
        for (band, subject), (kps, _) in self._groups.items():
            if kp_id in kps:
                return band, subject
        self._build_id_index()
        if kp_id not in self._id_index:
            raise KeyError(f"未知的知识点 ID：{kp_id}")
        return self._id_index[kp_id]

    def get_kp(self, kp_id: str) -> KnowledgePoint:
        band, subject = self.locate(kp_id)
        kps, _ = self.group(band, subject)
        return kps[kp_id]

    # -------- 搜索

    def search(self, keyword: str, band: str | None = None,
               subject: str | None = None, limit: int = 20) -> list[KnowledgePoint]:
        """按名称/别名模糊搜索知识点。"""
        groups = [(band, subject)] if band and subject else \
            [(g["band"], g["subject"]) for g in self.list_groups()
             if (not band or g["band"] == band) and (not subject or g["subject"] == subject)]
        kw = keyword.lower()
        hits: list[KnowledgePoint] = []
        for b, s in groups:
            kps, _ = self.group(b, s)
            for kp in kps.values():
                if kw in kp.name.lower() or any(kw in a.lower() for a in kp.aliases):
                    hits.append(kp)
                    if len(hits) >= limit:
                        return hits
        return hits
