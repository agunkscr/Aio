"""
Action envelope builder + cooldown state tracker.
UPDATED for newer Molty Royale schema
"""
from bot.utils.logger import get_logger
from bot.config import SKILL_VERSION

log = get_logger(__name__)

COOLDOWN_ACTIONS = {"move", "attack", "use_item", "interact", "rest"}
FREE_ACTIONS = {"pickup", "equip", "talk", "whisper", "broadcast"}


class ActionSender:
    """Tracks cooldown state and builds action envelopes."""

    def __init__(self):
        self.can_act = True
        self.cooldown_remaining_ms = 0

    def update_from_result(self, result: dict):
        if isinstance(result, dict):
            self.can_act = result.get("canAct", self.can_act)
            self.cooldown_remaining_ms = result.get("cooldownRemainingMs", 0)

    def update_from_can_act_changed(self, msg: dict):
        self.can_act = msg.get("canAct", True)
        self.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)

    def can_send_cooldown_action(self) -> bool:
        return self.can_act

    # ✅ NEW SCHEMA ONLY
    def build_action(self, action_type: str, data: dict = None) -> dict:
        return {
            "type": "action",
            "version": SKILL_VERSION,
            "action": {
                "name": action_type,
                "params": data or {}
            }
        }

    # ── Convenience builders ──────────────────────────────────────────

    def move(self, region_id: str) -> dict:
        return self.build_action("move", {"regionId": region_id})

    def attack(self, target_id: str, target_type: str = "agent") -> dict:
        return self.build_action("attack", {
            "targetId": target_id,
            "targetType": target_type
        })

    def use_item(self, item_id: str) -> dict:
        return self.build_action("use_item", {"itemId": item_id})

    def interact(self, interactable_id: str) -> dict:
        return self.build_action("interact", {"interactableId": interactable_id})

    def rest(self) -> dict:
        return self.build_action("rest", {})

    def pickup(self, item_id: str) -> dict:
        return self.build_action("pickup", {"itemId": item_id})

    def equip(self, weapon_id: str) -> dict:
        return self.build_action("equip", {"itemId": weapon_id})

    def talk(self, message: str) -> dict:
        return self.build_action("talk", {"message": message[:200]})

    def whisper(self, target_id: str, message: str) -> dict:
        return self.build_action("whisper", {
            "targetId": target_id,
            "message": message[:200]
        })

    def broadcast(self, message: str) -> dict:
        return self.build_action("broadcast", {"message": message[:200]})
