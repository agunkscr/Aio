"""
WebSocket gameplay engine — wss://cdn.moltyroyale.com/ws/agent.
Core loop: connect → process messages → decide → act → repeat.

Fixes (v1.8.1):
- Now calls decide_actions() and sends all free actions + main cooldown action.
- Correctly skips cooldown actions when can_act=False.
"""
import json
import asyncio
import websockets
from bot.config import WS_URL, SKILL_VERSION
from bot.credentials import get_api_key
from bot.game.action_sender import ActionSender, COOLDOWN_ACTIONS, FREE_ACTIONS
from bot.strategy.brain import decide_actions, reset_game_state, learn_from_map  # v1.8.1: use decide_actions
from bot.dashboard.state import dashboard_state
from bot.utils.rate_limiter import ws_limiter
from bot.utils.logger import get_logger

log = get_logger(__name__)


def _update_dz_knowledge(view: dict):
    """Continuously track death zones from every agent_view.
    Updates brain._map_knowledge with any new DZ regions observed.
    v1.5.2: pendingDeathzones entries are {id, name} objects.
    """
    from bot.strategy.brain import _map_knowledge
    # Track DZ from visible regions
    for region in view.get("visibleRegions", []):
        if isinstance(region, dict) and region.get("isDeathZone"):
            rid = region.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
    # Track from connected regions (type-safe: may be string IDs or objects)
    for conn in view.get("connectedRegions", []):
        if isinstance(conn, dict) and conn.get("isDeathZone"):
            rid = conn.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
    # Track current region
    cur = view.get("currentRegion", {})
    if isinstance(cur, dict) and cur.get("isDeathZone"):
        rid = cur.get("id", "")
        if rid:
            _map_knowledge["death_zones"].add(rid)
    # Track pending DZ — v1.5.2: entries are {id, name} objects
    for dz in view.get("pendingDeathzones", []):
        if isinstance(dz, dict):
            rid = dz.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
        elif isinstance(dz, str):
            _map_knowledge["death_zones"].add(dz)


class WebSocketEngine:
    """Manages the gameplay WebSocket session."""

    def __init__(self, game_id: str, agent_id: str, ws=None):
        self.game_id = game_id
        self.agent_id = agent_id
        self.action_sender = ActionSender()
        self.ws = None
        self.game_result = None
        self.last_view = None
        self._ping_task = None
        self._running = False
        self._map_just_used = False
        self.dashboard_key = agent_id
        self.dashboard_name = "Agent"
        self._handoff_ws = ws

    async def run(self) -> dict:
        if self._handoff_ws is not None:
            log.info("✅ Using handoff socket from /ws/join for game=%s", self.game_id)
            self._running = True
            self.ws = self._handoff_ws
            try:
                self._ping_task = asyncio.create_task(self._ping_loop())
                async for raw_msg in self.ws:
                    try:
                        msg = json.loads(raw_msg)
                        if not isinstance(msg, dict):
                            continue
                        result = await self._handle_message(msg)
                        if result is not None:
                            self._running = False
                            return result
                    except json.JSONDecodeError:
                        log.warning("Non-JSON message: %s", raw_msg[:100])
            except Exception as e:
                log.error("Handoff socket error: %s — falling through to reconnect", e)
            finally:
                if self._ping_task:
                    self._ping_task.cancel()
            self._handoff_ws = None

        api_key = get_api_key()
        headers = {
            "X-API-Key": api_key,
            "X-Version": SKILL_VERSION,
        }

        self._running = True
        retry_count = 0
        max_retries = 5

        while self._running and retry_count < max_retries:
            try:
                log.info("Connecting WebSocket to %s...", WS_URL)
                async with websockets.connect(
                    WS_URL,
                    additional_headers=headers,
                    ping_interval=None,
                    max_size=2**20,
                ) as ws:
                    self.ws = ws
                    retry_count = 0
                    log.info("✅ WebSocket connected for game=%s", self.game_id)

                    self._ping_task = asyncio.create_task(self._ping_loop())

                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                            if not isinstance(msg, dict):
                                log.warning("Non-dict WS message: %s", type(msg).__name__)
                                continue
                            msg_type = msg.get("type", "unknown")
                            log.debug("WS recv: type=%s", msg_type)
                            result = await self._handle_message(msg)
                            if result is not None:
                                self._running = False
                                return result
                        except json.JSONDecodeError:
                            log.warning("Non-JSON message: %s", raw_msg[:100])

            except websockets.exceptions.ConnectionClosed as e:
                retry_count += 1
                log.warning("WebSocket closed: code=%s reason=%s (retry %d/%d)",
                            e.code, e.reason, retry_count, max_retries)
                if self._ping_task:
                    self._ping_task.cancel()
                await asyncio.sleep(min(2 ** retry_count, 30))

            except Exception as e:
                retry_count += 1
                log.error("WebSocket error: %s (retry %d/%d)", e, retry_count, max_retries)
                if self._ping_task:
                    self._ping_task.cancel()
                await asyncio.sleep(min(2 ** retry_count, 30))

        return self.game_result or {"status": "disconnected"}

    async def _handle_message(self, msg: dict) -> dict | None:
        msg_type = msg.get("type", "")

        if msg_type == "agent_view":
            view = msg.get("view") or msg.get("data") or {}
            if isinstance(view, dict) and view:
                self.last_view = view
                reason = msg.get("reason", "initial")
                alive = view.get("self", {}).get("isAlive", "?")
                hp = view.get("self", {}).get("hp", "?")
                ep = view.get("self", {}).get("ep", "?")
                log.info("agent_view (reason=%s) alive=%s HP=%s EP=%s", reason, alive, hp, ep)
                await self._on_agent_view(view)
            else:
                log.warning("agent_view with empty/invalid view: %s", str(view)[:100])

        elif msg_type == "action_result":
            success = msg.get("success", False)
            self.action_sender.can_act = msg.get("canAct", self.action_sender.can_act)
            self.action_sender.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)

            if success:
                data = msg.get("data", {})
                action_msg = data.get("message", "") if isinstance(data, dict) else str(data)
                log.info("Action OK: %s (canAct=%s)", action_msg, msg.get("canAct"))
                if isinstance(data, dict) and "map" in str(action_msg).lower():
                    self._map_just_used = True
            else:
                err = msg.get("error", {})
                err_code = err.get("code", "") if isinstance(err, dict) else str(err)
                err_msg = err.get("message", "") if isinstance(err, dict) else ""
                log.warning("Action FAILED: %s — %s (canAct=%s)", err_code, err_msg, msg.get("canAct"))

        elif msg_type == "can_act_changed":
            self.action_sender.can_act = msg.get("canAct", True)
            self.action_sender.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)
            log.info("can_act_changed: canAct=%s", msg.get("canAct"))
            if self.last_view and msg.get("canAct"):
                await self._on_agent_view(self.last_view)

        elif msg_type == "turn_advanced":
            turn_num = msg.get("turn", "?")
            view = msg.get("view")
            if not view and isinstance(msg.get("data"), dict):
                view = msg["data"].get("view")
                turn_num = msg["data"].get("turn", turn_num)

            log.info("Turn %s — processing view...", turn_num)
            if view and isinstance(view, dict):
                self.last_view = view
                await self._on_agent_view(view)
            elif self.last_view:
                await self._on_agent_view(self.last_view)
            else:
                log.warning("Turn advanced but no view data available")

        elif msg_type == "game_ended":
            log.info("═══ GAME ENDED ═══")
            reset_game_state()
            self.game_result = msg
            return msg

        elif msg_type == "event":
            event_type = msg.get("eventType", msg.get("data", {}).get("eventType", ""))
            log.debug("Event: %s", event_type)

        elif msg_type == "waiting":
            log.info("Game is waiting for players...")

        elif msg_type == "pong":
            pass

        elif msg_type == "error":
            err_msg = msg.get("message", msg.get("data", {}).get("message", str(msg)))
            log.error("Server error: %s", err_msg)

        else:
            log.info("Unknown WS message type=%s keys=%s", msg_type, list(msg.keys()))

        return None

    async def _on_agent_view(self, view: dict):
        """Process agent_view → decide all actions → send free + main."""
        if not isinstance(view, dict):
            return

        self_data = view.get("self", {})
        if not isinstance(self_data, dict):
            return

        alive_count = view.get("aliveCount", "?")

        if not self_data.get("isAlive", True):
            log.info("☠️ Agent DEAD — Alive remaining: %s. Waiting for game_ended...", alive_count)
            dk = self.dashboard_key
            dashboard_state.update_agent(dk, {
                "name": self.dashboard_name,
                "status": "dead",
                "hp": 0,
                "ep": 0,
                "maxHp": self_data.get("maxHp", 100),
                "maxEp": self_data.get("maxEp", 10),
                "alive_count": alive_count,
                "last_action": "☠️ DEAD — waiting for game to end",
                "enemies": [],
                "region_items": [],
            })
            dashboard_state.add_log(
                f"☠️ Agent DEAD — Alive remaining: {alive_count}",
                "warning", dk
            )
            return

        hp = self_data.get("hp", "?")
        ep = self_data.get("ep", "?")
        region = view.get("currentRegion", {})
        region_name = region.get("name", "?") if isinstance(region, dict) else "?"
        log.info("Status: HP=%s EP=%s Region=%s | Alive: %s", hp, ep, region_name, alive_count)
        dashboard_state.add_log(
            f"HP={hp} EP={ep} Region={region_name} | Alive: {alive_count}",
            "info", self.dashboard_key
        )

        inv = self_data.get("inventory", [])
        enemies = [a for a in view.get("visibleAgents", [])
                   if isinstance(a, dict) and a.get("isAlive") and a.get("id") != self_data.get("id")]

        region_id = region.get("id", "") if isinstance(region, dict) else ""

        def _unwrap_items(raw_items):
            result = []
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("item")
                if isinstance(inner, dict):
                    inner["regionId"] = entry.get("regionId", "")
                    result.append(inner)
                elif entry.get("id"):
                    result.append(entry)
            return result

        region_items = []

        if isinstance(region, dict) and region.get("items"):
            region_items = _unwrap_items(region["items"])

        if not region_items:
            all_visible = _unwrap_items(view.get("visibleItems", []))
            region_items = [i for i in all_visible
                            if i.get("regionId") == region_id]

        if not region_items:
            all_visible = _unwrap_items(view.get("visibleItems", []))
            if all_visible:
                region_items = all_visible

        equipped = self_data.get("equippedWeapon")
        weapon_name = "fist"
        weapon_bonus = 0
        if equipped and isinstance(equipped, dict):
            weapon_name = equipped.get("typeId", "fist")
            from bot.strategy.brain import WEAPONS
            weapon_bonus = WEAPONS.get(weapon_name.lower(), {}).get("bonus", 0)

        def _item_label(i):
            return (i.get("name") or i.get("typeId") or i.get("type") or i.get("itemType")
                    or i.get("itemName") or i.get("label") or i.get("kind") or str(i.get("id", "?"))[:12])

        def _item_cat(i):
            return (i.get("category") or i.get("cat") or i.get("itemCategory") or i.get("type") or "")

        dk = self.dashboard_key
        dashboard_state.update_agent(dk, {
            "name": self.dashboard_name,
            "hp": hp, "ep": ep,
            "status": "playing",
            "maxHp": self_data.get("maxHp", 100),
            "maxEp": self_data.get("maxEp", 10),
            "atk": self_data.get("atk", 0),
            "def": self_data.get("def", 0),
            "weapon": weapon_name,
            "weapon_bonus": weapon_bonus,
            "kills": self_data.get("kills", 0),
            "region": region_name,
            "alive_count": alive_count,
            "inventory": [{"typeId": i.get("typeId","?"), "name": _item_label(i), "cat": _item_cat(i)}
                          for i in inv if isinstance(i, dict)],
            "enemies": [{"name": e.get("name","?"), "hp": e.get("hp","?"), "id": e.get("id","")}
                        for e in enemies[:8]],
            "region_items": [{"typeId": i.get("typeId","?"), "name": _item_label(i), "cat": _item_cat(i)}
                             for i in region_items[:10]],
        })

        if self._map_just_used:
            self._map_just_used = False
            learn_from_map(view)
            log.info("🗺️ Map knowledge updated — DZ tracking active")

        _update_dz_knowledge(view)

        # ── v1.8.1: Use decide_actions to get ALL actions (free + main) ──
        can_act = self.action_sender.can_send_cooldown_action()
        decisions = decide_actions(view, can_act)   # returns list of dicts

        for decision in decisions:
            action_type = decision["action"]
            action_data = decision.get("data", {})
            reason = decision.get("reason", "")

            # Only send cooldown actions when can_act is True
            if action_type in COOLDOWN_ACTIONS and not can_act:
                log.debug("Skipping cooldown action %s (can_act=False)", action_type)
                continue

            payload = self.action_sender.build_action(
                action_type, action_data, reason, action_type,
            )
            await self._send(payload)
            log.info("→ %s | %s", action_type.upper(), reason)

            # Update dashboard with each action sent
            dashboard_state.update_agent(self.dashboard_key, {"last_action": f"{action_type}: {reason[:60]}"})
            dashboard_state.add_log(f"{action_type}: {reason[:80]}", "info", self.dashboard_key)

    async def _send(self, payload: dict):
        if self.ws is None:
            return
        await ws_limiter.acquire()
        await self.ws.send(json.dumps(payload))

    async def _ping_loop(self):
        try:
            while self._running:
                await asyncio.sleep(15)
                if self.ws:
                    await self._send({"type": "ping"})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("Ping loop error: %s", e)