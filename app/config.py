"""读取 .env 与环境变量。所有可调参数集中在这里。"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析:KEY=VALUE,支持 # 注释和引号;已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _str(key: str, default: str = "") -> str:
    """未设置或留空时返回默认值。"""
    return os.environ.get(key, "").strip() or default


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key, str(default)))
    except ValueError:
        return default


def _json(key: str) -> dict:
    raw = _str(key)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


class Settings:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        # 服务
        self.host = _str("APP_HOST", "0.0.0.0")
        self.port = _int("APP_PORT", 8000)
        self.data_dir = Path(_str("DATA_DIR", str(BASE_DIR / "data"))).resolve()
        self.app_title = _str("APP_TITLE", "优学东方·原版教研助手")
        self.admin_username = _str("ADMIN_USERNAME", "admin")
        self.admin_password = _str("ADMIN_PASSWORD", "")
        self.session_days = _int("SESSION_DAYS", 30)
        self.max_upload_mb = _int("MAX_UPLOAD_MB", 50)
        self.daily_message_limit = _int("DAILY_MESSAGE_LIMIT", 0)

        # 大模型(任何 OpenAI 兼容接口:豆包/火山方舟、DeepSeek、通义千问、Kimi 等)
        self.llm_base_url = _str("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
        self.llm_api_key = _str("LLM_API_KEY")
        self.llm_model = _str("LLM_MODEL")
        self.llm_temperature = _float("LLM_TEMPERATURE", 0.4)
        self.llm_max_tokens = _int("LLM_MAX_TOKENS", 8192)
        self.llm_timeout = _int("LLM_TIMEOUT", 180)
        self.llm_extra_body = _json("LLM_EXTRA_BODY")
        self.llm_supports_tools = _str("LLM_SUPPORTS_TOOLS", "true").lower() != "false"

        # 向量模型(可选;不配置时只用关键词检索)
        self.embed_base_url = _str("EMBED_BASE_URL", self.llm_base_url).rstrip("/")
        self.embed_api_key = _str("EMBED_API_KEY", self.llm_api_key)
        self.embed_model = _str("EMBED_MODEL")
        self.embed_batch = max(1, _int("EMBED_BATCH", 10))
        # auto:模型名含 embedding-vision 时走豆包多模态向量接口,否则走通用 /embeddings
        self.embed_api_style = _str("EMBED_API_STYLE", "auto").lower()
        self.embed_dimensions = _int("EMBED_DIMENSIONS", 0)  # 0 = 用模型默认(豆包多模态默认取 1024)
        self.embed_concurrency = _int("EMBED_CONCURRENCY", 5)

        # 联网搜索
        self.search_provider = _str("SEARCH_PROVIDER", "none").lower()
        self.search_api_key = _str("SEARCH_API_KEY")
        self.search_endpoint = _str("SEARCH_ENDPOINT", "https://api.bocha.cn/v1/web-search")
        self.search_mode = _str("SEARCH_MODE", "tool").lower()  # tool=模型自己决定;auto=关键词触发
        self.search_count = _int("SEARCH_COUNT", 8)

        # 知识库检索
        self.kb_top_k = _int("KB_TOP_K", 8)
        self.kb_chunk_size = _int("KB_CHUNK_SIZE", 600)
        self.kb_chunk_overlap = _int("KB_CHUNK_OVERLAP", 80)
        self.kb_min_vector_score = _float("KB_MIN_VECTOR_SCORE", 0.3)
        self.kb_max_context_chars = _int("KB_MAX_CONTEXT_CHARS", 6000)

        # 对话
        self.history_messages = _int("HISTORY_MESSAGES", 12)
        self.attachment_max_chars = _int("ATTACHMENT_MAX_CHARS", 30000)

        # 扣子插件接口(留空则关闭)
        self.plugin_api_key = _str("PLUGIN_API_KEY")

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def embed_ready(self) -> bool:
        return bool(self.embed_api_key and self.embed_model)

    @property
    def search_ready(self) -> bool:
        return self.search_provider == "bocha" and bool(self.search_api_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


settings = Settings()
