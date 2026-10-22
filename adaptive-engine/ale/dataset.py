"""数据加载层：按学段×科目懒加载 split/ 下的数据集，构建全局索引。

数据集字段见仓库 README：knowledge_points 与 relations 两个数组，
annotations 是 JSON 字符串，内含 knowledgeType / difficulty / learningObjectives /
prerequisites / misconceptions / assessability 六个标注维度。
"""

from __future__ import annotations

import hashlib
import json
import os
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
    """懒加载 split/<学段>/<科目>.json，并维护 kp_id → (学段, 科目) 全局索引。

    两个派生索引（id→分组、名称/别名→id）会缓存到 config.cache_dir：
    冷启动时全量解析 39 个 split 文件要 0.4s 并占用上百 MB 内存，而这两个
    索引只依赖 id / name / aliases，用文件指纹（大小 + mtime）判断新鲜度后
    落盘，运行时就只是读一个小 JSON。
    """

    def __init__(self, config: Config = DEFAULT):
        self.config = config
        self.root = Path(config.data_dir)
        self._groups: dict[tuple[str, str], tuple[dict[str, KnowledgePoint], list[Relation]]] = {}
        self._id_index: dict[str, tuple[str, str]] = {}      # kp_id -> (band, subject)
        self._index_loaded = False
        self._search_index: list[tuple[str, str, str, list[str]]] | None = None
        # 命中缓存时记录来源，便于排查"为什么这次没走缓存"
        self.index_source: dict[str, str] = {}

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

    # -------- 派生索引缓存

    def _fingerprint(self) -> str:
        """数据集指纹：各 split 文件的 (相对路径, 大小, mtime)。

        数据集更新后指纹变化，缓存自动失效重建。
        """
        parts: list[str] = []
        for g in self.list_groups():
            p = self.root / g["path"]
            try:
                st = p.stat()
                parts.append(f"{g['path']}:{st.st_size}:{int(st.st_mtime)}")
            except OSError:
                parts.append(f"{g['path']}:missing")
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def _cache_path(self, name: str) -> Path:
        return Path(self.config.cache_dir) / f"{name}.{self._fingerprint()[:12]}.json"

    def _read_cache(self, name: str) -> Any | None:
        path = self._cache_path(name)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_cache(self, name: str, payload: Any) -> None:
        """落盘缓存（原子写）。缓存目录可写失败（只读部署）时静默跳过。"""
        path = self._cache_path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _build_id_index(self, force: bool = False) -> None:
        """首次查询任意 kp_id 时，建立 kp_id → (学段, 科目) 映射。

        优先读缓存；缓存不可用时全量扫描各分组文件并写回缓存。
        """
        if self._index_loaded and not force:
            return
        if not force:
            cached = self._read_cache("id_index")
            if isinstance(cached, dict) and cached:
                self._id_index = {k: tuple(v) for k, v in cached.items()}
                self._index_loaded = True
                self.index_source["id_index"] = "cache"
                return

        for g in self.list_groups():
            data = json.loads((self.root / g["path"]).read_text(encoding="utf-8"))
            for kp in data["knowledge_points"]:
                self._id_index[kp["id"]] = (g["band"], g["subject"])
        self._index_loaded = True
        self.index_source["id_index"] = "scan"
        self._write_cache("id_index",
                          {k: list(v) for k, v in self._id_index.items()})

    def _build_search_index(self, force: bool = False) -> list[tuple[str, str, str, list[str]]]:
        """构建 (id, 名称, 学段, 科目, 别名) 的搜索索引（落盘缓存）。

        只保留搜索需要的字段，避免把 3 万多个 KnowledgePoint 对象常驻内存。
        """
        if self._search_index is not None and not force:
            return self._search_index

        if not force:
            cached = self._read_cache("search_index")
            if isinstance(cached, list) and cached:
                self._search_index = [tuple(item) for item in cached]
                self.index_source["search_index"] = "cache"
                return self._search_index

        rows: list[tuple[str, str, str, str, list[str]]] = []
        for g in self.list_groups():
            data = json.loads((self.root / g["path"]).read_text(encoding="utf-8"))
            for kp in data["knowledge_points"]:
                aliases = kp.get("aliases") or "[]"
                if isinstance(aliases, str):
                    try:
                        aliases = json.loads(aliases)
                    except json.JSONDecodeError:
                        aliases = []
                rows.append((kp["id"], kp.get("canonical_name") or "",
                             g["band"], g["subject"],
                             [a for a in aliases if isinstance(a, str)]))
        self._search_index = rows
        self.index_source["search_index"] = "scan"
        self._write_cache("search_index", [list(r) for r in rows])
        return rows

    def rebuild_indexes(self) -> dict[str, str]:
        """强制重建派生索引（数据集更新后手动调用；忽略现有缓存）。"""
        self._index_loaded = False
        self._id_index = {}
        self._search_index = None
        self._build_id_index(force=True)
        self._build_search_index(force=True)
        return dict(self.index_source)

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

        def val_any(*keys: str, default: Any = None) -> Any:
            """按多个候选 key 依次取值。

            数据集里 annotation 的 key 是驼峰（learningObjectives），早期代码按
            蛇形（learning_objectives）读，结果「学习目标」对全部 3 万多个知识点
            都是空的。这里两种写法都认。
            """
            for k in keys:
                if k in ann:
                    v = ann[k]
                    v = v.get("value") if isinstance(v, dict) else v
                    if v is not None:
                        return v
            return default

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
            learning_objectives=[s for s in (val_any("learningObjectives",
                                                     "learning_objectives", default=[]) or [])
                                if isinstance(s, str)],
            misconceptions=[s for s in (val_any("misconceptions", default=[]) or [])
                            if isinstance(s, str)],
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
        """按名称/别名模糊搜索知识点。

        走搜索索引（id/名称/别名 + 分组），避免为了搜一个词就把 3 万多个
        知识点对象全部解析进内存。命中后再按需加载对应分组的完整对象。
        """
        kw = keyword.strip().lower()
        if not kw:
            return []
        rows = self._build_search_index()
        matched: list[tuple[str, str, str, str, list[str]]] = []
        for row in rows:
            _, name, r_band, r_subject, aliases = row
            if band and r_band != band:
                continue
            if subject and r_subject != subject:
                continue
            if kw in name.lower() or any(kw in a.lower() for a in aliases):
                matched.append(row)

        hits: list[KnowledgePoint] = []
        for kp_id, _name, r_band, r_subject, _aliases in matched[:limit]:
            try:
                hits.append(self.get_kp(kp_id))
            except KeyError:
                continue
        return hits
