"""
Free game join via /ws/join unified WebSocket.

Per skill v1.6.0 Core Rule #1:
  Open /ws/join → welcome frame → hello{entryType:"free"} → assigned.
  The SAME socket becomes the gameplay socket; do NOT re-dial.
"""
from bot.game.ws_join import WsJoinSession
from bot.api_client import APIError
from bot.utils.logger import get_logger

log = get_logger(__name__)


async def join_free_game(api=None) -> tuple[str, str, object]:
    """
    Enter free matchmaking via /ws/join.

    Returns (game_id, agent_id, websocket).
    The websocket is already the live gameplay socket —
    pass it directly to WebSocketEngine(game_id, agent_id, ws=ws).

    Raises:
        APIError    — READINESS_BLOCKED, NOT_PRIMARY_AGENT, NO_IDENTITY
        RuntimeError — timeout or protocol error
    """
    session = WsJoinSession(preferred_entry="free")
    try:
        game_id, agent_id, ws = await session.join()
        log.info("✅ Free game joined: game=%s agent=%s", game_id, agent_id)
        return game_id, agent_id, ws
    except APIError as e:
        if e.code == "NOT_PRIMARY_AGENT":
            log.error(
                "❌ NOT_PRIMARY_AGENT — only the smallest accounts.id for this "
                "SC wallet may enter rooms. See references/sc-wallet-policy.md"
            )
        elif e.code in ("READINESS_BLOCKED", "NO_IDENTITY"):
            log.error("❌ Identity/readiness block: %s", e.message)
        raise
