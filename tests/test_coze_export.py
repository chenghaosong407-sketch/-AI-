import asyncio
import io
import zipfile

from app import coze_export, db, kb


def _zip():
    return zipfile.ZipFile(io.BytesIO(coze_export.build_zip()))


def test_export_splits_by_limits(monkeypatch):
    asyncio.run(kb.ingest_text("拆分测试.md", "\n===分段===\n".join(f"第{i}段 联系 13912345678 a@b.com" for i in range(25)), 4))
    monkeypatch.setattr(coze_export, "MAX_SEGMENTS_PER_KB", 10)
    monkeypatch.setattr(coze_export, "MAX_FILES_PER_KB", 2)
    monkeypatch.setattr(coze_export, "MAX_FILE_BYTES", 400)
    zf = _zip()
    l4 = [n for n in zf.namelist() if n.startswith("L4参考资料")]
    folders = {n.split("/")[0] for n in l4}
    assert len(folders) >= 3  # 25 段、每库最多 10 段 → 至少 3 个库
    for folder in folders:
        files = [n for n in l4 if n.startswith(folder + "/")]
        assert 1 <= len(files) <= 2
        segs = sum(zf.read(f).decode().count("【L4参考资料】") for f in files)
        assert segs <= 10
    body = "".join(zf.read(n).decode() for n in l4)
    assert "13912345678" not in body and "a@b.com" not in body
    assert body.count("第") >= 25
    readme = zf.read("导入说明.txt").decode()
    assert "2000" in readme and "已隐去" in readme
    db.execute("DELETE FROM documents WHERE filename='拆分测试.md'")
    kb.reload_index()


def test_export_file_size_limit():
    zf = _zip()
    for info in zf.infolist():
        assert info.file_size <= coze_export.MAX_FILE_BYTES + 10_000


def test_export_splits_overlong_segment():
    long_text = "这是一个很长的段落。" * 400  # 4000 字,模拟调大 KB_CHUNK_SIZE 后的超长分段
    db_doc = asyncio.run(kb.ingest_text("超长.md", long_text, 3))
    db.execute("UPDATE chunks SET content=? WHERE document_id=?", (long_text, db_doc["id"]))
    zf = _zip()
    body = "".join(zf.read(n).decode() for n in zf.namelist() if n.startswith("L3课例参考"))
    for seg in body.split("\n===分段===\n"):
        assert len(seg) <= coze_export.MAX_SEGMENT_CHARS + 60
    db.execute("DELETE FROM documents WHERE id=?", (db_doc["id"],))
    kb.reload_index()
