import os
from pathlib import Path

# ── Core ─────────────────────────────
API_BASE = os.getenv("API_BASE", "https://api.moltyroyale.com")

# Version (akan diupdate runtime)
SKILL_VERSION = os.getenv("SKILL_VERSION", "1.5.2")

# File cache
BASE_DIR = Path(__file__).resolve().parent
VERSION_FILE = BASE_DIR / "version.json"

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Feature flags
ADVANCED_MODE = os.getenv("ADVANCED_MODE", "true").lower() == "true"
AUTO_SC_WALLET = os.getenv("AUTO_SC_WALLET", "true").lower() == "true"
AUTO_WHITELIST = os.getenv("AUTO_WHITELIST", "true").lower() == "true"
AUTO_IDENTITY = os.getenv("AUTO_IDENTITY", "true").lower() == "true"
ENABLE_MEMORY = os.getenv("ENABLE_MEMORY", "true").lower() == "true"

ROOM_MODE = os.getenv("ROOM_MODE", "auto")
