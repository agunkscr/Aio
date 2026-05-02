import os
import asyncio
import httpx
from bot.utils.logger import get_logger

log = get_logger(__name__)

API_BASE = os.getenv("API_BASE", "https://api.moltyroyale.com")

# Default fallback (dipakai kalau fetch gagal)
DEFAULT_VERSION = os.getenv("SKILL_VERSION", "1.5.2")

# Cache global
_skill_version = DEFAULT_VERSION
_last_fetch = 0
_lock = asyncio.Lock()


async def fetch_server_version() -> str:
    """Fetch latest version from server."""
    global _skill_version, _last_fetch

    async with _lock:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{API_BASE}/version")

                if resp.status_code == 200:
                    data = resp.json()
                    version = data.get("data", {}).get("version")

                    if version:
                        _skill_version = version
                        _last_fetch = asyncio.get_event_loop().time()
                        log.info("🔄 Updated SKILL_VERSION → %s", version)
                        return version

        except Exception as e:
            log.warning("Failed to fetch version: %s", e)

    return _skill_version


async def get_skill_version() -> str:
    """Return cached version (refresh every 5 minutes)."""
    now = asyncio.get_event_loop().time()

    if now - _last_fetch > 300:
        return await fetch_server_version()

    return _skill_version
