"""全局配置。环境变量 K12_DATA_DIR 可覆盖数据集路径。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 默认数据集位置：本仓库的 ../split（即 k12-knowledge-points/split）
DATA_DIR = Path(os.environ.get("K12_DATA_DIR") or PROJECT_ROOT.parent / "split")
STUDENTS_DIR = PROJECT_ROOT / "data" / "students"
WEB_DIR = PROJECT_ROOT / "web"
FIGURES_DIR = PROJECT_ROOT / "figures"


@dataclass
class Config:
    # 知识点置信度过滤阈值。
    # 默认 0 = 全量加载。原因：音乐/体育等科目的条目大量来自"目录标题抽取"，
    # confidence 仅 0.2~0.4，若按 0.6 硬过滤会导致整科知识点静默消失。
    # 需要严格模式时设置环境变量 K12_MIN_CONFIDENCE=0.6。
    min_confidence: float = float(os.environ.get("K12_MIN_CONFIDENCE", "0"))

    # 低置信度提示阈值：低于此值的知识点在 UI 上以虚线弱化，但仍可用
    low_confidence_hint: float = 0.6

    # 掌握判定阈值
    mastered_threshold: float = 0.85        # BKT: P(掌握)
    elo_mastered_threshold: float = 0.80    # Elo: logistic 概率

    # 诊断参数
    diag_max_depth: int = 6      # 向上回溯的最大先修层数
    diag_decay: float = 0.6      # 每远离一层，证据权重衰减
    diag_max_causes: int = 5     # 最多返回的根因数

    # 路径规划
    plan_max_nodes: int = 40     # 单条学习路径最多包含的知识点数

    # 遗忘
    bkt_forget_per_day: float = 0.02   # BKT P(L) 向先验指数回落速率/天
    elo_forget_per_day: float = 0.01   # Elo 能力值向初始值回落速率/天

    data_dir: Path = field(default_factory=lambda: DATA_DIR)
    students_dir: Path = field(default_factory=lambda: STUDENTS_DIR)


DEFAULT = Config()
