import json
import httpx
from typing import Optional
from bot.config import API_BASE
from bot.utils.logger import get_logger
from bot.utils.rate_limiter import rest_limiter
from bot.version_manager import VersionManager

log = get_logger(__name__)


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 0):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"[{code}] {message}")


class MoltyAPI:
    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None
        self.version_manager = VersionManager()

    # ── HEADERS ─────────────────────────────
    async def _headers(self) -> dict:
        if not self.version_manager.version:
            await self.version_manager.init()

        h = {
            "X-Version": self.version_manager.version
        }

        if self.api_key:
            h["X-API-Key"] = self.api_key

        return h

    # ── CLIENT ──────────────────────────────
    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            headers = await self._headers()

            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers=headers,
            )

    async def _reset_client(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ── SAFE JSON ───────────────────────────
    def _safe_parse_json(self, text: str) -> dict:
        text = text.strip()
        if not text:
            return {}

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            try:
                obj, _ = decoder.raw_decode(text)
                return obj
            except json.JSONDecodeError:
                log.warning("Bad JSON: %s", text[:100])
                return {}

    # ── CORE REQUEST ────────────────────────
    async def _request(self, method: str, path: str, retry=True, **kwargs) -> dict:
        await rest_limiter.acquire()
        await self._ensure_client()

        resp = await self._client.request(method, path, **kwargs)

        # 🔥 HANDLE VERSION MISMATCH
        if resp.status_code == 426 and retry:
            log.warning("⚠️ VERSION_MISMATCH → refreshing...")

            new_version = await self.version_manager.refresh()

            if new_version:
                await self._reset_client()
                return await self._request(method, path, retry=False, **kwargs)

            raise APIError("VERSION_MISMATCH", "Failed to refresh version", 426)

        # Rate limit
        if resp.status_code == 429:
            raise APIError("RATE_LIMITED", "Too many requests", 429)

        data = self._safe_parse_json(resp.text)

        # Error format API
        if isinstance(data, dict) and not data.get("success", True) and "error" in data:
            err = data["error"]
            raise APIError(
                err.get("code", "UNKNOWN"),
                err.get("message", "Unknown error"),
                resp.status_code,
            )

        if isinstance(data, dict):
            result = data.get("data", data)
            return result if isinstance(result, dict) else {"value": result}

        return {"_raw": data}

    # ── ENDPOINTS ───────────────────────────

    async def get_accounts_me(self) -> dict:
        return await self._request("GET", "/accounts/me")

    async def create_account(self, name: str, wallet_address: str) -> dict:
        return await self._request("POST", "/accounts", json={
            "name": name,
            "wallet_address": wallet_address,
        })

    async def put_wallet(self, wallet_address: str) -> dict:
        return await self._request("PUT", "/accounts/wallet", json={
            "wallet_address": wallet_address,
        })

    async def create_wallet(self, owner_eoa: str) -> dict:
        return await self._request("POST", "/create/wallet", json={
            "ownerEoa": owner_eoa,
        })

    async def whitelist_request(self, owner_eoa: str) -> dict:
        return await self._request("POST", "/whitelist/request", json={
            "ownerEoa": owner_eoa,
        })

    async def post_identity(self, agent_id: int) -> dict:
        return await self._request("POST", "/identity", json={
            "agentId": agent_id,
        })

    async def get_identity(self) -> dict:
        return await self._request("GET", "/identity")

    async def delete_identity(self) -> dict:
        return await self._request("DELETE", "/identity")

    async def post_join(self, entry_type: str = "free") -> dict:
        return await self._request("POST", "/join", json={
            "entryType": entry_type
        })

    async def get_join_status(self) -> dict:
        return await self._request("GET", "/join/status")

    async def get_games(self, status: str = "waiting") -> dict:
        return await self._request("GET", "/games", params={"status": status})

    async def get_join_paid_message(self, game_id: str) -> dict:
        return await self._request("GET", f"/games/{game_id}/join-paid/message")

    async def post_join_paid(self, game_id: str, deadline: str,
                             signature: str, mode: str = "offchain") -> dict:
        body = {"deadline": deadline, "signature": signature}
        if mode == "onchain":
            body["mode"] = "onchain"

        return await self._request("POST", f"/games/{game_id}/join-paid", json=body)

    async def get_version(self) -> dict:
        return await self._request("GET", "/version")

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
