import os
import sys
import tempfile
from pathlib import Path

# 必须在导入 app 之前设置:测试用临时数据目录、演示模式
_TMP = tempfile.mkdtemp(prefix="yx-test-")
os.environ.update({
    "DATA_DIR": _TMP,
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "admin123",
    "LLM_API_KEY": "",
    "LLM_MODEL": "",
    "EMBED_MODEL": "",
    "SEARCH_PROVIDER": "none",
    "PLUGIN_API_KEY": "test-plugin-key",
})
for key in ("EMBED_API_KEY", "SEARCH_API_KEY", "LLM_EXTRA_BODY"):
    os.environ.pop(key, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

settings.reload()
