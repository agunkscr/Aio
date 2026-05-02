"""
Unified WebSocket join via wss://cdn.moltyroyale.com/ws/join.

Per skill v1.6.0 Core Rule #1:
  1. Open /ws/join ONCE
  2. Read server's `welcome` frame → check `decision`
  3. Send ONE `hello` frame with entryType + optional mode
  4. Same socket progresses through join state machine
  5. Same socket becomes gameplay socket → pass to WebSocketEngine

decision values:
  ASK_ENTRY_TYPE  → server waits for our hello
  FREE_ONLY       → only free accepted
  PAID_ONLY       → only paid accepted
  BLOCKED         → no ERC-8004, closes 4001
  ALREADY_IN_GAME → resume; state router should have caught this
"""
import json
import asyncio
import websockets
from bot.config import WS_JOIN_URL, SKILL_VERSION
from bot.credentials import get_api_key
from bot.api_client import APIError
from bot.utils.logger import get_logger

log = get_logger(__name__)

# States the join state machine emits before gameplay starts
_JOIN_TERMINAL = {"assigned", "game_started", "waiting"}
_JOIN_ERROR    = {"blocked", "rejected", "error"}

# How long to wait for assignment after hello (seconds)
ASSIGNMENT_TIMEOUT = 60


class WsJoinSession:
    """
    Manages the /ws/join lifecycle.

    Usage:
        session = WsJoinSession(preferred_entry="free")
        game_id, agent_id, ws = await session.join()
        # ws is now the live gameplay socket — pass directly to WebSocketEngine
        engine = WebSocketEngine(game_id, agent_id, ws=ws)
        result = await engine.run()
    """

    def __init__(self, preferred_entry: str = "free", mode: str = "offchain"):
        self.preferred_entry = preferred_entry  # "free" | "paid"
        self.mode = mode                        # "offchain" | "onchain"
        self._ws = None

    # ── Public API ────────────────────────────────────────────────────

    async def join(self) -> tuple[str, str, object]:
        """
        Full join flow.
        Returns (game_id, agent_id, websocket).
        Raises APIError on BLOCKED / unrecoverable states.
        Raises RuntimeError on timeout or protocol errors.
        """
        api_key = get_api_key()
        headers = {
            "Authorization": f"mr-auth {api_key}",
            "X-Version": SKILL_VERSION,
        }

        log.info("Opening /ws/join (preferred=%s mode=%s)...", self.preferred_entry, self.mode)

        ws = await websockets.connect(
            WS_JOIN_URL,
            additional_headers=headers,
            ping_interval=None,
            max_size=2 ** 20,
        )
        self._ws = ws

        try:
            game_id, agent_id = await asyncio.wait_for(
                self._run_join_handshake(ws),
                timeout=ASSIGNMENT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await ws.close()
            raise RuntimeError(
                f"No game assignment after {ASSIGNMENT_TIMEOUT}s on /ws/join"
            )
        except Exception:
            await ws.close()
            raise

        log.info("✅ /ws/join assigned: game=%s agent=%s", game_id, agent_id)
        return game_id, agent_id, ws

    # ── Internals ─────────────────────────────────────────────────────

    async def _run_join_handshake(self, ws) -> tuple[str, str]:
        """
        Step 1: read welcome frame
        Step 2: send hello frame
        Step 3: wait for assignment
        """
        # ── Step 1: welcome ──────────────────────────────────────────
        raw = await ws.recv()
        welcome = self._parse(raw, "welcome")

        msg_type = welcome.get("type", "")
        if msg_type != "welcome":
            raise RuntimeError(f"Expected 'welcome' frame, got '{msg_type}'")

        decision = welcome.get("decision", "ASK_ENTRY_TYPE")
        log.info("/ws/join welcome: decision=%s", decision)

        # Handle non-joinable decisions immediately
        if decision == "BLOCKED":
            missing = welcome.get("readiness", {}).get("missing", [])
            codes = [m.get("code", "") for m in missing if isinstance(m, dict)]
            # Surface NOT_PRIMARY_AGENT if it's the blocker
            if "NOT_PRIMARY_AGENT" in codes:
                raise APIError("NOT_PRIMARY_AGENT",
                               "Only the primary agent for this SC wallet may enter rooms",
                               403)
            raise APIError("READINESS_BLOCKED",
                           f"Agent blocked — missing: {codes or 'unknown'}", 403)

        if decision == "ALREADY_IN_GAME":
            # State router should have caught this; extract game info if available
            gid = welcome.get("gameId", "")
            aid = welcome.get("agentId", "")
            if gid and aid:
                log.warning("Already in game — reusing: game=%s", gid)
                return gid, aid
            raise RuntimeError("ALREADY_IN_GAME but no gameId in welcome frame")

        # Resolve entry type from server decision
        entry_type = self._resolve_entry(decision)
        log.info("Using entryType=%s (decision=%s)", entry_type, decision)

        # ── Step 2: hello ────────────────────────────────────────────
        hello: dict = {"type": "hello", "entryType": entry_type}
        if entry_type == "paid":
            hello["mode"] = self.mode
        await ws.send(json.dumps(hello))
        log.debug("Sent hello: %s", hello)

        # ── Step 3: wait for assignment ──────────────────────────────
        return await self._wait_for_assignment(ws)

    async def _wait_for_assignment(self, ws) -> tuple[str, str]:
        """Read frames until we get game_id + agent_id assignment."""
        async for raw in ws:
            msg = self._parse(raw, "join-loop")
            msg_type = msg.get("type", "unknown")
            log.debug("/ws/join recv: type=%s", msg_type)

            if msg_type in ("queued", "matchmaking"):
                log.info("Queued — waiting for match...")
                continue

            if msg_type == "waiting":
                # Game formed, waiting for players to fill
                gid = msg.get("gameId", "")
                aid = msg.get("agentId", "")
                if gid and aid:
                    log.info("Game waiting: game=%s agent=%s", gid, aid)
                    return gid, aid
                log.debug("Waiting frame without gameId — continuing...")
                continue

            if msg_type in ("assigned", "game_started", "agent_view"):
                # Fully assigned — extract ids from whichever field they arrive in
                gid = (msg.get("gameId")
                       or msg.get("game_id")
                       or (msg.get("view") or {}).get("gameId", ""))
                aid = (msg.get("agentId")
                       or msg.get("agent_id")
                       or (msg.get("view") or {}).get("agentId", ""))
                if gid and aid:
                    return gid, aid
                log.warning("Assigned frame missing gameId/agentId: %s", str(msg)[:120])
                continue

            if msg_type in _JOIN_ERROR or msg_type == "error":
                err_msg = msg.get("message") or msg.get("data", {}).get("message", str(msg))
                err_code = msg.get("code", "JOIN_ERROR")
                raise APIError(err_code, err_msg, 0)

            # Unknown frame — log and continue (socket is still live)
            log.debug("Unhandled /ws/join frame: type=%s", msg_type)

        raise RuntimeError("/ws/join closed before assignment")

    def _resolve_entry(self, decision: str) -> str:
        """Map server decision to entry type string."""
        if decision == "FREE_ONLY":
            return "free"
        if decision == "PAID_ONLY":
            return "paid"
        # ASK_ENTRY_TYPE → use our preference
        return self.preferred_entry

    @staticmethod
    def _parse(raw, context: str) -> dict:
        try:
            msg = json.loads(raw)
            return msg if isinstance(msg, dict) else {}
        except json.JSONDecodeError:
            log.warning("Non-JSON frame in %s: %s", context, str(raw)[:80])
            return {}
