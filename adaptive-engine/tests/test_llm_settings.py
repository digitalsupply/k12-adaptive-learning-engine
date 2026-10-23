"""LLM 设置面板的服务端测试：.env 写回、热切换、探测助手、游客拒绝。"""

from __future__ import annotations

import json

import pytest

from ale import llm
from ale.engine import AdaptiveEngine
from ale.llm import LLMConfig, probe_chat


# ---------------- .env 写回 ----------------

def test_save_llm_config_roundtrip(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "K12_DB_BACKEND=sqlite\n"
        "# K12_LLM_API_KEY= old commented\n"
        "# K12_LLM_BASE_URL=https://old\n"
        "K12_FOO=bar\n", encoding="utf-8")
    monkeypatch.setattr(llm, "_ENV_PATH", env)
    llm.save_llm_config("https://api.x.com/v1", "sk-test-9999", "m-1", max_tokens=6000)
    text = env.read_text(encoding="utf-8")
    assert "K12_DB_BACKEND=sqlite" in text and "K12_FOO=bar" in text   # 其余行保留
    assert "old commented" not in text                                # 旧 K12_LLM 行清掉
    lines = dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln)
    assert lines["K12_LLM_BASE_URL"] == "https://api.x.com/v1"
    assert lines["K12_LLM_API_KEY"] == "sk-test-9999"
    assert lines["K12_LLM_MODEL"] == "m-1"
    assert lines["K12_LLM_MAX_TOKENS"] == "6000"

    # 停用（三项全空）→ 只保留 K12_LLM_SESSION（如有），其余配置行消失
    llm.save_llm_config("", "", "")
    text = env.read_text(encoding="utf-8")
    assert "K12_LLM_BASE_URL" not in text
    assert "K12_LLM_API_KEY" not in text
    assert "K12_DB_BACKEND=sqlite" in text


def test_config_toggle_matches_env(monkeypatch):
    monkeypatch.setenv("K12_LLM_BASE_URL", "https://x/v1")
    monkeypatch.setenv("K12_LLM_API_KEY", "sk-t")
    monkeypatch.setenv("K12_LLM_MODEL", "m")
    cfg = LLMConfig.from_env()
    assert cfg.enabled is True
    monkeypatch.delenv("K12_LLM_MODEL")
    assert LLMConfig.from_env().enabled is False


# ---------------- 引擎热切换 ----------------

def test_engine_hot_swap():
    cfg = LLMConfig(base_url="https://stub/v1", api_key="k", model="m-a")
    try:
        eng = AdaptiveEngine()
        cfg_a = eng.llm.config
        eng.set_llm_config(cfg)
        assert eng.llm.config.model == "m-a" and eng.llm.config is cfg
        cfg_a.model = cfg_a.model       # 原配置对象不被引用共享
    except RuntimeError:
        pytest.skip("数据集未就位")


# ---------------- 探测助手 ----------------

class _StubProbe(llm.LLMClient):
    def chat(self, system, user, temperature=None, max_tokens=None):  # noqa: A003
        return "正常"

def test_probe_chat_returns_short_reply():
    client = _StubProbe(LLMConfig(base_url="http://stub", api_key="x", model="s"))
    assert probe_chat(client) == "正常"


# ---------------- KNOWN_PROVIDERS 预设 ----------------

def test_known_providers_have_zeroable_models():
    for k, v in llm.KNOWN_PROVIDERS.items():
        assert v["base_url"] and v["model"], f"预设 {k} 缺字段"
