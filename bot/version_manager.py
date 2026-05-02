import json
import httpx
from bot.config import API_BASE, VERSION_FILE
from bot.utils.logger import get_logger

log = get_logger(__name__)


class VersionManager:
    def __init__(self):
        self.version = None

    # ── Load dari cache ────────────────────────────────
    def load_local(self):
        try:
            if VERSION_FILE.exists():
                data = json.loads(VERSION_FILE.read_text())
                self.version = data.get("version")
                log.info("Loaded cached version: %s", self.version)
                return self.version
        except Exception as e:
            log.warning("Failed to load cached version: %s", e)
        return None

    # ── Save ke cache ────────────────────────────────
    def save_local(self, version: str):
        try:
            VERSION_FILE.write_text(json.dumps({"version": version}))
            log.info("Saved version: %s", version)
        except Exception as e:
            log.warning("Failed to save version: %s", e)

    # ── Fetch dari server ─────────────────────────────
    async def fetch_remote(self):
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as client:
                resp = await client.get("/version")

                if resp.status_code != 200:
                    log.warning("Version fetch failed: status=%s", resp.status_code)
                    return None

                data = resp.json()
                version = data.get("data", {}).get("version") or data.get("version")

                if version:
                    log.info("Fetched server version: %s", version)
                    self.version = version
                    self.save_local(version)
                    return version

        except Exception as e:
            log.warning("Version fetch error: %s", e)

        return None

    # ── Init (load → fallback fetch) ──────────────────
    async def init(self):
        v = self.load_local()
        if v:
            return v

        return await self.fetch_remote()

    # ── Force refresh (dipakai saat 426) ──────────────
    async def refresh(self):
        log.info("Refreshing version from server...")
        return await self.fetch_remote()

def load_from_skill_file(self):
    try:
        from pathlib import Path

        skill_path = Path("skill.md")  # atau path kamu
        if not skill_path.exists():
            return None

        text = skill_path.read_text()

        for line in text.splitlines():
            if line.startswith("version:"):
                version = line.split(":", 1)[1].strip()
                log.info("Version from skill.md: %s", version)
                self.version = version
                return version

    except Exception as e:
        log.warning("Failed reading skill.md: %s", e)

    return None
