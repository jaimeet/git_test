import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
IMAGES_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "hisab.db"

# Which AI reads the photos: auto | ollama | gemini | claude.
# "auto" picks the first one configured, in that order: OLLAMA_URL, GEMINI_API_KEY, ANTHROPIC_API_KEY.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "auto").strip().lower()

# Local model served by Ollama (https://ollama.com) on another machine, e.g. http://192.168.1.50:11434
OLLAMA_URL = os.environ.get("OLLAMA_URL", "").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-vl:8b")
# Seconds to wait for one sheet. CPU-only laptops can take several minutes.
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "900"))
# Context window for the request; the photo plus a long sheet's JSON needs room.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))

# Google Gemini: free API key from https://aistudio.google.com/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
# "gemini-flash-latest" always points at Google's current Flash model, which the free tier covers.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
# low | medium | high | xhigh | max — higher reads messy handwriting more carefully but costs more.
PARSE_EFFORT = os.environ.get("PARSE_EFFORT", "high")

# Optional HTTP basic auth for the whole app. Leave unset on a trusted LAN.
APP_USERNAME = os.environ.get("APP_USERNAME", "hisab")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

CURRENCY = os.environ.get("CURRENCY", "₹")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))

# Public base URL of this app, used for links in WhatsApp replies, e.g. http://192.168.1.20:8092
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# WhatsApp via a WAHA container (https://github.com/devlikeapro/waha) linked to your number.
WAHA_URL = os.environ.get("WAHA_URL", "").rstrip("/")  # e.g. http://waha:3000; empty disables WhatsApp
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
# Must equal WHATSAPP_HOOK_HMAC_KEY on the WAHA container; webhook calls without a valid signature are rejected.
WHATSAPP_WEBHOOK_SECRET = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "")
# Optional fixed chat id. Normally you pick the chat by sending "hisab here" in it.
WHATSAPP_CHAT_ID = os.environ.get("WHATSAPP_CHAT_ID", "")
WHATSAPP_REPLY = os.environ.get("WHATSAPP_REPLY", "true").lower() in ("1", "true", "yes")
# Save WhatsApp sheets straight to the ledger when nothing looks off (totals match, no unclear lines).
WHATSAPP_AUTO_SAVE = os.environ.get("WHATSAPP_AUTO_SAVE", "false").lower() in ("1", "true", "yes")


def whatsapp_enabled() -> bool:
    return bool(WAHA_URL)


def ensure_dirs() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
