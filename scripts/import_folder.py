"""批量导入资料到知识库。

用法:
  python scripts/import_folder.py 资料目录            # 子文件夹名为 L1/L2/L3/L4 时按文件夹定层级,
                                                     # 其余文件按开头标签判断,无标签归 L4
  python scripts/import_folder.py 资料目录 --tier 3   # 全部按指定层级导入
  加 --skip-existing 跳过同名同层级的已入库文件
"""
import argparse
import asyncio
import re
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, kb, parsers  # noqa: E402
from app.config import settings  # noqa: E402


def folder_tier(path: Path, root: Path) -> int:
    for part in path.relative_to(root).parts[:-1]:
        m = re.match(r"^L([1-4])", part, re.I)
        if m:
            return int(m.group(1))
    return 0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--tier", type=int, choices=[1, 2, 3, 4])
    ap.add_argument("--note", default="批量导入")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    root = Path(args.folder).resolve()
    all_files = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith((".", "~$"))]
    files = sorted(p for p in all_files if p.suffix.lower() in parsers.SUPPORTED)
    for p in sorted(set(all_files) - set(files)):
        print(f"- 跳过不支持的格式:{p.relative_to(root)}(老版 .doc/.ppt/.xls 请另存为新格式)")
    if not files:
        print("没有找到可导入的文件")
        return
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    existing = {(r["filename"], r["tier"]) for r in db.query("SELECT filename, tier FROM documents")}
    ok = fail = skipped = 0
    for path in files:
        tier = args.tier or folder_tier(path, root)
        try:
            text = parsers.extract_text(path.name, path.read_bytes())
            real_tier = tier or kb.detect_tier(text) or 4
            if args.skip_existing and (path.name, real_tier) in existing:
                skipped += 1
                continue
            stored = settings.upload_dir / f"{uuid.uuid4().hex}{path.suffix.lower()}"
            shutil.copyfile(path, stored)
            doc = await kb.ingest_text(path.name, text, real_tier, None, str(stored), args.note, reload=False)
            ok += 1
            warn = f"  ⚠ {doc['warning']}" if doc["warning"] else ""
            print(f"✔ [{kb.TIERS[real_tier][0]}] {path.relative_to(root)}  {doc['chunks']}段{warn}")
        except Exception as exc:
            fail += 1
            print(f"✘ {path.relative_to(root)}:{exc}")
    print(f"\n完成:成功 {ok},失败 {fail},跳过 {skipped}。正在运行的程序会在下次检索时自动加载新资料。")


if __name__ == "__main__":
    asyncio.run(main())
