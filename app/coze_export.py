"""把本地知识库导出成扣子(Coze)可直接上传的文本文件。

每段开头带层级标签,段与段之间用"===分段==="隔开;在扣子上传时选择"自定义"分段,
分段标识符填 ===分段=== 即可保持与本地一致的切分和层级。

按扣子官方《知识库使用限制》处理(2026-10 核对):
- txt 文件最大 5 MB → 每个文件控制在 4 MB 以内
- 每个文本知识库最多 300 个文件、10000 个分段 → 超出时自动拆成多个知识库文件夹
- 单个分段最长 5000 字符 → 本地分段远小于此
- 知识库含邮箱、电话等个人信息时部分渠道受限 → 导出时隐去
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import defaultdict

from . import db
from .kb import SEGMENT_DELIMITER, TIERS, _split_long, tier_tag
from .privacy import mask_pii

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SEGMENTS_PER_KB = 9500
MAX_FILES_PER_KB = 300
MAX_SEGMENT_CHARS = 1800  # 导入说明里建议扣子分段最大长度填 2000,留出标签行的余量

README = """扣子知识库导入说明(由程序生成)

本压缩包共 {kb_count} 个文件夹,每个文件夹对应扣子里的 1 个文本知识库:
{folders}

1. 在扣子"资源库 → +资源 → 知识库"中按文件夹新建文本知识库,名称与文件夹名一致。
   需要多人共用时,请在团队/企业工作空间中创建(个人空间的资源只有本人能用)。
2. 把文件夹里的 .txt 上传到对应知识库(本地文档)。
3. 分段设置选"自定义":
   - 分段标识符:选"自定义",填入 {delim}
   - 分段最大长度:填 2000(本地分段都短于这个长度,不会被再次切开)
   - 分段重叠度:0
   - 预处理规则:只勾"替换连续的空格、换行符和制表符",不要勾"删除所有 URL 和电子邮箱地址"
4. 在智能体里添加这些知识库,调用方式选"自动调用",召回数量建议 5-8。
5. 每段开头的【L1机构标准】【L2官方依据】等标签就是层级依据,不要删除。
6. 导出时已把手机号、身份证号、邮箱替换为"[已隐去]"(共 {masked} 处)。
"""


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")[:60] or "未命名"


def build_zip() -> bytes:
    rows = db.query(
        "SELECT d.id AS doc_id, d.filename, d.tier, c.seq, c.content FROM chunks c"
        " JOIN documents d ON d.id = c.document_id ORDER BY d.tier, d.id, c.seq"
    )
    masked_total = 0
    per_tier: dict[int, list[str]] = defaultdict(list)
    for r in rows:
        content, n = mask_pii(r["content"].strip(), emails=True)
        masked_total += n
        pieces = [content] if len(content) <= MAX_SEGMENT_CHARS else _split_long(content, MAX_SEGMENT_CHARS)
        for piece in pieces:
            per_tier[r["tier"]].append(f"{tier_tag(r['tier'])}《{r['filename']}》\n{piece}")

    sep = f"\n{SEGMENT_DELIMITER}\n"
    folders: list[tuple[str, int, int]] = []  # (文件夹名, 文件数, 分段数)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for tier in sorted(per_tier):
            base = f"{TIERS[tier][0]}_{TIERS[tier][1]}"
            kb_no, file_no, kb_segments = 1, 0, 0
            part: list[str] = []
            size = 0

            def folder_name() -> str:
                return base if kb_no == 1 else f"{base}_{kb_no}"

            def flush() -> None:
                nonlocal part, size, file_no
                if not part:
                    return
                file_no += 1
                zf.writestr(f"{folder_name()}/{_safe(folder_name())}_{file_no:03d}.txt", sep.join(part))
                part, size = [], 0

            for seg in per_tier[tier]:
                seg_bytes = len(seg.encode("utf-8")) + len(sep.encode("utf-8"))
                need_new_kb = kb_segments >= MAX_SEGMENTS_PER_KB
                if size + seg_bytes > MAX_FILE_BYTES and part:
                    flush()
                if not part and file_no >= MAX_FILES_PER_KB:
                    need_new_kb = True
                if need_new_kb:
                    flush()
                    folders.append((folder_name(), file_no, kb_segments))
                    kb_no, file_no, kb_segments = kb_no + 1, 0, 0
                part.append(seg)
                size += seg_bytes
                kb_segments += 1
            flush()
            folders.append((folder_name(), file_no, kb_segments))

        listing = "\n".join(f"   - {name}:{files} 个文件,{segs} 个分段" for name, files, segs in folders) or "   (知识库为空)"
        zf.writestr("导入说明.txt", README.format(kb_count=len(folders), folders=listing,
                                                 delim=SEGMENT_DELIMITER, masked=masked_total))
    return buf.getvalue()
