import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app


def sse_events(resp):
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as client:
        r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200
        yield client


def test_requires_login():
    with TestClient(app) as client:
        assert client.get("/api/me").status_code == 401
        assert client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401


def test_me(admin):
    data = admin.get("/api/me").json()
    assert data["user"]["role"] == "admin"
    assert data["app"]["llm_ready"] is False


def test_create_teacher_and_permissions(admin):
    r = admin.post("/api/admin/users", json={"lines": "t1,张老师,pass123,教师\nbad,缺密码\nt2,李老师,123"})
    body = r.json()
    assert body["created"] == ["t1"]
    assert len(body["errors"]) == 2
    with TestClient(app) as teacher:
        assert teacher.post("/api/login", json={"username": "t1", "password": "pass123"}).status_code == 200
        assert teacher.get("/api/me").json()["user"]["must_change_password"] == 1
        assert teacher.get("/api/admin/users").status_code == 403
        r = teacher.post("/api/me/password", json={"old_password": "pass123", "new_password": "newpass1"})
        assert r.status_code == 200


def test_upload_and_chat_demo(admin):
    files = [("files", ("PU2_单元卡.md", "PU 第2级 Unit 3 单元卡\n===分段===\nUnit 3 语法:一般现在时".encode(), "text/markdown"))]
    r = admin.post("/api/admin/documents", data={"tier": "2", "note": "测试"}, files=files)
    res = r.json()["results"][0]
    assert res["ok"] and res["tier"] == 2 and res["chunks"] == 2

    r = admin.post("/api/chat", data={"text": "备课:PU 第2级 Unit 3 一般现在时 13912345678"})
    events = sse_events(r)
    types = [e["type"] for e in events]
    assert types[0] == "meta" and types[-1] == "done"
    assert events[0]["masked"] == 1
    assert any(e["type"] == "sources" for e in events)
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "演示模式" in text and "PU2_单元卡.md" in text

    conv_id = events[0]["conversation_id"]
    conv = admin.get(f"/api/conversations/{conv_id}").json()
    assert "13912345678" not in conv["messages"][0]["content"]
    assert conv["messages"][1]["role"] == "assistant"


def test_chat_with_attachment(admin):
    files = [("files", ("听课记录.txt", "教师用 5 分钟导入,学生两两对话。".encode(), "text/plain"))]
    r = admin.post("/api/chat", data={"text": "评课"}, files=files)
    assert r.status_code == 200
    events = sse_events(r)
    conv = admin.get(f"/api/conversations/{events[0]['conversation_id']}").json()
    assert conv["messages"][0]["attachments"] == ["听课记录.txt"]


def test_reject_unsupported_attachment(admin):
    files = [("files", ("old.doc", b"xxx", "application/msword"))]
    r = admin.post("/api/chat", data={"text": "学习这份"}, files=files)
    assert r.status_code == 400 and ".docx" in r.json()["detail"]


def test_submission_flow(admin):
    card = ("建议文件名:课例库_PU_L2_分层游戏_20261010\n归入:课例库\n状态:待审核\n提交人:\n"
            "适用范围:PU 第2级 混合班\n核心内容:\n1. 词汇接龙分三档\n效果证据:单元测词汇题正确率提升\n审核意见:")
    r = admin.post("/api/submissions", json={"content": card})
    sid = r.json()["id"]
    pending = admin.get("/api/admin/submissions?status=pending").json()
    assert any(s["id"] == sid and "提交人:教研负责人" in s["content"] for s in pending)
    r = admin.post(f"/api/admin/submissions/{sid}/approve", json={"tier": 3, "note": "同意"})
    doc = r.json()["document"]
    assert doc["tier"] == 3 and doc["filename"] == "课例库_PU_L2_分层游戏_20261010.md"
    chunks = admin.get(f"/api/admin/documents/{doc['id']}/chunks").json()
    assert "状态:已审核入库" in chunks[0]["content"] and "审核意见:同意" in chunks[0]["content"]
    assert admin.post(f"/api/admin/submissions/{sid}/approve", json={"tier": 3}).status_code == 404


def test_gaps_and_stats(admin):
    from app import db
    conv = admin.get("/api/conversations").json()[0]["id"]
    db.execute("INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?, 'assistant', ?, '{}', ?)",
               (conv, "回答……\n知识缺口:Think 2 级单元卡,建议补充单元卡", db.now()))
    gaps = admin.get("/api/admin/gaps").json()
    assert gaps[0]["gap"].startswith("Think 2 级单元卡")
    stats = admin.get("/api/admin/stats").json()
    assert stats["by_command"].get("备课", 0) >= 1


def test_tier_change_and_delete(admin):
    docs = admin.get("/api/admin/documents").json()
    doc_id = docs[0]["id"]
    assert admin.patch(f"/api/admin/documents/{doc_id}", json={"tier": 4}).status_code == 200
    assert admin.get(f"/api/admin/documents?tier=4").json()[0]["id"] == doc_id
    assert admin.delete(f"/api/admin/documents/{doc_id}").status_code == 200
    assert admin.delete(f"/api/admin/documents/{doc_id}").status_code == 404


def test_coze_export(admin):
    r = admin.get("/api/admin/export/coze")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "导入说明.txt" in names
    l2 = [n for n in names if n.startswith("L2官方依据")]
    assert l2
    body = zf.read(l2[0]).decode()
    assert body.startswith("【L2官方依据】") and "===分段===" in body


def test_plugin_endpoint():
    with TestClient(app) as client:
        assert client.post("/api/plugin/kb_search", json={"query": "PU"}).status_code == 401
        r = client.post("/api/plugin/kb_search", json={"query": "PU 第2级 Unit 3 一般现在时"},
                        headers={"Authorization": "Bearer test-plugin-key"})
        assert r.status_code == 200
        assert r.json()["text"].startswith("【知识库检索结果】")
        r = client.post("/api/plugin/kb_search", json={"query": "PU"}, headers={"Authorization": "test-plugin-key"})
        assert r.status_code == 200  # 扣子里只填密钥、不带 Bearer 也可以


def test_feedback_flow(admin):
    r = admin.post("/api/chat", data={"text": "答疑:测试反馈"})
    done = sse_events(r)[-1]
    mid = done["message_id"]
    assert admin.post(f"/api/messages/{mid}/feedback", json={"rating": -1, "comment": "页码是编的"}).status_code == 200
    rows = admin.get("/api/admin/feedback?rating=-1").json()
    assert rows[0]["comment"] == "页码是编的" and rows[0]["question"] == "答疑:测试反馈"
    conv = admin.get(f"/api/conversations/{sse_events(r)[0]['conversation_id']}").json()
    assert conv["messages"][1]["rating"] == -1
    assert admin.post(f"/api/messages/{mid}/feedback", json={"rating": 1}).status_code == 200
    assert admin.get("/api/admin/feedback?rating=-1").json() == [] or \
        all(x["message_id"] != mid for x in admin.get("/api/admin/feedback?rating=-1").json())
    assert admin.get("/api/admin/stats").json()["feedback"]["up"] >= 1
    assert admin.post(f"/api/messages/{mid}/feedback", json={"rating": 5}).status_code == 400
    assert admin.post("/api/messages/999999/feedback", json={"rating": 1}).status_code == 404


def test_docx_export(admin):
    import docx
    content = "课题|PU 第2级 Unit 3|40分钟|混合班\n一、语言目标\n1. **掌握**一般现在时\n- 基础层:跟读\n> 注意事项"
    r = admin.post("/api/export/docx", json={"content": content, "title": "PU2 Unit3 教案"})
    assert r.status_code == 200
    assert "filename*=UTF-8''PU2%20Unit3%20" in r.headers["content-disposition"]
    d = docx.Document(io.BytesIO(r.content))
    text = "\n".join(p.text for p in d.paragraphs)
    assert "PU2 Unit3 教案" in text and "掌握一般现在时" in text and "**" not in text
    assert admin.post("/api/export/docx", json={"content": "  "}).status_code == 400
