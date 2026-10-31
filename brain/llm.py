# -*- coding: utf-8 -*-
"""llm.py — 通用 LLM 客户端（OpenAI 兼容格式，异步 httpx）

所有已选服务商都提供 OpenAI 兼容的 /chat/completions 接口：
  - DeepSeek     https://api.deepseek.com                            （原生兼容）
  - OpenAI       https://api.openai.com/v1                           （原生）
  - Anthropic    https://api.anthropic.com/v1                        （官方 OpenAI SDK 兼容层）
  - Google Gemini https://generativelanguage.googleapis.com/v1beta/openai （官方 OpenAI 兼容端点）
  - OpenRouter   https://openrouter.ai/api/v1                        （聚合：一个 key 用遍所有模型）
  - 其它自托管（Ollama / vLLM / LM Studio 等）也基本是 OpenAI 兼容的。

因此一个客户端代码通吃：provider 选预设，base_url / model / api_key_env
可单独覆盖。返回 (content, usage)；余额/额度不足（HTTP 402）抛 BalanceError；
瞬时网络错误（DNS 抖动、代理闪断等）自动重试一次。

注意各服务商的差异：
  - 鉴权均为 `Authorization: Bearer <key>`（Anthropic/Gemini 兼容层同）；
    各家 key 前缀不同（sk- / sk-ant- / AIza…），这里只要求非空，不校验前缀；
  - OpenAI 新一代推理模型（o 系列/gpt-5 系）用 max_completion_tokens 且
    部分不支持 temperature —— 用 max_tokens_param / temperature=null 适配；
  - response_format json_object 不是所有兼容层都强制执行 —— 大脑侧
    对非 JSON 输出有纯文本回退，不受影响。
"""

import asyncio
import os

import httpx

# 服务商预设：provider 名 → 默认 base_url / 模型 / 环境变量名。
# 模型名是"建议值"：具体可用模型请按各家最新文档在 config 里覆盖。
PROVIDER_PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "api_key_env": "OPENAI_API_KEY",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-5",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "api_key_env": "GEMINI_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "custom": {
        "base_url": "",
        "model": "",
        "api_key_env": "LLM_API_KEY",
    },
}


class BalanceError(Exception):
    """余额 / 额度不足（HTTP 402）。"""


class LLMError(Exception):
    """其他调用失败（网络、超时、非 200 状态码等）。"""


class LLMClient:
    """OpenAI 兼容客户端。密钥优先级：显式传入 > 环境变量。"""

    def __init__(self, cfg: dict, timeout: float = 60.0, api_key: str | None = None):
        provider = (cfg.get("provider") or "deepseek").strip().lower()
        preset = PROVIDER_PRESETS.get(provider) or PROVIDER_PRESETS["custom"]
        self.provider = provider
        self.base_url = (cfg.get("base_url") or preset["base_url"] or "").rstrip("/")
        if not self.base_url:
            raise LLMError(
                "LLM base_url 未配置：请选一个 provider 预设，或在 config 的 "
                "llm.base_url 里填服务地址（OpenAI 兼容端点）"
            )
        self.model = cfg.get("model") or preset["model"]
        if not self.model:
            raise LLMError("LLM 模型名未配置：请在 config 的 llm.model 里填写")
        self.max_tokens = int(cfg.get("max_tokens") or 1024)
        # OpenAI 新推理模型：max_completion_tokens 替代 max_tokens
        self.max_tokens_param = cfg.get("max_tokens_param") or "max_tokens"
        # temperature=None → 请求体省略（部分推理模型不支持该参数）
        temperature = cfg.get("temperature", 0.7)
        self.temperature = float(temperature) if temperature is not None else None
        key_env = cfg.get("api_key_env") or preset["api_key_env"]
        key = (api_key or "").strip() or os.environ.get(key_env, "").strip()
        if not key:
            raise LLMError(
                f"API Key 缺失：请设置环境变量 {key_env}，或在 "
                f"websocket\\.apikey 文件中写入密钥"
                f"（首次运行 launcher 会自动引导）"
            )
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(self, messages: list, response_format: dict | None = None,
                   max_tokens: int | None = None):
        """一次对话补全。返回 (content: str, usage: dict)。

        瞬时网络错误（DNS 抖动、代理闪断等）自动重试一次。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            self.max_tokens_param: max_tokens or self.max_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if response_format:
            payload["response_format"] = response_format
        url = f"{self.base_url}/chat/completions"
        for attempt in (1, 2):
            try:
                resp = await self._client.post(url, json=payload)
                break
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise LLMError(f"网络错误：{exc!r}") from exc
                print(f"[LLM] 网络瞬时错误（第 {attempt} 次失败，0.5 秒后重试）：{exc!r}")
                await asyncio.sleep(0.5)
        if resp.status_code == 402:
            raise BalanceError("余额不足（HTTP 402）")
        if resp.status_code != 200:
            raise LLMError(f"API 返回 {resp.status_code}：{resp.text[:500]}")
        data = resp.json()
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        return content, usage


# 兼容旧名（一期工程及各处 import 用的是 DeepSeekClient）
DeepSeekClient = LLMClient
