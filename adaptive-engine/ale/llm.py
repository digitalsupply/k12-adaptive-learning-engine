"""OpenAI 兼容的 LLM 客户端（纯标准库实现）。

配置走环境变量 / Config：

    K12_LLM_BASE_URL   如 https://api.deepseek.com/v1
    K12_LLM_API_KEY    对应 Provider 的 API Key
    K12_LLM_MODEL      如 deepseek-chat
    K12_LLM_TIMEOUT    请求超时秒数（默认 45）

三项都配齐才算启用；缺任何一项 `enabled` 为 False，调用方自行退回到
模板出题。不引第三方依赖：OpenAI 兼容接口的请求体就是 JSON，urllib 足够。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# 常见 Provider 的默认配置，供 CLI 提示与测试使用
KNOWN_PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "model": "qwen-plus"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "opencode": {"base_url": "https://opencode.ai/inference/openai/v1",
                 "model": "qwen3.8-flash"},
}


_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path=_ENV_PATH) -> dict[str, str]:
    """加载 .env 文件（KEY=VALUE，# 注释），不覆盖已有环境变量。

    手写实现不引 python-dotenv：只需支持最普通的格式。返回读到的键值。
    """
    path = Path(path)
    loaded: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return loaded
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and os.environ.get(key) is None:
            os.environ[key] = value
            loaded[key] = value
    return loaded


class LLMError(RuntimeError):
    """LLM 调用失败。message 里附有人话版的原因与修法。"""


@dataclass(slots=True)
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # 推理型模型(如 minimax/deepseek)思考也要十几秒,90s 是稳妥下限;
    # 出题一次 35s 左右,45s 会误杀。
    timeout: float = 90.0
    # 推理模型的 reasoning_content 也占输出预算,出题 JSON 约 400 token,
    # 思考可能吃掉 1~3k,给 6000 才不会半路截断。
    max_tokens: int = 6000
    temperature: float = 0.7
    # 稳定会话 ID：OpenCode Go 网关要求 x-opencode-session 头做路由亲和，
    # 其余 OpenAI 兼容服务会忽略它。默认从 .env 的 K12_LLM_SESSION 读；
    # 没配就自动生成一份并存 data/llm_session_id（重启不变）。
    session_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "LLMConfig":
        env = os.environ if env is None else env
        g = env.get
        cfg = cls(
            base_url=(g("K12_LLM_BASE_URL") or "").rstrip("/"),
            api_key=g("K12_LLM_API_KEY") or "",
            model=g("K12_LLM_MODEL") or "",
        )
        try:
            cfg.timeout = float(g("K12_LLM_TIMEOUT") or cfg.timeout)
        except ValueError:
            pass
        try:
            cfg.max_tokens = int(g("K12_LLM_MAX_TOKENS") or cfg.max_tokens)
        except ValueError:
            pass
        cfg.session_id = (g("K12_LLM_SESSION") or "").strip()
        if not cfg.session_id:
            cfg.session_id = _default_session_id()
        return cfg

    @classmethod
    def for_provider(cls, name: str, api_key: str = "",
                     env: dict[str, str] | None = None) -> "LLMConfig":
        """按 Provider 名生成默认配置（base_url/model 用内置表）。"""
        if name not in KNOWN_PROVIDERS:
            raise KeyError(f"未知 Provider：{name}（可选：{', '.join(KNOWN_PROVIDERS)}）")
        return cls.from_env({**(env or {}), **{
            "K12_LLM_BASE_URL": KNOWN_PROVIDERS[name]["base_url"],
            "K12_LLM_MODEL": KNOWN_PROVIDERS[name]["model"],
            **({"K12_LLM_API_KEY": api_key} if api_key else {}),
        }})


class LLMClient:
    """最薄的一层：POST {base_url}/chat/completions，返回文本内容。"""

    def __init__(self, config: LLMConfig):
        self.config = config

    def chat(self, system: str, user: str, temperature: float | None = None,
             max_tokens: int | None = None) -> str:
        if not self.config.enabled:
            raise LLMError("LLM 未配置：需要 K12_LLM_BASE_URL / K12_LLM_API_KEY / K12_LLM_MODEL 三项")
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                # Cloudflare 会拦 Python-urllib 默认 UA（403 error 1010）；
                # OpenCode Go 要求正式 UA + 稳定会话头（路由亲和/提示缓存）
                "User-Agent": "k12-adaptive-engine/0.2 (quiz-generation)",
                "x-opencode-session": self.config.session_id,
            },
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001 - 读不到响应体就只报状态码
                pass
            hint = {401: "API Key 无效或过期", 402: "余额不足",
                    429: "触发限流,请稍后重试或降低出题频率",
                    404: "模型名或 base_url 有误"}.get(e.code, f"HTTP {e.code}")
            raise LLMError(f"LLM 调用失败（{hint}）：{detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"LLM 服务无法连接：{e}") from e
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 响应格式异常：{json.dumps(data, ensure_ascii=False)[:300]}") from e


def _default_session_id() -> str:
    """进程级稳定会话 ID（OpenCode Go 网关要求的 x-opencode-session 头）：
    优先读持久化文件 data/llm_session_id，没有则生成一次并存盘，重启不变。"""
    path = Path(__file__).resolve().parent.parent / "data" / "llm_session_id"
    try:
        sid = path.read_text(encoding="utf-8").strip()
        if len(sid) >= 12:
            return sid
    except OSError:
        pass
    import secrets
    sid = secrets.token_hex(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sid, encoding="utf-8")
    except OSError:
        pass
    return sid


def parse_json_block(text: str) -> dict:
    """从模型回复里抠出 JSON。容忍 ```json 代码块与前后废话。"""
    text = text.strip()
    if "```" in text:                       # 提取代码块
        for seg in text.split("```"):
            seg = seg.strip()
            if seg.startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{"):
                text = seg
                break
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("LLM 回复里没有 JSON")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM 返回的 JSON 解析失败：{e}") from e
    if not isinstance(obj, dict):
        raise LLMError("LLM 返回的不是 JSON 对象")
    return obj
