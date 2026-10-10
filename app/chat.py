"""对话主流程:隐私过滤 → 知识库检索 → (可选)联网搜索 → 流式调用模型 → 保存记录。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date
from typing import AsyncIterator

from . import db, kb, llm, search
from .config import BASE_DIR, settings
from .privacy import mask_pii

log = logging.getLogger("chat")

_COMMAND = re.compile(r"^\s*(答疑|备课|分层|衔接|做培训|评课|诊断|体系|家长话术|查一下|提交|展开|学习这份)\s*[:：]?\s*")
_NO_KB = {"菜单", "怎么用", "简版"}
MAX_TOOL_ROUNDS = 3


def _read_prompt(name: str) -> str:
    return (BASE_DIR / "prompts" / name).read_text(encoding="utf-8").strip()


def system_prompt(user: dict) -> str:
    if settings.search_ready and settings.search_mode == "tool" and settings.llm_supports_tools:
        status = "已接入,工具名 web_search。原则5列出的时效内容必须先调用它。"
    elif settings.search_ready:
        status = "已接入(自动模式):程序会在命中关键词时先搜索,结果放在【联网搜索结果】块中。没有该块时,时效内容写\"未检索到,需人工核实\"。"
    else:
        status = "当前未接入。需要联网核实的内容一律写\"未检索到,需人工核实\",不得写(检索:…)。"
    runtime = _read_prompt("runtime_rules.md").format(
        today=date.today().strftime("%Y年%m月%d日"),
        user_name=user.get("display_name") or user.get("username"),
        user_role="教研负责人/管理员" if user.get("role") == "admin" else "教师",
        search_status=status,
    )
    return _read_prompt("system_prompt.md") + "\n\n" + runtime


def retrieval_query(text: str, history: list[dict]) -> str:
    query = _COMMAND.sub("", text).strip()
    if len(query) < 20:  # "展开:第二部分"这类短追问,带上上一个问题
        prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        query = f"{_COMMAND.sub('', prev)[:200]} {query}".strip()
    return query[:500]


def _compose_user(text: str, attachments: list[dict], kb_block: str | None, search_block: str | None,
                  attachment_limit: int) -> str:
    parts = []
    if kb_block:
        parts.append(kb_block)
    if search_block:
        parts.append("【联网搜索结果】\n" + search_block)
    for att in attachments:
        body = att["text"][:attachment_limit]
        cut = "\n(附件过长,已截断)" if len(att["text"]) > attachment_limit else ""
        parts.append(f"【附件:{att['name']}】\n{body}{cut}")
    if parts:
        parts.append("【用户问题】\n" + text)
        return "\n\n".join(parts)
    return text


def _history_messages(conversation_id: int) -> list[dict]:
    rows = db.query(
        "SELECT role, content, meta FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
        (conversation_id, settings.history_messages),
    )
    out = []
    for row in reversed(rows):
        meta = json.loads(row["meta"] or "{}")
        content = row["content"]
        if row["role"] == "assistant" and meta.get("error") and content == "(未生成回答)":
            continue
        if row["role"] == "user" and meta.get("attachments"):
            content = _compose_user(content, meta["attachments"], None, None, 3000)
        out.append({"role": row["role"], "content": content})
    return out


def daily_count(user_id: int) -> int:
    return db.query_one(
        "SELECT COUNT(*) AS n FROM messages m JOIN conversations c ON c.id = m.conversation_id"
        " WHERE c.user_id=? AND m.role='user' AND m.created_at >= ?",
        (user_id, date.today().isoformat()),
    )["n"]


def _demo_reply(results: list[dict]) -> str:
    lines = ["【演示模式】尚未配置大模型(.env 中的 LLM_API_KEY / LLM_MODEL),以下仅展示知识库检索结果,便于检查资料是否入库正确。", ""]
    if results:
        for i, r in enumerate(results, 1):
            snippet = r["content"][:120].replace("\n", " ")
            lines.append(f"{i}. {r['tag']}《{r['filename']}》{snippet}……")
    else:
        lines.append("知识库暂无此项。")
    lines += ["", "配置方法见 README.md 第三节。"]
    return "\n".join(lines)


async def run_chat(user: dict, conversation_id: int | None, text: str,
                   attachments: list[dict] | None = None) -> AsyncIterator[dict]:
    attachments = attachments or []
    text, masked = mask_pii(text.strip())
    for att in attachments:
        att["text"], n = mask_pii(att["text"])
        masked += n

    if settings.daily_message_limit and daily_count(user["id"]) >= settings.daily_message_limit:
        yield {"type": "error", "text": f"今日提问已达上限({settings.daily_message_limit}次),明天再来或联系管理员调整。"}
        return

    if conversation_id:
        conv = db.query_one("SELECT * FROM conversations WHERE id=? AND user_id=?", (conversation_id, user["id"]))
        if not conv:
            yield {"type": "error", "text": "对话不存在"}
            return
    else:
        title = (_COMMAND.sub(lambda m: m.group(1) + ":", text) or (attachments[0]["name"] if attachments else "新对话"))[:24]
        conversation_id = db.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user["id"], title, db.now(), db.now()),
        )
    yield {"type": "meta", "conversation_id": conversation_id, "masked": masked}

    history = _history_messages(conversation_id)
    user_meta = {"attachments": [{"name": a["name"], "text": a["text"][:settings.attachment_max_chars]}
                                 for a in attachments]} if attachments else {}
    db.execute("INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?, 'user', ?, ?, ?)",
               (conversation_id, text, json.dumps(user_meta, ensure_ascii=False), db.now()))

    # 知识库检索
    results: list[dict] = []
    kb_block = None
    if text not in _NO_KB:
        query = retrieval_query(text or " ".join(a["name"] for a in attachments), history)
        results = await kb.retrieve(query)
        kb_block = kb.format_context(results)
    sources = [{"tag": r["tag"], "filename": r["filename"], "document_id": r["document_id"]} for r in results]
    if sources:
        yield {"type": "sources", "items": sources}

    # 自动模式下的预搜索
    searches: list[dict] = []
    search_block = None
    if settings.search_ready and search.should_auto_search(text):
        q = search.auto_query(text)
        yield {"type": "status", "text": f"正在联网搜索:{q}"}
        res = await search.web_search(q)
        searches.append({"query": q, "count": len(res["results"]), "urls": [r["url"] for r in res["results"]]})
        search_block = search.format_results(res)

    messages = [{"role": "system", "content": system_prompt(user)}, *history,
                {"role": "user", "content": _compose_user(text, attachments, kb_block, search_block,
                                                          settings.attachment_max_chars)}]
    use_tools = settings.search_ready and settings.search_mode == "tool" and settings.llm_supports_tools

    answer: list[str] = []
    error = ""
    try:
        if not settings.llm_ready:
            for piece in re.findall(r".{1,12}", _demo_reply(results), flags=re.S):
                answer.append(piece)
                yield {"type": "delta", "text": piece}
                await asyncio.sleep(0.01)
        else:
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                tools = [search.TOOL_SCHEMA] if use_tools and round_no < MAX_TOOL_ROUNDS else None
                calls: list[dict] = []
                thought = {"reasoning": "", "encrypted": ""}
                round_text: list[str] = []
                thinking_sent = False
                async for event in llm.stream_chat(messages, tools):
                    if event["type"] == "content":
                        round_text.append(event["text"])
                        answer.append(event["text"])
                        yield {"type": "delta", "text": event["text"]}
                    elif event["type"] == "reasoning" and not thinking_sent:
                        thinking_sent = True
                        yield {"type": "status", "text": "深度思考中……"}
                    elif event["type"] == "tool_calls":
                        calls = event["calls"]
                        thought = {"reasoning": event.get("reasoning", ""), "encrypted": event.get("encrypted", "")}
                if not calls:
                    break
                assistant_msg = {
                    "role": "assistant", "content": "".join(round_text),
                    "tool_calls": [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                   for c in calls],
                }
                # 深度思考模型:原样回传思考摘要与加密原文(豆包官方要求,缺失会降低效果)
                if thought["reasoning"]:
                    assistant_msg["reasoning_content"] = thought["reasoning"]
                if thought["encrypted"]:
                    assistant_msg["encrypted_content"] = thought["encrypted"]
                messages.append(assistant_msg)
                for call in calls:
                    try:
                        args = json.loads(call["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    q = str(args.get("query") or text)[:100]
                    if call["name"] != "web_search":
                        content = f"未知工具 {call['name']}"
                    else:
                        yield {"type": "status", "text": f"正在联网搜索:{q}"}
                        res = await search.web_search(q, args.get("freshness") or "noLimit")
                        searches.append({"query": q, "count": len(res["results"]),
                                         "urls": [r["url"] for r in res["results"]]})
                        content = search.format_results(res)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
    except llm.LLMError as exc:
        error = str(exc)
        log.warning("模型调用失败:%s", exc)
        yield {"type": "error", "text": f"模型调用失败:{exc}"}
    except Exception as exc:  # 网络中断等
        error = str(exc)
        log.exception("对话出错")
        yield {"type": "error", "text": f"出错了:{exc}"}
    finally:
        full = "".join(answer)
        if full or error:
            meta = {"sources": sources, "searches": searches}
            if error:
                meta["error"] = error[:300]
            msg_id = db.execute(
                "INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?, 'assistant', ?, ?, ?)",
                (conversation_id, full or "(未生成回答)", json.dumps(meta, ensure_ascii=False), db.now()),
            )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (db.now(), conversation_id))
        else:
            msg_id = None
    yield {"type": "done", "message_id": msg_id, "searches": searches}
