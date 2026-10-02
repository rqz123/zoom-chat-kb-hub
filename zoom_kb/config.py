from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DB_PATH = DATA_DIR / "zoom_kb.db"
TOKEN_PATH = DATA_DIR / "zoom-token.bin"
LEGACY_TOKEN_PATH = PROJECT_DIR.parent / "work" / ".zoom_tokens.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"

ZOOM_CLIENT_ID = os.getenv("ZOOM_PUBLIC_CLIENT_ID", "npewtSYfRO6UyvSp2pqmcw")
ZOOM_API_BASE = "https://api.zoom.us/v2"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
DEFAULT_SYNC_DAYS = int(os.getenv("ZOOM_KB_INITIAL_SYNC_DAYS", "90"))
ALLOWED_INITIAL_SYNC_DAYS = (30, 90, 180)

OPENAI_CONFIG_FILE = os.getenv("OPENAI_CONFIG_FILE", "")
OPENAI_FALLBACK_CONFIG = Path(r"C:\Works\access-redmine\openai_config.json")
AI_PROMPT_VERSION = "topic-extraction-v3-source-language"
KNOWLEDGE_PROMPT_VERSION = "knowledge-archive-v1"
KNOWLEDGE_DECISION_PROMPT_VERSION = "knowledge-decision-v1"
EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "512"))
AI_MODEL_TIERS = {
    "low": "gpt-5-mini",
    "mid": "gpt-5.6-luna",
    "high": "gpt-5.6-terra",
}
DEFAULT_AI_MODEL_TIER = "mid"
AI_OUTPUT_LANGUAGES = ("chinese", "english")
DEFAULT_AI_OUTPUT_LANGUAGE = "chinese"
ALLOWED_KNOWLEDGE_MATURITY_DAYS = (7, 14, 30)
DEFAULT_KNOWLEDGE_MATURITY_DAYS = 14
AI_WINDOW_GAP_HOURS = int(os.getenv("ZOOM_KB_AI_WINDOW_GAP_HOURS", "6"))
AI_WINDOW_MAX_MESSAGES = int(os.getenv("ZOOM_KB_AI_WINDOW_MAX_MESSAGES", "60"))
AI_WINDOW_MAX_CHARS = int(os.getenv("ZOOM_KB_AI_WINDOW_MAX_CHARS", "24000"))

