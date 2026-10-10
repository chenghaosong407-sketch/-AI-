"""OpenAI 兼容接口客户端:流式对话(含工具调用)与文本向量。

豆包(火山方舟)、DeepSeek、通义千问(DashScope 兼容模式)、Kimi 等都走同一套协议,
只需在 .env 中改 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .config import settings


class LLMError(Exception):
    pass


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


async def stream_chat(messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[dict]:
    """逐块产出事件:
    {"type": "content", "text": ...}      正文增量
    {"type": "reasoning", "text": ...}    深度思考增量(部分模型有)
    {"type": "tool_calls", "calls": [...]} 本轮结束时模型要求调用的工具
    {"type": "done", "finish_reason": ...}
    """
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": True,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }
    if tools:
        payload["tools"] = tools
    payload.update(settings.llm_extra_body)

    calls: dict[int, dict] = {}
    finish_reason = None
    timeout = httpx.Timeout(settings.llm_timeout, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{settings.llm_base_url}/chat/completions",
                                 headers=_headers(settings.llm_api_key), json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="ignore")[:500]
                raise LLMError(f"模型接口返回 {resp.status_code}:{body}")
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"])[:500])
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        yield {"type": "reasoning", "text": delta["reasoning_content"]}
                    if delta.get("content"):
                        yield {"type": "content", "text": delta["content"]}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
    if calls:
        ordered = [calls[i] for i in sorted(calls)]
        for i, call in enumerate(ordered):
            call["id"] = call["id"] or f"call_{i}"
        yield {"type": "tool_calls", "calls": ordered}
    yield {"type": "done", "finish_reason": finish_reason}


async def complete(messages: list[dict]) -> str:
    """非流式的简单调用(用于生成对话标题等)。"""
    text = []
    async for event in stream_chat(messages):
        if event["type"] == "content":
            text.append(event["text"])
    return "".join(text)


async def embed(texts: list[str]) -> list[list[float]]:
    if not settings.embed_ready:
        raise LLMError("未配置向量模型")
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
        for start in range(0, len(texts), settings.embed_batch):
            batch = [t[:4000] for t in texts[start:start + settings.embed_batch]]
            resp = await client.post(
                f"{settings.embed_base_url}/embeddings",
                headers=_headers(settings.embed_api_key),
                json={"model": settings.embed_model, "input": batch, "encoding_format": "float"},
            )
            if resp.status_code != 200:
                raise LLMError(f"向量接口返回 {resp.status_code}:{resp.text[:500]}")
            items = sorted(resp.json().get("data", []), key=lambda d: d.get("index", 0))
            if len(items) != len(batch):
                raise LLMError("向量接口返回数量与请求不一致")
            vectors.extend(item["embedding"] for item in items)
    return vectors
