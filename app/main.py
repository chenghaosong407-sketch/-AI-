"""Web 服务入口:页面、登录、对话(SSE 流式)、管理后台、扣子插件接口。"""
from __future__ import annotations

import hmac
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, chat, coze_export, db, kb, parsers
from .config import BASE_DIR, settings

log = logging.getLogger("app")
STATIC = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    db.get_conn()
    password = auth.ensure_admin()
    if password == "env":
        log.warning("已创建管理员账号:%s,密码为 .env 中的 ADMIN_PASSWORD。", settings.admin_username)
    elif password:
        log.warning("=" * 60)
        log.warning("已创建管理员账号:%s  初始密码:%s", settings.admin_username, password)
        log.warning("请记下密码,登录后系统会要求修改。")
        log.warning("=" * 60)
    kb.reload_index()
    if not settings.llm_ready:
        log.warning("未配置 LLM_API_KEY / LLM_MODEL,当前为演示模式(只返回知识库检索结果)。")
    yield


app = FastAPI(title=settings.app_title, lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(ValueError)
async def _value_error(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------------------------------------------------------- 页面

@app.get("/")
def index_page():
    return FileResponse(STATIC / "index.html")


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


# ---------------------------------------------------------------- 登录与个人

class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/login")
def api_login(body: LoginIn):
    token = auth.login(body.username.strip(), body.password)
    if not token:
        raise HTTPException(401, "用户名或密码错误")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=settings.session_days * 86400,
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.logout(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/api/me")
def api_me(user: dict = Depends(auth.current_user)):
    return {
        "user": user,
        "app": {
            "title": settings.app_title,
            "llm_ready": settings.llm_ready,
            "model": settings.llm_model,
            "search_ready": settings.search_ready,
            "embed_ready": settings.embed_ready,
            "daily_limit": settings.daily_message_limit,
            "today_count": chat.daily_count(user["id"]),
        },
    }


@app.post("/api/me/password")
def api_change_password(body: PasswordIn, user: dict = Depends(auth.current_user)):
    row = db.query_one("SELECT password_hash FROM users WHERE id=?", (user["id"],))
    if not auth.verify_password(body.old_password, row["password_hash"]):
        raise HTTPException(400, "原密码不正确")
    if len(body.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    db.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
               (auth.hash_password(body.new_password), user["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- 对话

def _own_conversation(conv_id: int, user: dict) -> dict:
    conv = db.query_one("SELECT * FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"]))
    if not conv:
        raise HTTPException(404, "对话不存在")
    return conv


@app.get("/api/conversations")
def list_conversations(user: dict = Depends(auth.current_user)):
    return db.query("SELECT id, title, updated_at FROM conversations WHERE user_id=?"
                    " ORDER BY updated_at DESC LIMIT 200", (user["id"],))


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: int, user: dict = Depends(auth.current_user)):
    conv = _own_conversation(conv_id, user)
    rows = db.query("SELECT id, role, content, meta, created_at FROM messages"
                    " WHERE conversation_id=? ORDER BY id", (conv_id,))
    for r in rows:
        meta = json.loads(r.pop("meta") or "{}")
        r["sources"] = meta.get("sources", [])
        r["searches"] = meta.get("searches", [])
        r["attachments"] = [a["name"] for a in meta.get("attachments", [])]
    return {"conversation": conv, "messages": rows}


class RenameIn(BaseModel):
    title: str


@app.patch("/api/conversations/{conv_id}")
def rename_conversation(conv_id: int, body: RenameIn, user: dict = Depends(auth.current_user)):
    _own_conversation(conv_id, user)
    db.execute("UPDATE conversations SET title=? WHERE id=?", (body.title.strip()[:40] or "未命名", conv_id))
    return {"ok": True}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: int, user: dict = Depends(auth.current_user)):
    _own_conversation(conv_id, user)
    db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    return {"ok": True}


async def _read_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"{f.filename} 超过 {settings.max_upload_mb}MB")
    return data


@app.post("/api/chat")
async def api_chat(text: str = Form(""), conversation_id: int | None = Form(None),
                   files: list[UploadFile] = File(default=[]), user: dict = Depends(auth.current_user)):
    attachments = []
    for f in files[:5]:
        if not f.filename:
            continue
        try:
            attachments.append({"name": f.filename, "text": parsers.extract_text(f.filename, await _read_upload(f))})
        except parsers.ParseError as exc:
            raise HTTPException(400, f"{f.filename}:{exc}")
    if not text.strip() and not attachments:
        raise HTTPException(400, "请输入问题或上传附件")
    if not text.strip():
        text = "学习这份"

    async def stream():
        async for event in chat.run_chat(user, conversation_id, text, attachments):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- 入库卡(教师提交)

class SubmissionIn(BaseModel):
    content: str
    conversation_id: int | None = None


@app.post("/api/submissions")
def create_submission(body: SubmissionIn, user: dict = Depends(auth.current_user)):
    content = body.content.strip()
    if len(content) < 30:
        raise HTTPException(400, "入库卡内容太短")
    if re.search(r"提交人[:：]", content):
        content = re.sub(r"提交人[:：][^\n]*", f"提交人:{user['display_name']}", content, count=1)
    sid = db.execute(
        "INSERT INTO submissions (user_id, conversation_id, content, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], body.conversation_id, content[:20000], db.now()),
    )
    return {"ok": True, "id": sid}


@app.get("/api/submissions/mine")
def my_submissions(user: dict = Depends(auth.current_user)):
    return db.query("SELECT id, substr(content,1,200) AS preview, status, review_note, created_at, reviewed_at"
                    " FROM submissions WHERE user_id=? ORDER BY id DESC LIMIT 100", (user["id"],))


# ---------------------------------------------------------------- 管理后台:账号

class UsersIn(BaseModel):
    lines: str  # 每行:用户名,姓名,初始密码,角色(teacher/admin,可省略)


class UserPatch(BaseModel):
    display_name: str | None = None
    role: str | None = None
    active: bool | None = None
    reset_password: str | None = None


@app.get("/api/admin/users")
def admin_users(_: dict = Depends(auth.require_admin)):
    return db.query(
        "SELECT u.id, u.username, u.display_name, u.role, u.active, u.created_at,"
        " (SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id=m.conversation_id"
        "   WHERE c.user_id=u.id AND m.role='user') AS questions"
        " FROM users u ORDER BY u.id")


@app.post("/api/admin/users")
def admin_create_users(body: UsersIn, _: dict = Depends(auth.require_admin)):
    created, errors = [], []
    for n, raw in enumerate(body.lines.splitlines(), start=1):
        parts = [p.strip() for p in re.split(r"[,，\t]", raw) if p.strip()]
        if not parts:
            continue
        if len(parts) < 3:
            errors.append(f"第{n}行格式不对(需要:用户名,姓名,初始密码[,角色])")
            continue
        username, name, password = parts[:3]
        role = parts[3] if len(parts) > 3 else "teacher"
        role = {"管理员": "admin", "教研负责人": "admin", "教师": "teacher", "老师": "teacher"}.get(role, role)
        if len(password) < 6:
            errors.append(f"第{n}行密码至少 6 位")
            continue
        try:
            auth.create_user(username, name, password, role)
            created.append(username)
        except Exception as exc:
            errors.append(f"第{n}行 {username}:{'用户名已存在' if 'UNIQUE' in str(exc) else exc}")
    return {"created": created, "errors": errors}


@app.patch("/api/admin/users/{user_id}")
def admin_patch_user(user_id: int, body: UserPatch, admin: dict = Depends(auth.require_admin)):
    if not db.query_one("SELECT id FROM users WHERE id=?", (user_id,)):
        raise HTTPException(404, "用户不存在")
    if user_id == admin["id"] and (body.active is False or body.role == "teacher"):
        raise HTTPException(400, "不能停用或降级自己")
    if body.display_name is not None:
        db.execute("UPDATE users SET display_name=? WHERE id=?", (body.display_name.strip(), user_id))
    if body.role in ("admin", "teacher"):
        db.execute("UPDATE users SET role=? WHERE id=?", (body.role, user_id))
    if body.active is not None:
        db.execute("UPDATE users SET active=? WHERE id=?", (int(body.active), user_id))
        if not body.active:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    if body.reset_password:
        if len(body.reset_password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        db.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                   (auth.hash_password(body.reset_password), user_id))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    return {"ok": True}


# ---------------------------------------------------------------- 管理后台:知识库

@app.get("/api/admin/kb/stats")
def admin_kb_stats(_: dict = Depends(auth.require_admin)):
    return kb.stats()


@app.get("/api/admin/documents")
def admin_documents(tier: int | None = None, q: str = "", _: dict = Depends(auth.require_admin)):
    sql = ("SELECT d.id, d.filename, d.tier, d.chars, d.chunk_count, d.note, d.created_at, u.display_name AS uploader"
           " FROM documents d LEFT JOIN users u ON u.id=d.uploaded_by WHERE 1=1")
    params: list = []
    if tier:
        sql += " AND d.tier=?"
        params.append(tier)
    if q.strip():
        sql += " AND (d.filename LIKE ? OR d.note LIKE ?)"
        params += [f"%{q.strip()}%"] * 2
    return db.query(sql + " ORDER BY d.id DESC LIMIT 500", params)


@app.post("/api/admin/documents")
async def admin_upload(tier: int = Form(...), note: str = Form(""), files: list[UploadFile] = File(...),
                       admin: dict = Depends(auth.require_admin)):
    if tier not in (0, 1, 2, 3, 4):
        raise HTTPException(400, "层级必须是 0(自动)或 1-4")
    results = []
    for f in files:
        try:
            data = await _read_upload(f)
            text = parsers.extract_text(f.filename, data)
            stored = settings.upload_dir / f"{uuid.uuid4().hex}{Path(f.filename).suffix.lower()}"
            stored.write_bytes(data)
            doc = await kb.ingest_text(f.filename, text, tier, admin["id"], str(stored), note.strip())
            results.append({"filename": f.filename, "ok": True, **doc})
        except (parsers.ParseError, ValueError) as exc:
            results.append({"filename": f.filename, "ok": False, "error": str(exc)})
        except HTTPException as exc:
            results.append({"filename": f.filename, "ok": False, "error": exc.detail})
    return {"results": results}


class DocPatch(BaseModel):
    tier: int


@app.patch("/api/admin/documents/{doc_id}")
def admin_doc_tier(doc_id: int, body: DocPatch, _: dict = Depends(auth.require_admin)):
    kb.set_tier(doc_id, body.tier)
    return {"ok": True}


@app.delete("/api/admin/documents/{doc_id}")
def admin_doc_delete(doc_id: int, _: dict = Depends(auth.require_admin)):
    doc = kb.delete_document(doc_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    if doc.get("file_path"):
        Path(doc["file_path"]).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/admin/documents/{doc_id}/chunks")
def admin_doc_chunks(doc_id: int, _: dict = Depends(auth.require_admin)):
    return db.query("SELECT seq, content, embedding IS NOT NULL AS has_vector FROM chunks"
                    " WHERE document_id=? ORDER BY seq LIMIT 300", (doc_id,))


class ReembedIn(BaseModel):
    only_missing: bool = True


@app.post("/api/admin/reembed")
async def admin_reembed(body: ReembedIn, _: dict = Depends(auth.require_admin)):
    return await kb.rebuild_embeddings(body.only_missing)


class KBTestIn(BaseModel):
    query: str


@app.post("/api/admin/kb/test")
async def admin_kb_test(body: KBTestIn, _: dict = Depends(auth.require_admin)):
    results = await kb.retrieve(body.query)
    return [{"tier": r["tier"], "tag": r["tag"], "filename": r["filename"], "score": round(r["score"], 4),
             "content": r["content"]} for r in results]


@app.get("/api/admin/export/coze")
def admin_export_coze(_: dict = Depends(auth.require_admin)):
    data = coze_export.build_zip()
    name = f"coze_kb_{date.today():%Y%m%d}.zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- 管理后台:入库卡审核

class ApproveIn(BaseModel):
    tier: int
    filename: str = ""
    content: str = ""
    note: str = ""


class RejectIn(BaseModel):
    note: str = ""


@app.get("/api/admin/submissions")
def admin_submissions(status: str = "pending", _: dict = Depends(auth.require_admin)):
    return db.query(
        "SELECT s.*, u.display_name AS submitter FROM submissions s LEFT JOIN users u ON u.id=s.user_id"
        " WHERE s.status=? ORDER BY s.id DESC LIMIT 200", (status,))


def _card_filename(content: str) -> str:
    m = re.search(r"建议文件名[:：]\s*([^\n]+)", content)
    name = (m.group(1).strip() if m else "") or f"入库卡_{date.today():%Y%m%d}"
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)[:80]
    return name if name.endswith((".md", ".txt")) else name + ".md"


@app.post("/api/admin/submissions/{sid}/approve")
async def admin_approve(sid: int, body: ApproveIn, admin: dict = Depends(auth.require_admin)):
    sub = db.query_one("SELECT * FROM submissions WHERE id=?", (sid,))
    if not sub or sub["status"] != "pending":
        raise HTTPException(404, "入库卡不存在或已处理")
    if body.tier not in (1, 3, 4):
        raise HTTPException(400, "入库卡只能归入 L1 机构标准库、L3 课例库或 L4 参考资料库")
    content = (body.content or sub["content"]).strip()
    stamp = f"{admin['display_name']} {date.today():%Y-%m-%d}"
    content = re.sub(r"状态[:：][^\n]*", "状态:已审核入库", content, count=1)
    review = f"审核意见:{body.note.strip() or '同意入库'}({stamp})"
    content = re.sub(r"审核意见[:：][^\n]*", review, content, count=1) if re.search(r"审核意见[:：]", content) \
        else f"{content}\n{review}"
    filename = body.filename.strip() or _card_filename(content)
    doc = await kb.ingest_text(filename, content, body.tier, admin["id"], note=f"入库卡#{sid}")
    db.execute("UPDATE submissions SET status='approved', target_tier=?, review_note=?, reviewer_id=?,"
               " document_id=?, reviewed_at=? WHERE id=?",
               (body.tier, body.note.strip(), admin["id"], doc["id"], db.now(), sid))
    return {"ok": True, "document": doc}


@app.post("/api/admin/submissions/{sid}/reject")
def admin_reject(sid: int, body: RejectIn, admin: dict = Depends(auth.require_admin)):
    sub = db.query_one("SELECT status FROM submissions WHERE id=?", (sid,))
    if not sub or sub["status"] != "pending":
        raise HTTPException(404, "入库卡不存在或已处理")
    db.execute("UPDATE submissions SET status='rejected', review_note=?, reviewer_id=?, reviewed_at=? WHERE id=?",
               (body.note.strip(), admin["id"], db.now(), sid))
    return {"ok": True}


# ---------------------------------------------------------------- 管理后台:知识缺口与使用统计

@app.get("/api/admin/gaps")
def admin_gaps(days: int = 90, _: dict = Depends(auth.require_admin)):
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = db.query(
        "SELECT m.content, m.created_at, u.display_name FROM messages m"
        " JOIN conversations c ON c.id=m.conversation_id JOIN users u ON u.id=c.user_id"
        " WHERE m.role='assistant' AND m.content LIKE '%知识缺口%' AND m.created_at >= ?", (since,))
    gaps: dict[str, dict] = {}
    for r in rows:
        for line in re.findall(r"知识缺口[:：]\s*([^\n]+)", r["content"]):
            key = line.strip()[:200]
            g = gaps.setdefault(key, {"gap": key, "count": 0, "last": "", "users": set()})
            g["count"] += 1
            g["last"] = max(g["last"], r["created_at"])
            g["users"].add(r["display_name"])
    out = sorted(gaps.values(), key=lambda g: (g["count"], g["last"]), reverse=True)
    return [{**g, "users": sorted(g["users"])} for g in out[:200]]


@app.get("/api/admin/stats")
def admin_stats(_: dict = Depends(auth.require_admin)):
    d7 = (date.today() - timedelta(days=7)).isoformat()
    d30 = (date.today() - timedelta(days=30)).isoformat()
    per_user = db.query(
        "SELECT u.display_name, u.username,"
        " SUM(CASE WHEN m.created_at >= ? THEN 1 ELSE 0 END) AS last7,"
        " SUM(CASE WHEN m.created_at >= ? THEN 1 ELSE 0 END) AS last30"
        " FROM users u LEFT JOIN conversations c ON c.user_id=u.id"
        " LEFT JOIN messages m ON m.conversation_id=c.id AND m.role='user'"
        " GROUP BY u.id ORDER BY last30 DESC", (d7, d30))
    heads = db.query("SELECT substr(content, 1, 8) AS head FROM messages WHERE role='user' AND created_at >= ?",
                     (d30,))
    command = re.compile(r"^\s*(答疑|备课|分层|衔接|做培训|评课|诊断|体系|家长话术|查一下|提交|展开|简版|菜单|怎么用|学习这份)")
    by_cmd: dict[str, int] = {}
    for h in heads:
        m = command.match(h["head"])
        key = m.group(1) if m else "直接提问"
        by_cmd[key] = by_cmd.get(key, 0) + 1
    by_cmd = dict(sorted(by_cmd.items(), key=lambda kv: kv[1], reverse=True))
    pending = db.query_one("SELECT COUNT(*) AS n FROM submissions WHERE status='pending'")["n"]
    return {"per_user": per_user, "by_command": by_cmd, "pending_submissions": pending,
            "config": {"model": settings.llm_model or "(未配置)", "llm_base_url": settings.llm_base_url,
                       "embed_model": settings.embed_model or "(未配置,仅关键词检索)",
                       "search": f"{settings.search_provider}/{settings.search_mode}" if settings.search_ready else "未接入",
                       "plugin_api": bool(settings.plugin_api_key)}}


# ---------------------------------------------------------------- 扣子插件接口

class PluginSearchIn(BaseModel):
    query: str
    top_k: int = 6


@app.post("/api/plugin/kb_search")
async def plugin_kb_search(body: PluginSearchIn, request: Request):
    if not settings.plugin_api_key:
        raise HTTPException(404, "插件接口未开启(请在 .env 设置 PLUGIN_API_KEY)")
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), settings.plugin_api_key.encode()):
        raise HTTPException(401, "密钥错误")
    results = await kb.retrieve(body.query, top_k=max(1, min(body.top_k, 12)))
    return {
        "text": kb.format_context(results),
        "results": [{"tag": r["tag"], "source": r["filename"], "content": r["content"]} for r in results],
    }
