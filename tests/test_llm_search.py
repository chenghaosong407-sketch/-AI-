"""用 httpx.MockTransport 模拟豆包向量接口、流式对话(含加密思考内容)和博查搜索。"""
import asyncio
import json

import httpx
import pytest

from app import llm, search
from app.config import settings


@pytest.fixture
def mock_http(monkeypatch):
    holder = {}

    def install(handler):
        real = httpx.AsyncClient

        class Fake(real):
            def __init__(self, *a, **kw):
                kw["transport"] = httpx.MockTransport(handler)
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "AsyncClient", Fake)
        holder["handler"] = handler

    return install


def test_ark_multimodal_embedding(monkeypatch, mock_http):
    seen = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        return httpx.Response(200, json={"data": {"embedding": [0.1, 0.2, 0.3], "object": "embedding"}})

    mock_http(handler)
    monkeypatch.setattr(settings, "embed_model", "doubao-embedding-vision-251215")
    monkeypatch.setattr(settings, "embed_api_key", "k")
    monkeypatch.setattr(settings, "embed_api_style", "auto")
    monkeypatch.setattr(settings, "embed_dimensions", 0)
    assert llm.embed_style() == "ark_multimodal"
    vecs = asyncio.run(llm.embed(["片段一", "片段二"], kind="document"))
    assert vecs == [[0.1, 0.2, 0.3]] * 2
    path, body = seen[0]
    assert path.endswith("/embeddings/multimodal")
    assert body["input"] == [{"type": "text", "text": "片段一"}] or body["input"] == [{"type": "text", "text": "片段二"}]
    assert body["instructions"] == llm.ARK_CORPUS_INSTRUCTION and body["dimensions"] == 1024
    asyncio.run(llm.embed(["问题"], kind="query"))
    assert seen[-1][1]["instructions"] == llm.ARK_QUERY_INSTRUCTION


def test_openai_style_embedding(monkeypatch, mock_http):
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i)]} for i in range(len(body["input"]))]})

    mock_http(handler)
    monkeypatch.setattr(settings, "embed_model", "text-embedding-v3")
    monkeypatch.setattr(settings, "embed_api_key", "k")
    monkeypatch.setattr(settings, "embed_api_style", "auto")
    monkeypatch.setattr(settings, "embed_batch", 2)
    assert asyncio.run(llm.embed(["a", "b", "c"])) == [[0.0], [1.0], [0.0]]


def test_stream_collects_encrypted_reasoning(monkeypatch, mock_http):
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "需要查"}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "一下", "encrypted_content": "ENC1"}}]},
        {"choices": [{"index": 0, "delta": {"encrypted_content": "ENC2", "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": \"x\"}"}}]},
                      "finish_reason": "tool_calls"}]},
    ]
    sse = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    mock_http(lambda request: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "doubao-seed-2-1-pro-260915")

    async def run():
        return [e async for e in llm.stream_chat([{"role": "user", "content": "hi"}], [search.TOOL_SCHEMA])]

    events = asyncio.run(run())
    tc = next(e for e in events if e["type"] == "tool_calls")
    assert tc["reasoning"] == "需要查一下" and tc["encrypted"] == "ENC1ENC2"
    assert tc["calls"][0] == {"id": "call_1", "name": "web_search", "arguments": "{\"query\": \"x\"}"}


def _search_settings(monkeypatch):
    monkeypatch.setattr(settings, "search_provider", "bocha")
    monkeypatch.setattr(settings, "search_api_key", "s")


def test_search_error_message(monkeypatch, mock_http):
    _search_settings(monkeypatch)
    mock_http(lambda r: httpx.Response(403, json={"code": "403", "message": "You do not have enough money"}))
    res = asyncio.run(search.web_search("常州中考"))
    assert not res["ok"] and "余额不足" in res["error"]
    assert "未检索到" in search.format_results(res) or "未获得结果" in search.format_results(res)


def test_search_retries_without_time_limit(monkeypatch, mock_http):
    _search_settings(monkeypatch)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["freshness"])
        value = [] if body["freshness"] != "noLimit" else [
            {"name": "考试说明", "url": "https://e.com", "siteName": "e", "datePublished": "2026-03-01T08:00:00+08:00",
             "summary": "摘要"}]
        return httpx.Response(200, json={"code": 200, "log_id": "x", "msg": None,
                                         "data": {"_type": "SearchResponse", "webPages": {"value": value}}})

    mock_http(handler)
    res = asyncio.run(search.web_search("常州中考", "oneWeek"))
    assert calls == ["oneWeek", "noLimit"]
    assert res["ok"] and res["results"][0]["date"] == "2026-03-01"
