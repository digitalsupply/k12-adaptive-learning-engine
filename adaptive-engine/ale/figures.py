"""交互图形注册表。

图形本身是离线构建的静态 HTML/SVG（不调用 LLM），这里只负责把知识点 UUID
映射到图形条目。注册表缺失或损坏时降级为"没有图形"，不影响主引擎。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import FIGURES_DIR


class FigureRegistry:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else FIGURES_DIR / "registry.json"
        self._figures: dict[str, dict[str, Any]] = {}
        self._by_kp: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        figures = data.get("figures") or {}
        if isinstance(figures, dict):
            self._figures = {
                str(key): dict(value)
                for key, value in figures.items()
                if isinstance(value, dict)
            }
        by_kp = data.get("by_kp") or {}
        if isinstance(by_kp, dict):
            self._by_kp = {
                str(key): str(value)
                for key, value in by_kp.items()
                if str(value) in self._figures
            }

    def for_kp(self, kp_id: str) -> dict[str, Any] | None:
        fid = self._by_kp.get(str(kp_id))
        if not fid:
            return None
        figure = self._figures.get(fid)
        if not figure:
            return None
        return dict(figure, id=fid)

    def all(self) -> list[dict[str, Any]]:
        return [dict(value, id=key) for key, value in self._figures.items()]
