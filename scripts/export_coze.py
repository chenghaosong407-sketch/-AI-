"""把知识库导出为扣子可上传的 zip:python scripts/export_coze.py [输出文件名]"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import coze_export  # noqa: E402

if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else f"coze_kb_{date.today():%Y%m%d}.zip")
    out.write_bytes(coze_export.build_zip())
    print(f"已导出:{out.resolve()}(内含导入说明.txt)")
