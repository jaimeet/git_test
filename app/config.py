import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
IMAGES_DIR = DATA_DIR / "images"
DB_PATH = DATA_DIR / "hisab.db"

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
# low | medium | high | xhigh | max — higher reads messy handwriting more carefully but costs more.
PARSE_EFFORT = os.environ.get("PARSE_EFFORT", "high")

# Optional HTTP basic auth for the whole app. Leave unset on a trusted LAN.
APP_USERNAME = os.environ.get("APP_USERNAME", "hisab")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

CURRENCY = os.environ.get("CURRENCY", "₹")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))


def ensure_dirs() -> None:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
