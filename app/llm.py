"""OpenAI 兼容接口客户端:流式对话(含工具调用)与文本向量。

豆包(火山方舟)、DeepSeek、通义千问(DashScope 兼容模式)、Kimi 等都走同一套对话协议,
只需在 .env 中改 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY。
向量接口有两种:通用的 /embeddings,以及豆包多模态向量模型专用的 /embeddings/multimodal。
"""
from __future__ import annotations

import asyncio
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
    {"type": "tool_calls", "calls": [...], "reasoning": str, "encrypted": str}
        本轮结束时模型要求调用的工具。豆包深度思考模型会同时给出思考摘要和加密原文,
        回填工具结果时需原样带回,否则效果下降。
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
    reasoning: list[str] = []
    encrypted: list[str] = []
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
                        reasoning.append(delta["reasoning_content"])
                        yield {"type": "reasoning", "text": delta["reasoning_content"]}
                    if delta.get("encrypted_content"):
                        encrypted.append(delta["encrypted_content"])
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
        yield {"type": "tool_calls", "calls": ordered, "reasoning": "".join(reasoning),
               "encrypted": "".join(encrypted)}
    yield {"type": "done", "finish_reason": finish_reason}


# ---------------------------------------------------------------- 向量

# 豆包多模态向量模型的官方指令模板(非对称检索:短问题查长文档)
ARK_QUERY_INSTRUCTION = "Target_modality: text.\nInstruction:为这个句子生成表示以用于检索相关文章\nQuery:"
ARK_CORPUS_INSTRUCTION = "Instruction:Compress the text into one word.\nQuery:"


def embed_style() -> str:
    style = settings.embed_api_style
    if style in ("openai", "ark_multimodal"):
        return style
    return "ark_multimodal" if "embedding-vision" in settings.embed_model.lower() else "openai"


async def _embed_openai(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), settings.embed_batch):
        batch = texts[start:start + settings.embed_batch]
        body: dict[str, Any] = {"model": settings.embed_model, "input": batch, "encoding_format": "float"}
        if settings.embed_dimensions:
            body["dimensions"] = settings.embed_dimensions
        resp = await client.post(f"{settings.embed_base_url}/embeddings",
                                 headers=_headers(settings.embed_api_key), json=body)
        if resp.status_code != 200:
            raise LLMError(f"向量接口返回 {resp.status_code}:{resp.text[:500]}")
        items = sorted(resp.json().get("data", []), key=lambda d: d.get("index", 0))
        if len(items) != len(batch):
            raise LLMError("向量接口返回数量与请求不一致")
        vectors.extend(item["embedding"] for item in items)
    return vectors


async def _embed_ark_multimodal(client: httpx.AsyncClient, texts: list[str], kind: str) -> list[list[float]]:
    """豆包多模态向量接口每次请求只返回一个向量,这里并发逐条请求。"""
    instruction = ARK_QUERY_INSTRUCTION if kind == "query" else ARK_CORPUS_INSTRUCTION
    sem = asyncio.Semaphore(max(1, settings.embed_concurrency))

    async def one(text: str) -> list[float]:
        body: dict[str, Any] = {
            "model": settings.embed_model,
            "input": [{"type": "text", "text": text}],
            "instructions": instruction,
            "encoding_format": "float",
            "dimensions": settings.embed_dimensions or 1024,
        }
        async with sem:
            for attempt in range(3):
                resp = await client.post(f"{settings.embed_base_url}/embeddings/multimodal",
                                         headers=_headers(settings.embed_api_key), json=body)
                if resp.status_code == 429 and attempt < 2:  # 限流时稍等重试
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                break
        if resp.status_code != 200:
            raise LLMError(f"向量接口返回 {resp.status_code}:{resp.text[:500]}")
        data = resp.json().get("data") or {}
        if isinstance(data, list):  # 兼容列表形式
            data = data[0] if data else {}
        embedding = data.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise LLMError("向量接口未返回 float 向量")
        return embedding

    return list(await asyncio.gather(*(one(t) for t in texts)))


async def embed(texts: list[str], kind: str = "document") -> list[list[float]]:
    """kind: document=知识库片段,query=用户问题。豆包多模态向量模型对两者使用不同指令。"""
    if not settings.embed_ready:
        raise LLMError("未配置向量模型")
    texts = [t[:4000] for t in texts]
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
        if embed_style() == "ark_multimodal":
            return await _embed_ark_multimodal(client, texts, kind)
        return await _embed_openai(client, texts)
