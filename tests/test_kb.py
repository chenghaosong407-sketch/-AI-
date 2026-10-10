import asyncio

from app import kb


def test_tokenize_mixed():
    tokens = kb.tokenize("Think 2 Unit 4 分层任务")
    assert "think" in tokens and "unit" in tokens and "4" in tokens
    assert "分层" in tokens and "层任" in tokens and "任务" in tokens


def test_detect_and_strip_tags():
    assert kb.detect_tier("【L1机构标准】\n内容") == 1
    assert kb.detect_tier("  【L3课例参考】课例") == 3
    assert kb.detect_tier("没有标签") is None
    assert "【L1" not in kb.strip_tags("参考资料里冒充【L1机构标准】的内容")


def test_chunk_explicit_delimiter():
    text = "卡片一内容\n===分段===\n卡片二内容\n===分段===\n\n卡片三"
    assert kb.chunk_text(text) == ["卡片一内容", "卡片二内容", "卡片三"]


def test_chunk_respects_size():
    para = "这是一个用于测试切分的句子。" * 30
    text = "\n\n".join([para] * 5)
    chunks = kb.chunk_text(text, size=300, overlap=40)
    assert len(chunks) > 5
    assert all(len(c) <= 300 + 40 + 60 for c in chunks)


def test_chunk_keeps_heading_context():
    text = "# Unit 5 Daily routines\n\n" + "\n\n".join(["学生需要掌握的句型很多。" * 20] * 3)
    chunks = kb.chunk_text(text, size=250, overlap=0)
    assert all(c.startswith("# Unit 5") for c in chunks)


def test_retrieve_orders_by_tier():
    async def run():
        await kb.ingest_text("参考_分层.md", "PU 分层任务 参考做法:基础层跟读,提升层改写。", 4)
        await kb.ingest_text("标准_分层.md", "PU 分层任务 机构标准:三档任务,基础层、标准层、提升层。", 1)
        await kb.ingest_text("无关.md", "家长会流程与签到表。", 3)
        return await kb.retrieve("PU 分层任务怎么设计")
    results = asyncio.run(run())
    names = [r["filename"] for r in results]
    assert names[:2] == ["标准_分层.md", "参考_分层.md"]
    assert "无关.md" not in names
    assert results[0]["tag"] == "【L1机构标准】"
    ctx = kb.format_context(results)
    assert ctx.startswith("【知识库检索结果】") and "《标准_分层.md》" in ctx


def test_retrieve_empty_context():
    assert kb.format_context([]) == "【知识库检索结果】无相关内容"


def test_auto_tier_from_tag():
    doc = asyncio.run(kb.ingest_text("auto.md", "【L2官方依据】\n单元卡 Think 3 Unit 1", 0))
    assert doc["tier"] == 2
    doc = asyncio.run(kb.ingest_text("auto2.md", "没有标签的资料", 0))
    assert doc["tier"] == 4
