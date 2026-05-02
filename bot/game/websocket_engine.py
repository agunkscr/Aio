"""
WebSocket gameplay engine — updated for newer Molty Royale protocol.

Key changes:
- NO X-Version header
- MUST send init handshake after connect
- Action payload uses version field (handled by ActionSender)
"""

import json
import asyncio
import websockets

from bot.config import WS_URL, SKILL_VERSION
from bot.credentials import get_api_key
from bot.game.action_sender import ActionSender, COOLDOWN_ACTIONS
from bot.strategy.brain import decide_action, reset_game_state, learn_from_map
from bot.dashboard.state import dashboard_state
from bot.utils.rate_limiter import ws_limiter
from bot.utils.logger import get_logger

log = get_logger(__name__)


class WebSocketEngine:
    def __init__(self, game_id: str, agent_id: str):
        self.game_id = game_id
        self.agent_id = agent_id
        self.action_sender = ActionSender()
        self.ws = None
        self._running = False
        self.last_view = None
        self._ping_task = None
        self._map_just_used = False

        self.dashboard_key = agent_id
        self.dashboard_name = "Agent"

    async def run(self):
        api_key = get_api_key()

        headers = {
            "X-API-Key": api_key
        }

        self._running = True

        while self._running:
            try:
                log.info("Connecting to WS...")

                async with websockets.connect(
                    WS_URL,
                    additional_headers=headers,
                    ping_interval=None
                ) as ws:

                    self.ws = ws
                    log.info("✅ Connected")

                    # 🔥 INIT HANDSHAKE (WAJIB)
                    await self._send({
                        "type": "init",
                        "version": SKILL_VERSION
                    })

                    # fallback (beberapa server pakai join)
                    await asyncio.sleep(0.5)
                    await self._send({
                        "type": "join",
                        "version": SKILL_VERSION
                    })

                    # start ping
                    self._ping_task = asyncio.create_task(self._ping_loop())

                    async for raw in ws:
                        msg = json.loads(raw)
                        result = await self._handle(msg)
                        if result:
                            return result

            except Exception as e:
                log.error("WS error: %s", e)
                await asyncio.sleep(3)

        return {"status": "stopped"}

    async def _handle(self, msg: dict):
        t = msg.get("type")

        if t == "agent_view":
            view = msg.get("view") or {}
            self.last_view = view
            await self._on_view(view)

        elif t == "turn_advanced":
            view = msg.get("view") or msg.get("data", {}).get("view")
            if view:
                self.last_view = view
                await self._on_view(view)

        elif t == "action_result":
            self.action_sender.update_from_result(msg)

        elif t == "can_act_changed":
            self.action_sender.update_from_can_act_changed(msg)

        elif t == "game_ended":
            reset_game_state()
            return msg

        elif t == "error":
            log.error("Server error: %s", msg)

        return None

    async def _on_view(self, view: dict):
        if not view:
            return

        me = view.get("self", {})
        if not me.get("isAlive", True):
            log.info("Dead")
            return

        can_act = self.action_sender.can_send_cooldown_action()

        decision = decide_action(view, can_act)
        if not decision:
            return

        action_type = decision["action"]
        data = decision.get("data", {})

        if action_type in COOLDOWN_ACTIONS and not can_act:
            return

        payload = self.action_sender.build_action(action_type, data)

        await self._send(payload)

    async def _send(self, payload: dict):
        await ws_limiter.acquire()
        await self.ws.send(json.dumps(payload))

    async def _ping_loop(self):
        while self._running:
            await asyncio.sleep(15)
            try:
                await self._send({
                    "type": "ping",
                    "version": SKILL_VERSION
                })
            except:
                pass
