import os
from pathlib import Path

# ── Core ─────────────────────────────
API_BASE = os.getenv("API_BASE", "https://api.moltyroyale.com")

# Version (runtime mutable via VersionManager)
SKILL_VERSION = os.getenv("SKILL_VERSION", "1.5.2")

# ── Paths ────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

# Root data dir (bisa override di Railway)
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "dev-agent"))

# Pastikan folder ada
DATA_DIR.mkdir(parents=True, exist_ok=True)

# File cache
VERSION_FILE = DATA_DIR / "version.json"

# Credentials & wallet files
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
AGENT_WALLET_FILE = DATA_DIR / "agent-wallet.json"
OWNER_WALLET_FILE = DATA_DIR / "owner-wallet.json"
OWNER_INTAKE_FILE = DATA_DIR / "owner-intake.json"

# ── Logging ──────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Feature flags ────────────────────
def _env_bool(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")

ADVANCED_MODE   = _env_bool("ADVANCED_MODE", "true")
AUTO_SC_WALLET  = _env_bool("AUTO_SC_WALLET", "true")
AUTO_WHITELIST  = _env_bool("AUTO_WHITELIST", "true")
AUTO_IDENTITY   = _env_bool("AUTO_IDENTITY", "true")
ENABLE_MEMORY   = _env_bool("ENABLE_MEMORY", "true")

ROOM_MODE = os.getenv("ROOM_MODE", "auto")

# ── Debug info (optional, aman karena no logger import) ──
if os.getenv("DEBUG_CONFIG", "false").lower() == "true":
    print("[CONFIG] DATA_DIR =", DATA_DIR)
    print("[CONFIG] API_BASE =", API_BASE)
    print("[CONFIG] VERSION  =", SKILL_VERSION)
