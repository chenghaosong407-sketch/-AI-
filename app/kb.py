"""四级知识库:入库(切分 + 向量化)与检索(关键词 BM25 + 向量,按层级加权)。"""
from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

from . import db, llm
from .config import settings

log = logging.getLogger("kb")

# 层级:编号 → (标签, 库名, 引用标注)
TIERS: dict[int, tuple[str, str, str]] = {
    1: ("L1机构标准", "机构标准库", "机构标准"),
    2: ("L2官方依据", "教材官方库", "官方依据"),
    3: ("L3课例参考", "课例库", "课例参考"),
    4: ("L4参考资料", "参考资料库", "参考资料,未审核"),
}
TIER_WEIGHT = {1: 1.2, 2: 1.1, 3: 1.05, 4: 1.0}
SEGMENT_DELIMITER = "===分段==="
_TAG_RE = re.compile(r"【\s*L\s*([1-4])\s*[^】]{0,8}】")


def tier_tag(tier: int) -> str:
    return f"【{TIERS[tier][0]}】"


def detect_tier(text: str) -> int | None:
    """读取文件开头的层级标签(如【L1机构标准】),没有则返回 None。"""
    match = _TAG_RE.match(text.lstrip())
    return int(match.group(1)) if match else None


def strip_tags(text: str) -> str:
    """去掉正文里的层级标签,防止低层级资料冒充高层级。层级只由入库时的选择决定。"""
    return _TAG_RE.sub("", text)


# ---------------------------------------------------------------- 切分

_SENT_SPLIT = re.compile(r"(?<=[。!?!?;;])|(?<=[.?!])\s+|\n")
_HEADING = re.compile(r"^(#{1,6}\s+.+|Unit\s+\d+.*|第[一二三四五六七八九十百\d]+[单元章节课].*)$", re.I)


def _split_long(block: str, size: int) -> list[str]:
    pieces, cur = [], ""
    for sent in _SENT_SPLIT.split(block):
        if not sent or not sent.strip():
            continue
        while len(sent) > size:  # 超长且没有标点的句子硬切
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(sent[:size])
            sent = sent[size:]
        if len(cur) + len(sent) + 1 > size and cur:
            pieces.append(cur)
            cur = ""
        # 英文句子之间补空格,中文直接相连
        sep = " " if cur and cur[-1:].isascii() and sent[:1].isascii() else ""
        cur = cur + sep + sent
    if cur.strip():
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    size = size or settings.kb_chunk_size
    overlap = settings.kb_chunk_overlap if overlap is None else overlap
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    # 显式分段:文件中有"===分段==="行时严格按它切(适合单元卡、FAQ)
    if any(line.strip() == SEGMENT_DELIMITER for line in text.splitlines()):
        segments = re.split(rf"^\s*{re.escape(SEGMENT_DELIMITER)}\s*$", text, flags=re.M)
        out: list[str] = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            out.extend([seg] if len(seg) <= size * 2 else _split_long(seg, size))
        return out

    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    chunks: list[str] = []
    cur = ""
    heading = ""
    for block in blocks:
        first_line = block.splitlines()[0].strip()
        is_heading = bool(_HEADING.match(first_line)) and len(first_line) <= 80
        if is_heading:
            heading = first_line
            if cur:
                chunks.append(cur)
                cur = ""
        parts = _split_long(block, size) if len(block) > size else [block]
        for part in parts:
            if cur and len(cur) + len(part) + 2 > size:
                chunks.append(cur)
                tail = cur[-overlap:] if overlap else ""
                cur = ""
                if heading and not part.startswith(heading):
                    cur = f"{heading}\n"
                if tail:
                    cur += f"……{tail}\n"
            cur = f"{cur}\n\n{part}" if cur and not cur.endswith("\n") else cur + part
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


# ---------------------------------------------------------------- 分词(无第三方依赖)

_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[一-鿿]+")


def tokenize(text: str) -> list[str]:
    """英文按单词,中文按相邻两字(bigram);单个汉字的片段保留单字。"""
    text = text.lower()
    tokens = _WORD.findall(text)
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


# ---------------------------------------------------------------- 索引


@dataclass
class _Index:
    ids: list[int] = field(default_factory=list)
    meta: list[dict] = field(default_factory=list)
    postings: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    doc_len: list[int] = field(default_factory=list)
    avg_len: float = 1.0
    vec_rows: list[int] = field(default_factory=list)  # 有向量的 chunk 在 ids 中的位置
    vectors: np.ndarray | None = None
    signature: tuple = ()


_index = _Index()
_index_lock = threading.Lock()


def _signature() -> tuple:
    """知识库内容指纹:其他进程(如批量导入脚本)改动后,下次检索自动重载。"""
    row = db.query_one(
        "SELECT (SELECT COUNT(*) FROM chunks) AS n, (SELECT COALESCE(MAX(id),0) FROM chunks) AS m,"
        " (SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL) AS v,"
        " (SELECT COALESCE(SUM(tier * id),0) FROM documents) AS t")
    return (row["n"], row["m"], row["v"], row["t"])


def reload_index() -> None:
    signature = _signature()
    rows = db.query(
        "SELECT c.id, c.document_id, c.content, c.embedding, d.tier, d.filename"
        " FROM chunks c JOIN documents d ON d.id = c.document_id ORDER BY c.id"
    )
    idx = _Index(signature=signature)
    postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
    vecs: list[np.ndarray] = []
    dim = None
    for pos, row in enumerate(rows):
        idx.ids.append(row["id"])
        idx.meta.append({"chunk_id": row["id"], "document_id": row["document_id"],
                         "tier": row["tier"], "filename": row["filename"], "content": row["content"]})
        counts = Counter(tokenize(row["filename"] + " " + row["content"]))
        idx.doc_len.append(sum(counts.values()))
        for term, tf in counts.items():
            postings[term].append((pos, tf))
        if row["embedding"]:
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            if dim is None:
                dim = vec.shape[0]
            if vec.shape[0] == dim:
                vecs.append(vec)
                idx.vec_rows.append(pos)
    idx.postings = dict(postings)
    idx.avg_len = (sum(idx.doc_len) / len(idx.doc_len)) if idx.doc_len else 1.0
    if vecs:
        mat = np.vstack(vecs)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        idx.vectors = mat / np.maximum(norms, 1e-8)
    global _index
    with _index_lock:
        _index = idx
    log.info("知识库索引已加载:%d 个片段,%d 个有向量", len(idx.ids), len(idx.vec_rows))


def _bm25(idx: _Index, query: str, limit: int) -> list[tuple[int, float]]:
    terms = list(dict.fromkeys(tokenize(query)))
    if not terms or not idx.ids:
        return []
    meaningful = [t for t in terms if len(t) > 1 or t.isascii()]
    n = len(idx.ids)
    k1, b = 1.5, 0.75
    scores: dict[int, float] = defaultdict(float)
    hits: dict[int, int] = defaultdict(int)
    for term in terms:
        plist = idx.postings.get(term)
        if not plist:
            continue
        idf = math.log(1 + (n - len(plist) + 0.5) / (len(plist) + 0.5))
        for pos, tf in plist:
            denom = tf + k1 * (1 - b + b * idx.doc_len[pos] / idx.avg_len)
            scores[pos] += idf * tf * (k1 + 1) / denom
            if term in meaningful:
                hits[pos] += 1
    need = min(2, len(meaningful)) or 1
    ranked = [(pos, s) for pos, s in scores.items() if hits[pos] >= need]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:limit]


async def _vector(idx: _Index, query: str, limit: int) -> list[tuple[int, float]]:
    if idx.vectors is None or not settings.embed_ready:
        return []
    try:
        qvec = np.asarray((await llm.embed([query]))[0], dtype=np.float32)
    except Exception as exc:  # 向量服务异常时退回关键词检索
        log.warning("查询向量化失败,仅用关键词检索:%s", exc)
        return []
    if qvec.shape[0] != idx.vectors.shape[1]:
        log.warning("向量维度不一致(可能更换了向量模型),请在管理后台点击'重建向量'")
        return []
    qvec = qvec / max(float(np.linalg.norm(qvec)), 1e-8)
    sims = idx.vectors @ qvec
    top = np.argsort(-sims)[:limit]
    return [(idx.vec_rows[i], float(sims[i])) for i in top if sims[i] >= settings.kb_min_vector_score]


async def retrieve(query: str, top_k: int | None = None, tiers: list[int] | None = None) -> list[dict]:
    """混合检索。结果按层级(L1→L4)再按相关度排序。"""
    top_k = top_k or settings.kb_top_k
    if _signature() != _index.signature:
        reload_index()
    with _index_lock:
        idx = _index
    if not idx.ids or not query.strip():
        return []
    pool = max(top_k * 4, 20)
    fused: dict[int, float] = defaultdict(float)
    for rank, (pos, _) in enumerate(_bm25(idx, query, pool)):
        fused[pos] += 1 / (60 + rank)
    for rank, (pos, _) in enumerate(await _vector(idx, query, pool)):
        fused[pos] += 1 / (60 + rank)
    scored = []
    for pos, score in fused.items():
        meta = idx.meta[pos]
        if tiers and meta["tier"] not in tiers:
            continue
        scored.append({**meta, "score": score * TIER_WEIGHT.get(meta["tier"], 1.0)})
    scored.sort(key=lambda r: r["score"], reverse=True)

    picked, used = [], 0
    for item in scored:
        if len(picked) >= top_k:
            break
        if used + len(item["content"]) > settings.kb_max_context_chars and picked:
            continue
        used += len(item["content"])
        picked.append(item)
    picked.sort(key=lambda r: (r["tier"], -r["score"]))
    for item in picked:
        item["tag"] = tier_tag(item["tier"])
    return picked


def format_context(results: list[dict]) -> str:
    if not results:
        return "【知识库检索结果】无相关内容"
    lines = [f"【知识库检索结果】共{len(results)}条,已按可信度从高到低排序"]
    for i, r in enumerate(results, start=1):
        lines.append(f"\n[{i}]{r['tag']}《{r['filename']}》\n{r['content']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 入库


async def _embed_chunks(chunks: list[str], filename: str, tier: int) -> tuple[list[bytes | None], str]:
    if not settings.embed_ready:
        return [None] * len(chunks), ""
    try:
        vectors = await llm.embed([f"{tier_tag(tier)}《{filename}》\n{c}" for c in chunks])
        return [np.asarray(v, dtype=np.float32).tobytes() for v in vectors], ""
    except Exception as exc:
        log.warning("向量化失败:%s", exc)
        return [None] * len(chunks), f"向量化失败,已仅按关键词入库,可稍后在后台'重建向量':{exc}"


async def ingest_text(filename: str, text: str, tier: int, uploaded_by: int | None = None,
                      file_path: str | None = None, note: str = "", reload: bool = True) -> dict:
    """tier=0 表示按文件开头标签自动判断,无标签按 L4 处理。"""
    if tier == 0:
        tier = detect_tier(text) or 4
    if tier not in TIERS:
        raise ValueError("层级必须是 1-4")
    clean = strip_tags(text)
    chunks = chunk_text(clean)
    if not chunks:
        raise ValueError("文件内容为空")
    embeddings, warning = await _embed_chunks(chunks, filename, tier)
    with db.tx() as conn:
        doc_id = conn.execute(
            "INSERT INTO documents (filename, tier, chars, chunk_count, file_path, note, uploaded_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, tier, len(clean), len(chunks), file_path, note, uploaded_by, db.now()),
        ).lastrowid
        conn.executemany(
            "INSERT INTO chunks (document_id, seq, content, embedding) VALUES (?, ?, ?, ?)",
            [(doc_id, i, c, e) for i, (c, e) in enumerate(zip(chunks, embeddings))],
        )
    if reload:
        reload_index()
    return {"id": doc_id, "filename": filename, "tier": tier, "chunks": len(chunks), "warning": warning}


def delete_document(doc_id: int) -> dict | None:
    doc = db.query_one("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not doc:
        return None
    db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    reload_index()
    return doc


def set_tier(doc_id: int, tier: int) -> None:
    if tier not in TIERS:
        raise ValueError("层级必须是 1-4")
    db.execute("UPDATE documents SET tier=? WHERE id=?", (tier, doc_id))
    reload_index()


async def rebuild_embeddings(only_missing: bool = True) -> dict:
    if not settings.embed_ready:
        raise ValueError("未配置向量模型(EMBED_MODEL)")
    where = "WHERE c.embedding IS NULL" if only_missing else ""
    rows = db.query(
        "SELECT c.id, c.content, d.filename, d.tier FROM chunks c"
        f" JOIN documents d ON d.id = c.document_id {where} ORDER BY c.id"
    )
    done = 0
    step = settings.embed_batch * 5
    for start in range(0, len(rows), step):
        batch = rows[start:start + step]
        vectors = await llm.embed([f"{tier_tag(r['tier'])}《{r['filename']}》\n{r['content']}" for r in batch])
        with db.tx() as conn:
            conn.executemany("UPDATE chunks SET embedding=? WHERE id=?",
                             [(np.asarray(v, dtype=np.float32).tobytes(), r["id"]) for v, r in zip(vectors, batch)])
        done += len(batch)
    reload_index()
    return {"updated": done}


def stats() -> dict:
    by_tier = {r["tier"]: r for r in db.query(
        "SELECT tier, COUNT(*) AS docs, COALESCE(SUM(chunk_count),0) AS chunks FROM documents GROUP BY tier")}
    missing = db.query_one("SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NULL")["n"]
    return {
        "tiers": [{"tier": t, "label": TIERS[t][0], "name": TIERS[t][1],
                   "docs": by_tier.get(t, {}).get("docs", 0), "chunks": by_tier.get(t, {}).get("chunks", 0)}
                  for t in TIERS],
        "missing_embeddings": missing,
        "embed_ready": settings.embed_ready,
    }
