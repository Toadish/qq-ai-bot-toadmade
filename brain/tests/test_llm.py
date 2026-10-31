# -*- coding: utf-8 -*-
"""
test_llm.py — 通用 LLM 客户端（OpenAI 兼容）离线测试
======================================================
运行：..\..\websocket\.venv\Scripts\python.exe test_llm.py

覆盖：服务商预设解析（deepseek/openai/anthropic/gemini/openrouter/custom +
     覆盖）、请求体形状（max_tokens_param、temperature=null 省略、
     response_format 透传）、非 sk- 前缀密钥可接受、瞬时网络错误重试。
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 工作区根

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx                                 # noqa: E402
from brain.llm import LLMClient, LLMError, PROVIDER_PRESETS  # noqa: E402


def t_provider_presets():
    # 各服务商默认地址/模型
    assert PROVIDER_PRESETS["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert PROVIDER_PRESETS["openai"]["base_url"] == "https://api.openai.com/v1"
    assert PROVIDER_PRESETS["anthropic"]["base_url"] == "https://api.anthropic.com/v1"
    assert PROVIDER_PRESETS["gemini"]["base_url"].endswith("/v1beta/openai")
    assert PROVIDER_PRESETS["openrouter"]["base_url"] == "https://openrouter.ai/api/v1"
    # gemini 预设 + 非 sk- 前缀密钥可接受
    c = LLMClient({"provider": "gemini"}, api_key="AIza-test-key")
    assert c.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert c.model == "gemini-2.5-flash"
    asyncio.get_event_loop().run_until_complete(c.close())
    # anthropic 预设
    c2 = LLMClient({"provider": "anthropic"}, api_key="sk-ant-test")
    assert c2.base_url == "https://api.anthropic.com/v1" and c2.model.startswith("claude")
    asyncio.get_event_loop().run_until_complete(c2.close())
    # custom + 全字段覆盖
    c3 = LLMClient({
        "provider": "custom",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen3:8b",
        "api_key_env": "OLLAMA_KEY",
    }, api_key="sk-x")
    assert c3.base_url == "http://127.0.0.1:11434/v1" and c3.model == "qwen3:8b"
    asyncio.get_event_loop().run_until_complete(c3.close())
    print("PASS 服务商预设：五家预设解析正确、custom 覆盖、非 sk- 密钥可接受")


def _patched_client(cfg, handler, api_key="sk-test"):
    calls = []
    real_factory = httpx.AsyncClient

    def wrap(request):
        calls.append(request)
        return handler(request, calls)

    transport = httpx.MockTransport(wrap)

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_factory(*args, **kwargs)

    patcher = mock.patch.object(httpx, "AsyncClient", side_effect=factory)
    patcher.start()
    client = LLMClient(cfg, api_key=api_key)
    return client, calls, patcher


def _ok_response():
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "好。"}}],
        "usage": {"total_tokens": 10},
    })


async def t_payload_shapes():
    # max_tokens_param：OpenAI 新推理模型
    client, calls, patcher = _patched_client(
        {"max_tokens_param": "max_completion_tokens"}, lambda req, c: _ok_response()
    )
    try:
        await client.chat([{"role": "user", "content": "在吗"}])
        payload = json.loads(calls[0].read())
        assert "max_completion_tokens" in payload and "max_tokens" not in payload
        await client.close()
    finally:
        patcher.stop()
    # temperature=None：省略该参数（部分推理模型不支持）
    client, calls, patcher = _patched_client(
        {"temperature": None}, lambda req, c: _ok_response()
    )
    try:
        await client.chat([{"role": "user", "content": "在吗"}])
        payload = json.loads(calls[0].read())
        assert "temperature" not in payload
        await client.close()
    finally:
        patcher.stop()
    # response_format 透传
    client, calls, patcher = _patched_client(
        {}, lambda req, c: _ok_response()
    )
    try:
        await client.chat([{"role": "user", "content": "在吗"}],
                          response_format={"type": "json_object"})
        payload = json.loads(calls[0].read())
        assert payload["response_format"] == {"type": "json_object"}
        await client.close()
    finally:
        patcher.stop()
    print("PASS 请求体形状：max_tokens 参数名可换、temperature 可省略、response_format 透传")


async def t_retry_on_transient():
    def handler(request, calls):
        if len(calls) == 1:
            raise httpx.ConnectError("getaddrinfo failed", request=request)
        return _ok_response()
    client, calls, patcher = _patched_client({}, handler)
    try:
        content, usage = await client.chat([{"role": "user", "content": "在吗"}])
        assert content == "好。"
        assert usage["total_tokens"] == 10
        assert len(calls) == 2, "瞬时错误应自动重试一次后成功"
        await client.close()
    finally:
        patcher.stop()
    print("PASS LLM 重试：瞬时网络错误重试一次后成功")


async def t_double_failure_raises():
    def handler(request, calls):
        raise httpx.ConnectError("getaddrinfo failed", request=request)
    client, calls, patcher = _patched_client({}, handler)
    try:
        try:
            await client.chat([{"role": "user", "content": "在吗"}])
            raise AssertionError("连续两次失败应抛 LLMError")
        except LLMError as exc:
            assert "网络错误" in str(exc)
        assert len(calls) == 2
        await client.close()
    finally:
        patcher.stop()
    print("PASS LLM 重试：连续两次失败抛 LLMError（最多重试一次）")


if __name__ == "__main__":
    t_provider_presets()
    asyncio.run(t_payload_shapes())
    asyncio.run(t_retry_on_transient())
    asyncio.run(t_double_failure_raises())
    print("\n全部测试通过 ✅")
