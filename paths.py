"""Where Violet keeps its data. VIOLET_DATA_DIR overrides it (the isolation tests use a temp folder)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VIOLET_DATA_DIR") or ROOT / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
VIOLET_DB = DATA_DIR / "violet.db"
MESSAGES_DB = DATA_DIR / "messages.db"
FILES_DIR = DATA_DIR / "files"
