import asyncio
from bot.api_client import MoltyAPI, APIError
from bot.config import fetch_server_version
from bot.dashboard.state import dashboard_state
from bot.state_router import determine_state, NO_IDENTITY, IN_GAME, READY_PAID, READY_FREE
from bot.setup.account_setup import ensure_account_ready
from bot.setup.wallet_setup import ensure_molty_wallet
from bot.setup.whitelist import ensure_whitelist
from bot.setup.identity import ensure_identity
from bot.game.room_selector import select_room
from bot.game.free_join import join_free_game
from bot.game.paid_join import join_paid_game
from bot.game.websocket_engine import WebSocketEngine
from bot.game.settlement import settle_game
from bot.memory.agent_memory import AgentMemory
from bot.credentials import load_credentials, get_api_key
from bot.config import (
    ADVANCED_MODE, ROOM_MODE, AUTO_WHITELIST,
    AUTO_SC_WALLET, ENABLE_MEMORY, AUTO_IDENTITY,
)
from bot.utils.logger import get_logger

log = get_logger(__name__)


class Heartbeat:

    def __init__(self):
        self.api: MoltyAPI | None = None
        self.memory = AgentMemory()
        self.running = True
        self._agent_key = "agent-1"
        self._agent_name = "Agent"

    async def run(self):
        log.info("═══════════════════════════════════════════")
        log.info("  MOLTY ROYALE AI AGENT — STARTING")
        log.info("═══════════════════════════════════════════")

        log.info("Config:")
        log.info("  ADVANCED_MODE   = %s", ADVANCED_MODE)
        log.info("  AUTO_SC_WALLET  = %s", AUTO_SC_WALLET)
        log.info("  AUTO_WHITELIST  = %s", AUTO_WHITELIST)
        log.info("  ENABLE_MEMORY   = %s", ENABLE_MEMORY)
        log.info("  AUTO_IDENTITY   = %s", AUTO_IDENTITY)
        log.info("  ROOM_MODE       = %s", ROOM_MODE)

        creds = None
        while self.running and not creds:
            try:
                creds = await ensure_account_ready()
                api_key = creds.get("api_key") or get_api_key()

                if not api_key:
                    log.error("No API key. Retry 60s")
                    creds = None
                    await asyncio.sleep(60)

            except Exception as e:
                log.error("Account setup error: %s", e)
                await asyncio.sleep(60)

        if not self.running:
            return

        self.api = MoltyAPI(creds.get("api_key") or get_api_key())

        dashboard_state.bots_running = 1
        dashboard_state.add_log("Bot started", "info")

        if ENABLE_MEMORY:
            await self.memory.load()

        consecutive_errors = 0

        while self.running:
            try:
                await self._heartbeat_cycle()
                consecutive_errors = 0

            except APIError as e:
                # 🔥 HANDLE VERSION MISMATCH
                if e.code == "VERSION_MISMATCH":
                    log.warning("⚠️ Version mismatch — syncing with server...")

                    await fetch_server_version()

                    # Reset client biar header update
                    if self.api:
                        await self.api.close()
                        self.api = MoltyAPI(get_api_key())

                    await asyncio.sleep(2)
                    continue

                if e.status == 401:
                    log.error("Invalid API key — stopping")
                    self.running = False
                    return

                log.error("API error: %s", e)

            except Exception as e:
                consecutive_errors += 1
                wait = min(10 * (2 ** min(consecutive_errors - 1, 4)), 120)

                log.error(
                    "Heartbeat error (#%d): %s → retry %ds",
                    consecutive_errors, e, wait
                )

                await asyncio.sleep(wait)

        if self.api:
            await self.api.close()

        log.info("Agent stopped.")

    async def _heartbeat_cycle(self):

        me = await self.api.get_accounts_me()

        state, ctx = determine_state(me)
        log.info("State: %s", state)

        self._agent_key = str(me.get("agentId", me.get("id", "agent-1")))
        self._agent_name = me.get("agentName", me.get("name", "Agent"))

        balance = me.get("balance", 0)

        dashboard_state.total_smoltz = balance
        dashboard_state.update_agent(self._agent_key, {
            "name": self._agent_name,
            "status": "playing" if state == IN_GAME else "idle",
            "smoltz": balance,
            "whitelisted": state != NO_IDENTITY,
        })

        if state == NO_IDENTITY:
            await self._handle_no_identity()
            return

        if state == IN_GAME:
            await self._handle_in_game(ctx)
            return

        if state in (READY_FREE, READY_PAID):
            await self._handle_ready(me)

    async def _handle_no_identity(self):
        creds = load_credentials() or {}

        owner_eoa = creds.get("owner_eoa", "")
        agent_eoa = creds.get("agent_wallet_address", "")

        if AUTO_SC_WALLET:
            await ensure_molty_wallet(self.api, owner_eoa)

        if AUTO_WHITELIST:
            ok = await ensure_whitelist(self.api, owner_eoa, agent_eoa)
            if not ok:
                log.info("Waiting whitelist (fund CROSS)")
                await asyncio.sleep(120)
                return

        if AUTO_IDENTITY:
            ok = await ensure_identity(self.api)
            if not ok:
                await asyncio.sleep(30)
                return

        log.info("✅ Setup complete")

    async def _handle_ready(self, me: dict):
        room_type = select_room(me)

        try:
            if room_type == "paid":
                game_id, agent_id = await join_paid_game(self.api)
            else:
                game_id, agent_id = await join_free_game(self.api)

        except Exception as e:
            log.warning("Join failed: %s", e)
            await asyncio.sleep(10)
            return

        await self._play_game(game_id, agent_id, room_type)

    async def _handle_in_game(self, ctx: dict):
        await self._play_game(
            ctx["game_id"],
            ctx["agent_id"],
            ctx.get("entry_type", "free")
        )

    async def _play_game(self, game_id: str, agent_id: str, entry_type: str):

        log.info("═══ PLAYING GAME: %s (%s) ═══", game_id, entry_type)

        self.memory.set_temp_game(game_id)
        await self.memory.save()

        engine = WebSocketEngine(game_id, agent_id)
        engine.dashboard_key = self._agent_key
        engine.dashboard_name = self._agent_name

        result = await engine.run()

        await settle_game(result, entry_type, self.memory)

        log.info("Game done → next in 5s")
        await asyncio.sleep(5)
