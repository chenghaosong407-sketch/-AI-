"""模拟模型先调用 web_search、再给出答案的完整流程。"""
import asyncio
import json

from app import chat, db, llm, search
from app.config import settings


def test_tool_call_loop(monkeypatch):
    calls = []

    async def fake_stream(messages, tools=None):
        calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if len(calls) == 1:
            yield {"type": "tool_calls", "calls": [
                {"id": "c1", "name": "web_search", "arguments": json.dumps({"query": "常州中考英语 题型"})}]}
            yield {"type": "done", "finish_reason": "tool_calls"}
        else:
            yield {"type": "content", "text": "结论:见检索结果。"}
            yield {"type": "done", "finish_reason": "stop"}

    async def fake_search(query, freshness="noLimit"):
        return {"ok": True, "query": query, "error": "",
                "results": [{"title": "考试说明", "url": "https://example.com/a", "site": "示例", "date": "2026-03-01",
                             "snippet": "题型说明"}]}

    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    monkeypatch.setattr(search, "web_search", fake_search)
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_model", "m")
    monkeypatch.setattr(settings, "search_provider", "bocha")
    monkeypatch.setattr(settings, "search_api_key", "s")
    monkeypatch.setattr(settings, "search_mode", "tool")

    user = db.query_one("SELECT * FROM users WHERE username='admin'")

    async def run():
        return [e async for e in chat.run_chat(user, None, "查一下:今年常州中考英语题型")]

    events = asyncio.run(run())
    assert any(e["type"] == "status" and "联网搜索" in e["text"] for e in events)
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "结论:见检索结果。"
    done = events[-1]
    assert done["type"] == "done" and done["searches"][0]["count"] == 1

    assert calls[0]["tools"][0]["function"]["name"] == "web_search"
    second = calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "c1"
    assert second[-1]["role"] == "tool" and "https://example.com/a" in second[-1]["content"]
    assert "已接入,工具名 web_search" in second[0]["content"]

    saved = db.query_one("SELECT content, meta FROM messages WHERE id=?", (done["message_id"],))
    assert saved["content"] == "结论:见检索结果。"
    assert json.loads(saved["meta"])["searches"][0]["query"] == "常州中考英语 题型"


def test_system_prompt_without_search():
    user = {"display_name": "张老师", "role": "teacher"}
    text = chat.system_prompt(user)
    assert text.startswith("【身份定位】")
    assert "张老师" in text and "未检索到,需人工核实" in text


def test_retrieval_query_uses_previous_for_short_followup():
    history = [{"role": "user", "content": "备课:Think 2 Unit 4 阅读课 90分钟"}, {"role": "assistant", "content": "……"}]
    q = chat.retrieval_query("展开:第三部分", history)
    assert "Think 2 Unit 4" in q and "第三部分" in q
