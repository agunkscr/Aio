"""
Agent memory — persistent cross-game learning via molty-royale-context.json.
Two sections: `overall` (persistent) and `temp` (per-game).
"""
import copy
import json
from bot.config import MEMORY_DIR, MEMORY_FILE
from bot.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_MEMORY = {
    "overall": {
        "identity": {"name": "", "playstyle": "adaptive guardian hunter"},
        "strategy": {
            "deathzone": "move inward before turn 5",
            "guardians": "engage immediately — highest sMoltz value",
            "weather": "avoid combat in fog or storm",
            "ep_management": "rest when EP < 4 before engaging",
        },
        "history": {
            "totalGames": 0,
            "wins": 0,
            "avgKills": 0.0,
            "totalSmoltzEarned": 0,   # FIX: was silently dropped before
            "lessons": [],
        },
    },
    "temp": {},
}


def _default_memory() -> dict:
    """Return a deep copy of DEFAULT_MEMORY to avoid shared mutable state."""
    return copy.deepcopy(DEFAULT_MEMORY)


class AgentMemory:
    """Read/write molty-royale-context.json with overall + temp sections."""

    def __init__(self):
        self.data = _default_memory()
        self._loaded = False

    async def load(self):
        """Load memory from disk. Create default if missing."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if MEMORY_FILE.exists():
            try:
                raw = MEMORY_FILE.read_text(encoding="utf-8")
                loaded = json.loads(raw)
                # Merge loaded data onto defaults so new keys are always present
                merged = _default_memory()
                self._deep_merge(merged, loaded)
                self.data = merged
                self._loaded = True
                log.info("Memory loaded: %d games, %d lessons",
                         self.data["overall"]["history"]["totalGames"],
                         len(self.data["overall"]["history"]["lessons"]))
            except (json.JSONDecodeError, KeyError) as e:
                log.warning("Memory file corrupt, using defaults: %s", e)
                self.data = _default_memory()
                self._loaded = True  # FIX: mark loaded even on corrupt so save() is safe
        else:
            log.info("No memory file — starting fresh")
            self._loaded = True  # FIX: mark loaded so set_temp_game+save won't overwrite

    async def save(self):
        """Persist memory to disk.

        FIX: Guard against saving unloaded (empty) state over existing file.
        Only write if _loaded is True.
        """
        if not self._loaded:
            log.warning("save() called before load() — skipping to protect existing data")
            return
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        MEMORY_FILE.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.debug("Memory saved to %s", MEMORY_FILE)

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _deep_merge(base: dict, override: dict):
        """Recursively merge override into base (in place).
        Leaf values from override win; missing keys in override keep base defaults.
        """
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                AgentMemory._deep_merge(base[key], val)
            else:
                base[key] = val

    # ── Identity ──────────────────────────────────────────────────────

    def set_agent_name(self, name: str):
        self.data["overall"]["identity"]["name"] = name

    def get_strategy(self) -> dict:
        return self.data.get("overall", {}).get("strategy", {})

    def get_lessons(self) -> list:
        return self.data.get("overall", {}).get("history", {}).get("lessons", [])

    # ── Temp (per-game) ───────────────────────────────────────────────

    def set_temp_game(self, game_id: str):
        self.data["temp"] = {
            "gameId": game_id,
            "currentStrategy": "adaptive",
            "knownAgents": [],
            "notes": "",
        }

    def update_temp_note(self, note: str):
        if "temp" not in self.data:
            self.data["temp"] = {}
        existing = self.data["temp"].get("notes", "")
        self.data["temp"]["notes"] = f"{existing}\n{note}".strip()

    def clear_temp(self):
        self.data["temp"] = {}

    # ── History update (after game end) ───────────────────────────────

    def record_game_end(self, is_winner: bool, final_rank: int,
                        kills: int, smoltz_earned: int = 0):
        history = self.data["overall"]["history"]
        history["totalGames"] += 1
        if is_winner:
            history["wins"] += 1

        # Rolling average kills
        total = history["totalGames"]
        old_avg = history["avgKills"]
        history["avgKills"] = round(((old_avg * (total - 1)) + kills) / total, 2)

        # FIX: persist sMoltz rewards — was silently discarded before
        history["totalSmoltzEarned"] = history.get("totalSmoltzEarned", 0) + smoltz_earned

    def add_lesson(self, lesson: str, max_lessons: int = 20):
        """Append a new lesson, keeping max_lessons most recent."""
        lessons = self.data["overall"]["history"]["lessons"]
        if lesson not in lessons:
            lessons.append(lesson)
            if len(lessons) > max_lessons:
                lessons.pop(0)
