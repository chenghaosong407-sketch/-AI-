"""联网搜索(默认接博查 Web Search API)。未配置时返回"未检索到,需人工核实"提示。"""
from __future__ import annotations

import logging
import re
from datetime import date

import httpx

from .config import settings

log = logging.getLogger("search")

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "联网搜索中文互联网的最新信息。以下情况必须先调用:常州中考英语考试说明/题型/分值/当年变化;"
            "江苏译林版教材版本与单元安排;剑桥 Power Up / Think 最新版本、级别对应与官方资源;"
            "国家及江苏、常州校外培训政策;同行产品与市场动态;用户说'查一下'。"
            "关键词要具体,包含地区、年份、文件名称。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词,例如:2026年常州市中考英语 考试说明 题型"},
                "freshness": {
                    "type": "string",
                    "enum": ["noLimit", "oneYear", "oneMonth", "oneWeek"],
                    "description": "时间范围,一般用 noLimit(搜索引擎会自动按时效排序);只有明确要最近一周/一月的新闻时才缩小",
                },
            },
            "required": ["query"],
        },
    },
}

# SEARCH_MODE=auto 时,出现这些词就先搜索再回答
AUTO_TRIGGERS = ("查一下", "最新", "今年", "明年", "考试说明", "中考", "政策", "双减", "规定", "文件",
                 "译林", "新版", "版本", "分值", "题型", "同行", "竞品", "市场")


def should_auto_search(text: str) -> bool:
    return settings.search_mode == "auto" and any(k in text for k in AUTO_TRIGGERS)


def auto_query(text: str) -> str:
    text = re.sub(r"^\s*查一下\s*[:：]?", "", text).strip()
    return text[:80]


# 博查官方异常码 → 给管理员看的中文原因
_ERRORS = {
    400: "请求参数缺失",
    401: "API KEY 无效,请检查 .env 中的 SEARCH_API_KEY",
    403: "博查账户余额不足,请到 open.bocha.cn 充值",
    429: "搜索请求频率达到上限,请稍后再试",
    500: "搜索服务内部错误",
}


async def _request(query: str, freshness: str) -> dict:
    payload = {"query": query, "freshness": freshness, "summary": True, "count": settings.search_count}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
        resp = await client.post(settings.search_endpoint, json=payload,
                                 headers={"Authorization": f"Bearer {settings.search_api_key}"})
    try:
        body = resp.json()
    except ValueError:
        body = {}
    code = resp.status_code
    if code == 200 and str(body.get("code", 200)) not in ("200", ""):
        code = int(body["code"]) if str(body["code"]).isdigit() else 500
    if code != 200:
        reason = _ERRORS.get(code) or body.get("message") or body.get("msg") or f"HTTP {code}"
        raise RuntimeError(f"{reason}(错误码 {code})")
    return body


async def web_search(query: str, freshness: str = "noLimit") -> dict:
    if not settings.search_ready:
        return {"ok": False, "query": query, "results": [], "error": "未接入联网搜索"}
    freshness = freshness or "noLimit"
    try:
        body = await _request(query, freshness)
        items = _items(body)
        if not items and freshness != "noLimit":  # 官方提示:限定时间范围容易搜不到,放宽后重试一次
            items = _items(await _request(query, "noLimit"))
    except Exception as exc:
        log.warning("联网搜索失败:%s", exc)
        return {"ok": False, "query": query, "results": [], "error": str(exc)[:200]}
    results = []
    for item in items:
        results.append({
            "title": item.get("name", ""),
            "url": item.get("url", ""),
            "site": item.get("siteName", ""),
            "date": (item.get("datePublished") or item.get("dateLastCrawled") or "")[:10],
            "snippet": (item.get("summary") or item.get("snippet") or "")[:800],
        })
    return {"ok": bool(results), "query": query, "results": results, "error": "" if results else "无结果"}


def _items(body: dict) -> list[dict]:
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return ((data or {}).get("webPages") or {}).get("value") or []


def format_results(res: dict) -> str:
    today = date.today().isoformat()
    if not res["results"]:
        return (f"联网搜索\"{res['query']}\"未获得结果({res.get('error') or '无结果'})。"
                "相关内容在回答中写\"未检索到,需人工核实\",不要写(检索:…)。")
    lines = [f"联网搜索\"{res['query']}\"结果(检索日期 {today})。引用时标注(检索:站点或标题,发布日期):"]
    for i, r in enumerate(res["results"], start=1):
        lines.append(f"[{i}] {r['title']}|{r['site'] or '未知站点'}|发布:{r['date'] or '未标注'}\n{r['url']}\n{r['snippet']}")
    return "\n\n".join(lines)
