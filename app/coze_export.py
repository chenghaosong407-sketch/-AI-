"""把本地知识库导出成扣子(Coze)可直接上传的文本文件。

每个层级一个文件夹;每个文件内的片段用"===分段==="隔开,且每段开头带层级标签,
在扣子上传时选择"自定义分段",分段标识符填 ===分段=== 即可保持与本地一致的切分和层级。
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import defaultdict

from . import db
from .kb import SEGMENT_DELIMITER, TIERS, tier_tag

# 扣子文本知识库单个文件建议不要过大,这里按字符数拆分文件
MAX_FILE_CHARS = 2_000_000

README = """扣子知识库导入说明(由程序生成)

1. 在扣子"资源库"新建 4 个文本知识库,名称建议:{names}。
   也可以只建 1 个,全部上传;层级靠每段开头的【L1…】【L2…】标签区分。
2. 把对应文件夹里的 .txt 上传到对应知识库。
3. 分段设置选"自定义":分段标识符选"自定义"并填入 {delim} ;分段最大长度设为 1000 左右;
   预处理规则只勾"替换连续空格、换行符和制表符",不要勾"删除URL"。
4. 在智能体里添加这些知识库,调用方式选"自动调用",召回数量建议 5-8。
5. 每个文本知识库有分段数和文件数上限,超过时按层级拆成多个知识库(以扣子当前帮助文档为准)。
"""


def _safe(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_")[:60] or "未命名"


def build_zip() -> bytes:
    rows = db.query(
        "SELECT d.id AS doc_id, d.filename, d.tier, c.seq, c.content FROM chunks c"
        " JOIN documents d ON d.id = c.document_id ORDER BY d.tier, d.id, c.seq"
    )
    per_tier: dict[int, list[str]] = defaultdict(list)
    for r in rows:
        per_tier[r["tier"]].append(f"{tier_tag(r['tier'])}《{r['filename']}》\n{r['content'].strip()}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        names = "、".join(f"{TIERS[t][0]}_{TIERS[t][1]}" for t in TIERS)
        zf.writestr("导入说明.txt", README.format(names=names, delim=SEGMENT_DELIMITER))
        for tier, segments in per_tier.items():
            folder = f"{TIERS[tier][0]}_{TIERS[tier][1]}"
            part, size, files = [], 0, 1
            for seg in segments:
                if size + len(seg) > MAX_FILE_CHARS and part:
                    zf.writestr(f"{folder}/{_safe(folder)}_{files:02d}.txt", f"\n{SEGMENT_DELIMITER}\n".join(part))
                    part, size, files = [], 0, files + 1
                part.append(seg)
                size += len(seg)
            if part:
                zf.writestr(f"{folder}/{_safe(folder)}_{files:02d}.txt", f"\n{SEGMENT_DELIMITER}\n".join(part))
    return buf.getvalue()
