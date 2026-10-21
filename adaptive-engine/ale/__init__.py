"""ale — Adaptive Learning Engine

基于 k12-knowledge-points 数据集的自适应学习引擎骨架：
知识点先修图谱 + 掌握度建模(BKT/Elo) + 错题归因 + 学习路径规划 + 复习调度。
"""

from .config import Config
from .engine import AdaptiveEngine

__all__ = ["AdaptiveEngine", "Config"]
__version__ = "0.1.0"
