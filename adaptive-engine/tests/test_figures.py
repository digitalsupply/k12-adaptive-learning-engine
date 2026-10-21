"""交互图形（欧姆定律实验台）的注册表与接入测试。

图形本身是离线构建的静态资源，这里只验证：
  1. 注册表能稳定读出「知识点 UUID → 图形」的映射；
  2. 注册表缺失/损坏时优雅降级，不影响主引擎；
  3. 详情接口按需注入 figure 字段，且构建产物确实存在。
"""

from __future__ import annotations

import json

import pytest

from ale.figures import FigureRegistry

OHM_KP = "81d142c4-baa5-432f-97d1-af8922180401"   # 欧姆定律表达式与适用条件


def test_registry_maps_kp_to_figure():
    reg = FigureRegistry()
    fig = reg.for_kp(OHM_KP)
    assert fig is not None
    assert fig["id"] == "ohm-lab"
    assert fig["embed_url"].startswith("/figures/")
    assert fig["title"]


def test_registry_unknown_kp_is_none():
    assert FigureRegistry().for_kp("00000000-0000-0000-0000-000000000000") is None


def test_registry_missing_file_degrades(tmp_path):
    reg = FigureRegistry(tmp_path / "missing.json")
    assert reg.all() == []
    assert reg.for_kp(OHM_KP) is None


def test_registry_drops_dangling_mapping(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "figures": {"a": {"id": "a", "embed_url": "/figures/a/embed.html"}},
        "by_kp": {"k1": "a", "k2": "ghost"},
    }), encoding="utf-8")
    reg = FigureRegistry(path)
    assert reg.for_kp("k1")["id"] == "a"
    assert reg.for_kp("k2") is None


def test_build_artifacts_exist():
    """注册表指向的静态页面必须真的在磁盘上。"""
    reg = FigureRegistry()
    fig = reg.for_kp(OHM_KP)
    root = reg.path.parent
    for key in ("embed_url", "embed_dark_url", "standalone_url", "standalone_dark_url"):
        rel = fig[key].split("/figures/", 1)[1]
        assert (root / rel).exists(), f"{key} 指向的文件不存在：{rel}"


def test_kp_detail_injects_figure():
    """详情接口：注册过的知识点带 figure，没注册的不带。"""
    import server
    from ale.config import Config

    if not (Config().data_dir / "index.json").exists():
        pytest.skip("数据集未就位")

    detail = server.kp_detail(OHM_KP)
    assert detail["figure"]["id"] == "ohm-lab"

    kps, _ = server.eng.dataset.group("初中", "物理")
    plain = next(kid for kid in kps if kid != OHM_KP and kid not in server.figures._by_kp)
    assert "figure" not in server.kp_detail(plain)
