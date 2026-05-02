import json
import httpx
from pathlib import Path
from bot.config import API_BASE, VERSION_FILE
from bot.utils.logger import get_logger

log = get_logger(__name__)


class VersionManager:
    def __init__(self):
        self.version = None

    # ── Load cache ────────────────────────────────
    def load_local(self):
        try:
            if VERSION_FILE.exists():
                data = json.loads(VERSION_FILE.read_text())
                v = data.get("version")

                if v:
                    self.version = v
                    log.info("Loaded cached version: %s", v)
                    return v

        except Exception as e:
            log.warning("Failed to load cached version: %s", e)

        return None

    # ── Save cache ────────────────────────────────
    def save_local(self, version: str):
        try:
            VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            VERSION_FILE.write_text(json.dumps({"version": version}))
            log.info("Saved version: %s", version)
        except Exception as e:
            log.warning("Failed to save version: %s", e)

    # ── Fetch server ──────────────────────────────
    async def fetch_remote(self):
        try:
            async with httpx.AsyncClient(base_url=API_BASE, timeout=10) as client:
                resp = await client.get("/version")

                if resp.status_code != 200:
                    log.warning("Version fetch failed: %s", resp.status_code)
                    return None

                try:
                    data = resp.json()
                except Exception:
                    log.warning("Invalid JSON from /version")
                    return None

                version = data.get("data", {}).get("version") or data.get("version")

                if version:
                    self.version = version
                    log.info("Fetched server version: %s", version)
                    self.save_local(version)
                    return version

        except Exception as e:
            log.warning("Version fetch error: %s", e)

        return None

    # ── Fallback skill.md ─────────────────────────
    def load_from_skill_file(self):
        try:
            skill_path = Path("skill.md")

            if not skill_path.exists():
                return None

            text = skill_path.read_text()

            for line in text.splitlines():
                if line.lower().startswith("version:"):
                    version = line.split(":", 1)[1].strip()

                    if version:
                        self.version = version
                        log.info("Version from skill.md: %s", version)
                        return version

        except Exception as e:
            log.warning("Failed reading skill.md: %s", e)

        return None

    # ── Init flow (SMART) ─────────────────────────
    async def init(self):
        # 1. coba server dulu (paling akurat)
        v = await self.fetch_remote()
        if v:
            return v

        # 2. fallback skill.md
        v = self.load_from_skill_file()
        if v:
            return v

        # 3. terakhir cache
        return self.load_local()

    # ── Refresh (dipanggil saat 426) ─────────────
    async def refresh(self):
        log.info("Refreshing version from server...")
        return await self.fetch_remote()
