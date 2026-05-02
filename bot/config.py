import os
from pathlib import Path

# ── Core ─────────────────────────────
API_BASE = os.getenv("API_BASE", "https://api.moltyroyale.com")

# Version (akan diupdate runtime oleh VersionManager)
SKILL_VERSION = os.getenv("SKILL_VERSION", "1.5.2")

# ── Paths ────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

# Folder utama agent
DEV_AGENT_DIR = BASE_DIR / "dev-agent"
DEV_AGENT_DIR.mkdir(exist_ok=True)

# File cache
VERSION_FILE = BASE_DIR / "version.json"

# Credential files
CREDENTIALS_FILE = DEV_AGENT_DIR / "credentials.json"
OWNER_WALLET_FILE = DEV_AGENT_DIR / "owner-wallet.json"
AGENT_WALLET_FILE = DEV_AGENT_DIR / "agent-wallet.json"
OWNER_INTAKE_FILE = DEV_AGENT_DIR / "owner-intake.json"
MEMORY_FILE = DEV_AGENT_DIR / "memory.json"

# ── Logging ──────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Feature flags ────────────────────
ADVANCED_MODE = os.getenv("ADVANCED_MODE", "true").lower() == "true"
AUTO_SC_WALLET = os.getenv("AUTO_SC_WALLET", "true").lower() == "true"
AUTO_WHITELIST = os.getenv("AUTO_WHITELIST", "true").lower() == "true"
AUTO_IDENTITY = os.getenv("AUTO_IDENTITY", "true").lower() == "true"
ENABLE_MEMORY = os.getenv("ENABLE_MEMORY", "true").lower() == "true"

ROOM_MODE = os.getenv("ROOM_MODE", "auto")
