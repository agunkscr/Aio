"""
Strategy brain — BERSERKER MODE v7.0 (TERMINATOR - NO MERCY)
===========================================================================
FITUR UTAMA v7.0:
- INSTANT COUNTER: Setiap diserang, langsung counter attack tanpa kompromi
- EXECUTE UNTIL DEATH: Chase musuh sampai mati, tidak ada kesempatan kabur
- NO FLEE: Hapus semua logika flee (kecuali deathzone)
- PREDATOR MOVEMENT: Langsung pindah ke region musuh yang terlihat
- BLOODLUST: Prioritaskan musuh yang pernah menyerang kita
- ZERO TOLERANCE: Attack terus sampai HP 0 atau musuh mati
===========================================================================
"""

import time
from collections import defaultdict, deque
from enum import Enum
from bot.utils.logger import get_logger

log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  KONFIGURASI SENJATA
# ═══════════════════════════════════════════════════════════════════

WEAPONS = {
    "fist":   {"bonus": 0,  "range": 0, "tier": 0, "damage": 4},
    "dagger": {"bonus": 10, "range": 0, "tier": 1, "damage": 14},
    "bow":    {"bonus": 5,  "range": 1, "tier": 1, "damage": 9},
    "pistol": {"bonus": 10, "range": 1, "tier": 2, "damage": 14},
    "sword":  {"bonus": 20, "range": 0, "tier": 3, "damage": 24},
    "sniper": {"bonus": 28, "range": 2, "tier": 4, "damage": 32},
    "katana": {"bonus": 35, "range": 0, "tier": 5, "damage": 39},
}

WEAPON_PRIORITY = ["katana", "sniper", "sword", "pistol", "dagger", "bow", "fist"]

ITEM_PRIORITY = {
    "rewards": 300,
    "katana": 100, "sniper": 98, "sword": 95, "pistol": 85,
    "dagger": 80, "bow": 75,
    "medkit": 90, "bandage": 80, "emergency_food": 70, "energy_drink": 60,
    "binoculars": 55, "map": 52, "megaphone": 40,
}

RECOVERY_ITEMS = {
    "medkit": 50, "bandage": 30, "emergency_food": 20, "energy_drink": 0,
}

WEATHER_COMBAT_PENALTY = {
    "clear": 0.0, "rain": 0.05, "fog": 0.10, "storm": 0.15,
}


# ═══════════════════════════════════════════════════════════════════
#  KONFIGURASI BERSERKER v7.0 - TERMINATOR MODE
# ═══════════════════════════════════════════════════════════════════

BERSERKER_CONFIG = {
    # ── HP & EP Management ───────────────────────────────────────────
    "HP_MINIMUM":            10,
    "HP_CRITICAL":           5,
    "HP_HEAL_URGENT":        20,
    "HP_HEAL_MODERATE":      40,
    "EP_MINIMUM_RATIO":      0.20,
    "EP_ATTACK_MIN_RATIO":   0.05,      # Bisa attack meskipun EP hampir habis
    "EP_SAFE_RATIO":         0.05,

    # ── Combat (NO MERCY!) ───────────────────────────────────────────
    "MIN_HP_TO_ATTACK":      10,        # Attack sampai HP 10!
    "MIN_HP_TO_ATTACK_GUARDIAN": 25,
    "COUNTER_ATTACK_HP":     5,         # Counter attack bahkan di HP 5!
    
    # ── NO FLEE! ─────────────────────────────────────────────────────
    "FLEE_HP":               0,         # TIDAK PERNAH FLEE karena HP!
    "FLEE_OUTNUMBERED":      99,        # TIDAK PERNAH FLEE karena outnumbered!
    "FLEE_STRONG_ENEMY_RATIO": 999,     # TIDAK PERNAH FLEE karena damage difference!
    "MAX_ENEMY_DAMAGE_RATIO": 999,      # TIDAK PERNAH cancel attack!
    
    # ── Blacklist (Hanya untuk yang benar-benar gila) ─────────────────
    "BLACKLIST_DAMAGE_THRESHOLD": 70,   # Hanya blacklist damage > 70
    "BLACKLIST_WINRATE_THRESHOLD": 0.9,
    
    # ── Survival Mode ───────────────────────────────────────────────
    "SURVIVAL_MODE_HP":      10,
    "FARM_TURNS_BEFORE_FIGHT": 0,
    
    # ── Pursuit (HUNT SAMPAI MATI!) ───────────────────────────────────
    "PURSUIT_ENABLED":       True,
    "PURSUIT_MAX_HOPS":      99,        # Chase kemana pun!
    "PURSUIT_MIN_HP":        10,        # Chase bahkan di HP 10!
    "PURSUIT_MAX_ENEMY_DAMAGE": 999,    # Chase musuh apapun!
    
    # ── Hunting (BLOODLUST) ──────────────────────────────────────────
    "HUNTING_MODE":          True,
    "HUNT_UNTIL_DEATH":      True,
    "TARGET_MARK_DURATION":  100,       # Mark target lebih lama
    "EXECUTE_HP_THRESHOLD":  70,        # Execute jika musuh HP < 70!
    "WOUNDED_HP_THRESHOLD":  80,
    "MIN_HP_TO_HUNT":        10,
    "MIN_DAMAGE_TO_HUNT":    3,

    # ── Inventory Management ────────────────────────────────────────
    "INV_MAX_CAPACITY":      12,
    "INV_DROP_THRESHOLD":    10,

    # ── Facility ────────────────────────────────────────────────────
    "MAX_FACILITY_INTERACTIONS": 1,
    "FACILITY_COOLDOWN_TURNS":   5,
    "BROADCAST_STATION_ONCE":    True,

    # ── Post-Heal Behavior ──────────────────────────────────────────
    "SAFE_TURNS_AFTER_HEAL": 1,
    
    # ── Combat Priorities ───────────────────────────────────────────
    "COUNTER_ATTACK_PRIORITY": True,    # Counter attack adalah prioritas #1
    "EXECUTE_PRIORITY": True,           # Execute low HP enemies first
    "REVENGE_PRIORITY": True,           # Target yang pernah menyerang kita
}


# ═══════════════════════════════════════════════════════════════════
#  ENEMY PLAYER STYLE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════

class PlayerStyle(Enum):
    AGGRESSOR = "aggressor"
    KITER = "kiter"
    HEALER = "healer"
    CAMPER = "camper"
    BERSERKER = "berserker"
    COUNTER_ATTACKER = "counter"
    OPPORTUNIST = "opportunist"
    ESCAPIST = "escapist"
    STRONG = "strong"
    BLACKLISTED = "blacklisted"
    PREY = "prey"  # Target yang sedang diburu


# ═══════════════════════════════════════════════════════════════════
#  ENEMY PROFILE
# ═══════════════════════════════════════════════════════════════════

class EnemyMemory:
    def __init__(self, enemy_id: str):
        self.id = enemy_id
        self.first_seen = time.time()
        
        self.encounters = 0
        self.victories_against_us = 0
        self.defeats_by_us = 0
        self.last_encounter_turn = 0
        self.last_encounter_result = None
        
        self.combat_logs = deque(maxlen=20)
        self.real_damage_samples = []
        self.estimated_damage = 10
        self.is_blacklisted = False
        self.blacklist_reason = ""
        self.attacked_us_count = 0
        self.last_attacked_turn = 0
        
    def record_real_damage(self, damage: int):
        self.real_damage_samples.append(damage)
        if len(self.real_damage_samples) > 5:
            self.real_damage_samples.pop(0)
        self.estimated_damage = sum(self.real_damage_samples) // max(1, len(self.real_damage_samples))
        
        if self.estimated_damage >= BERSERKER_CONFIG["BLACKLIST_DAMAGE_THRESHOLD"]:
            self.is_blacklisted = True
            self.blacklist_reason = f"damage={self.estimated_damage}"
    
    def record_attacked_us(self, turn: int, damage: int):
        self.attacked_us_count += 1
        self.last_attacked_turn = turn
        self.record_real_damage(damage)
    
    def record_combat(self, combat_data: dict):
        self.combat_logs.append(combat_data)
        self.last_encounter_turn = combat_data.get("turn", 0)
        self.last_encounter_result = combat_data.get("result", "unknown")
        
        if combat_data.get("result") == "loss":
            self.victories_against_us += 1
        elif combat_data.get("result") == "win":
            self.defeats_by_us += 1


# ═══════════════════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════════════════

_known_agents: dict = {}
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": []}
_hunting_target: dict = None
_hunting_timer: int = 0
_interacted_facilities: dict = {}
_recovery_mode: bool = False
_last_attacked_by: str = None
_last_attacked_turn: int = 0
_last_attacked_damage: int = 0
_broadcast_used: bool = False
_broadcast_region_used: str = None
_last_heal_turn: int = 0
_post_heal_safe_turns: int = 0
_revenge_target: str = None  # Musuh yang sedang kita balas

_enemy_memories: dict = {}
_current_combat_state = {
    "in_combat": False,
    "with_enemy": None,
    "start_turn": 0,
    "my_hp_start": 100,
    "enemy_hp_start": 100,
    "my_strategy": "terminator",
}
_active_special_counters: dict = {}


# ═══════════════════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def learn_from_map(view: dict):
    """Required by websocket_engine"""
    global _map_knowledge
    
    try:
        visible_regions = view.get("visibleRegions", [])
        if not visible_regions:
            return
        
        _map_knowledge["revealed"] = True
        _map_knowledge["death_zones"] = set()
        _map_knowledge["safe_center"] = []
        
        for region in visible_regions:
            if not isinstance(region, dict):
                continue
            rid = region.get("id", "")
            if not rid:
                continue
            if region.get("isDeathZone"):
                _map_knowledge["death_zones"].add(rid)
        
        log.info(f"🗺️ learn_from_map: {len(_map_knowledge['death_zones'])} death zones")
        
    except Exception as e:
        log.error(f"learn_from_map error: {e}")


def calc_damage(atk: int, weapon_bonus: int, target_def: int, weather: str = "clear") -> int:
    base = atk + weapon_bonus - int(target_def * 0.5)
    penalty = WEATHER_COMBAT_PENALTY.get(weather, 0.0)
    return max(1, int(base * (1 - penalty)))


def get_weapon_bonus(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("bonus", 0)


def get_weapon_damage(equipped_weapon) -> int:
    if not equipped_weapon:
        return WEAPONS["fist"]["damage"]
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, WEAPONS["fist"]).get("damage", 4)


def get_weapon_range(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("range", 0)


def _resolve_region(entry, view: dict):
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        for r in view.get("visibleRegions", []):
            if isinstance(r, dict) and r.get("id") == entry:
                return r
    return None


def _get_move_ep_cost(terrain: str, weather: str) -> int:
    if terrain == "water":
        return 3
    if weather == "storm":
        return 3
    return 2


def _estimate_enemy_weapon_bonus(agent: dict) -> int:
    weapon = agent.get("equippedWeapon")
    if not weapon:
        return 0
    type_id = weapon.get("typeId", "").lower() if isinstance(weapon, dict) else ""
    return WEAPONS.get(type_id, {}).get("bonus", 0)


def _select_weakest(targets: list) -> dict:
    return min(targets, key=lambda t: t.get("hp", 999))


def _is_in_range(target: dict, my_region: str, weapon_range: int, connections=None) -> bool:
    target_region = target.get("regionId", "")
    if not target_region or target_region == my_region:
        return True
    if weapon_range >= 1 and connections:
        adj_ids = set()
        for conn in connections:
            if isinstance(conn, str):
                adj_ids.add(conn)
            elif isinstance(conn, dict):
                adj_ids.add(conn.get("id", ""))
        if target_region in adj_ids:
            return True
    return False


def _find_healing_item(inventory: list) -> dict | None:
    heals = [i for i in inventory if isinstance(i, dict)
             and i.get("typeId", "").lower() in RECOVERY_ITEMS
             and RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0) > 0]
    if not heals:
        return None
    heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0),
               reverse=True)
    return heals[0]


def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None


def _check_equip(inventory: list, equipped) -> dict | None:
    current_damage = get_weapon_damage(equipped)
    best, best_damage = None, current_damage
    for item in inventory:
        if not isinstance(item, dict) or item.get("category") != "weapon":
            continue
        damage = get_weapon_damage(item)
        if damage > best_damage:
            best, best_damage = item, damage
    if best:
        log.info(f"🔫 EQUIP: {best.get('typeId', 'weapon')} (+{best_damage} DMG)")
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"EQUIP"}
    return None


def _smart_pickup(items: list, inventory: list, region_id: str, equipped) -> dict | None:
    local_items = [i for i in items
                   if isinstance(i, dict) and i.get("regionId") == region_id]
    if not local_items:
        local_items = [i for i in items if isinstance(i, dict) and i.get("id")]
    if not local_items:
        return None
    
    # Prioritaskan weapon
    for item in local_items:
        if item.get("category") == "weapon":
            weapon_type = item.get("typeId", "").lower()
            damage = WEAPONS.get(weapon_type, {}).get("damage", 0)
            current_damage = get_weapon_damage(equipped)
            if damage > current_damage:
                log.info(f"📦 PICKUP WEAPON: {weapon_type}")
                return {"action": "pickup", "data": {"itemId": item["id"]},
                        "reason": f"PICKUP: {weapon_type}"}
    
    # Prioritaskan healing items jika HP rendah
    if self_data and self_data.get("hp", 100) < 40:
        for item in local_items:
            if item.get("typeId", "").lower() in RECOVERY_ITEMS:
                log.info(f"💊 PICKUP HEALING: {item.get('typeId')}")
                return {"action": "pickup", "data": {"itemId": item["id"]},
                        "reason": f"PICKUP"}
    
    return None


def _track_agents(visible_agents: list, my_id: str, my_region: str, current_turn: int):
    global _known_agents
    for agent in visible_agents:
        if not isinstance(agent, dict):
            continue
        aid = agent.get("id", "")
        if not aid or aid == my_id:
            continue
        _known_agents[aid] = {
            "hp": agent.get("hp", 100),
            "atk": agent.get("atk", 10),
            "isGuardian": agent.get("isGuardian", False),
            "equippedWeapon": agent.get("equippedWeapon"),
            "lastSeen": my_region,
            "isAlive": agent.get("isAlive", True),
            "regionId": agent.get("regionId", my_region),
        }


def get_or_create_memory(enemy_id: str) -> EnemyMemory:
    global _enemy_memories
    if enemy_id not in _enemy_memories:
        _enemy_memories[enemy_id] = EnemyMemory(enemy_id)
    return _enemy_memories[enemy_id]


def reset_game_state():
    global _known_agents, _map_knowledge, _hunting_target, _hunting_timer
    global _interacted_facilities, _recovery_mode, _last_attacked_by, _last_attacked_turn
    global _broadcast_used, _broadcast_region_used, _enemy_memories
    global _active_special_counters, _current_combat_state, _last_heal_turn, _post_heal_safe_turns
    global _revenge_target, _last_attacked_damage
    
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _hunting_target = None
    _hunting_timer = 0
    _interacted_facilities = {}
    _recovery_mode = False
    _last_attacked_by = None
    _last_attacked_turn = 0
    _last_attacked_damage = 0
    _broadcast_used = False
    _broadcast_region_used = None
    _last_heal_turn = 0
    _post_heal_safe_turns = 0
    _revenge_target = None
    
    _active_special_counters = {}
    _current_combat_state = {
        "in_combat": False,
        "with_enemy": None,
        "start_turn": 0,
        "my_hp_start": 100,
        "enemy_hp_start": 100,
        "my_strategy": "terminator",
    }
    
    log.info("=" * 65)
    log.info("  🔥 BERSERKER v7.0 — TERMINATOR MODE ACTIVE!")
    log.info("  No Flee. No Mercy. Fight Until Death.")
    log.info("=" * 65)


def on_attacked_by(attacker_id: str, current_turn: int, damage: int = None):
    global _last_attacked_by, _last_attacked_turn, _last_attacked_damage, _revenge_target
    
    _last_attacked_by = attacker_id
    _last_attacked_turn = current_turn
    _last_attacked_damage = damage or 0
    _revenge_target = attacker_id  # Set revenge target
    
    if attacker_id:
        memory = get_or_create_memory(attacker_id)
        memory.record_attacked_us(current_turn, damage or 10)
    
    log.warning(f"⚠️ ATTACKED by {attacker_id[:8]} for {damage} dmg - PREPARING COUNTER!")


def on_enemy_killed(enemy_id: str):
    global _hunting_target, _revenge_target
    
    if _hunting_target and _hunting_target.get("id") == enemy_id:
        _hunting_target = None
    
    if _revenge_target == enemy_id:
        _revenge_target = None
        log.info(f"✅ REVENGE COMPLETE! {enemy_id[:8]} eliminated!")
    
    log.info(f"✅ KILLED {enemy_id[:8]}")


def on_we_died(killer_id: str, combat_summary: dict = None):
    log.warning(f"💀 DIED by {killer_id[:8]}")
    if combat_summary:
        memory = get_or_create_memory(killer_id)
        memory.record_combat({
            "result": "loss",
            "turn": combat_summary.get("turn", 0),
            "my_hp_end": combat_summary.get("my_hp_final", 0),
            "enemy_hp_end": combat_summary.get("enemy_hp_final", 100),
        })
    reset_game_state()


def print_learning_summary():
    print("\n" + "="*60)
    print("🔥 BERSERKER v7.0 - TERMINATOR MODE 🔥")
    print("="*60)
    print(f"Total enemies encountered: {len(_enemy_memories)}")
    for eid, mem in _enemy_memories.items():
        print(f"  • {eid[:8]}: attacked us {mem.attacked_us_count}x, dmg={mem.estimated_damage}")
    print("="*60 + "\n")


def get_all_enemy_intel() -> list:
    return [{"id": eid[:8], "damage": mem.estimated_damage, "attacks": mem.attacked_us_count} 
            for eid, mem in _enemy_memories.items()]


# ═══════════════════════════════════════════════════════════════════
#  MAIN DECISION ENGINE v7.0 - TERMINATOR MODE
# ═══════════════════════════════════════════════════════════════════

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    global _hunting_target, _hunting_timer, _recovery_mode
    global _last_attacked_by, _last_attacked_turn, _current_combat_state
    global _post_heal_safe_turns, _revenge_target

    self_data = view.get("self", {})
    region = view.get("currentRegion", {})
    hp = self_data.get("hp", 100)
    ep = self_data.get("ep", 10)
    max_ep = self_data.get("maxEp", 10)
    atk = self_data.get("atk", 10)
    defense = self_data.get("def", 5)
    is_alive = self_data.get("isAlive", True)
    inventory = self_data.get("inventory", [])
    equipped = self_data.get("equippedWeapon")
    my_id = self_data.get("id", "")

    visible_agents = view.get("visibleAgents", [])
    visible_monsters = view.get("visibleMonsters", [])
    visible_items_raw = view.get("visibleItems", [])

    visible_items = []
    for entry in visible_items_raw:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("item")
        if isinstance(inner, dict):
            inner["regionId"] = entry.get("regionId", "")
            visible_items.append(inner)
        elif entry.get("id"):
            visible_items.append(entry)

    connections = view.get("connectedRegions", []) or region.get("connections", [])
    pending_dz = view.get("pendingDeathzones", [])
    current_turn = view.get("turn", 0) or int(time.time())
    region_id = region.get("id", "")
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""

    if _post_heal_safe_turns > 0:
        _post_heal_safe_turns -= 1

    if not is_alive:
        return None

    if _hunting_timer > 0:
        _hunting_timer -= 1
    elif _hunting_target:
        _hunting_target = None

    # Deathzone detection
    danger_ids = set()
    for dz in pending_dz:
        if isinstance(dz, dict):
            danger_ids.add(dz.get("id", ""))
        elif isinstance(dz, str):
            danger_ids.add(dz)
    for conn in connections:
        resolved = _resolve_region(conn, view)
        if resolved and resolved.get("isDeathZone"):
            danger_ids.add(resolved.get("id", ""))

    _track_agents(visible_agents, my_id, region_id, current_turn)
    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)
    ep_ratio = ep / max_ep if max_ep > 0 else 1.0

    # Find enemies in same region
    enemies_here = [a for a in visible_agents
                    if a.get("isAlive", True) and a.get("id") != my_id
                    and a.get("regionId") == region_id
                    and not a.get("isGuardian", False)]

    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]

    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]

    just_attacked = (current_turn - _last_attacked_turn) <= 2 and _last_attacked_by
    my_damage = get_weapon_damage(equipped)

    # ================================================================
    # PRIORITY 0: DEATHZONE ESCAPE (ONLY FLEE SCENARIO!)
    # ================================================================
    if region.get("isDeathZone", False) or region_id in danger_ids:
        for conn in connections:
            if isinstance(conn, dict):
                rid = conn.get("id", "")
                if rid and not conn.get("isDeathZone"):
                    log.warning(f"💀 DEATHZONE! Moving to {rid}")
                    return {"action": "move", "data": {"regionId": rid}, "reason": "DEATHZONE"}
            elif isinstance(conn, str):
                if conn:
                    log.warning(f"💀 DEATHZONE! Moving to {conn}")
                    return {"action": "move", "data": {"regionId": conn}, "reason": "DEATHZONE"}

    # ================================================================
    # PRIORITY 1: EMERGENCY HEAL (ONLY IF CRITICAL)
    # ================================================================
    if hp < 10:
        heal = _find_healing_item(inventory)
        if heal:
            log.warning(f"🚨 EMERGENCY HEAL! HP={hp} -> {heal.get('typeId')}")
            _post_heal_safe_turns = 1
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "EMERGENCY"}

    # ================================================================
    # PRIORITY 2: COUNTER ATTACK - INSTANT BALAS! (PALING PENTING!)
    # ================================================================
    if just_attacked and _last_attacked_by:
        # Cari attacker di region yang sama
        attacker = None
        for e in enemies_here:
            if e.get("id") == _last_attacked_by:
                attacker = e
                break
        
        if attacker:
            log.warning(f"⚔️⚔️⚔️ INSTANT COUNTER ATTACK vs {_last_attacked_by[:8]}! HP={hp}")
            return {"action": "attack",
                    "data": {"targetId": attacker["id"], "targetType": "agent"},
                    "reason": "COUNTER_ATTACK"}

    # ================================================================
    # PRIORITY 3: ATTACK ENEMIES - NO MERCY!
    # ================================================================
    if enemies_here:
        # Prioritaskan revenge target dulu
        target = None
        if _revenge_target:
            for e in enemies_here:
                if e.get("id") == _revenge_target:
                    target = e
                    break
        
        # Then hunting target
        if not target and _hunting_target:
            for e in enemies_here:
                if e.get("id") == _hunting_target.get("id"):
                    target = e
                    break
        
        # Then lowest HP enemy (execute priority)
        if not target:
            target = min(enemies_here, key=lambda e: e.get("hp", 999))
        
        if target:
            enemy_hp = target.get("hp", 100)
            w_range = get_weapon_range(equipped)
            
            if _is_in_range(target, region_id, w_range, connections):
                log.warning(f"⚔️ ATTACK {target.get('id', '?')[:8]} (HP={enemy_hp}) MyHP={hp} DMG={my_damage}")
                
                if not _hunting_target:
                    _hunting_target = target
                    _hunting_timer = BERSERKER_CONFIG["TARGET_MARK_DURATION"]
                
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"ATTACK"}

    # ================================================================
    # PRIORITY 4: EQUIP BEST WEAPON
    # ================================================================
    equip_action = _check_equip(inventory, equipped)
    if equip_action:
        return equip_action

    # ================================================================
    # PRIORITY 5: MOVE TOWARD ENEMY (PREDATOR MOVEMENT)
    # ================================================================
    if ep >= move_ep_cost and connections:
        # Cari musuh di region lain
        enemies_elsewhere = []
        for agent in visible_agents:
            if agent.get("id") != my_id and agent.get("regionId") != region_id:
                if not agent.get("isGuardian", False):
                    enemies_elsewhere.append(agent)
        
        if enemies_elsewhere:
            # Prioritaskan revenge target jika ada
            target_enemy = None
            if _revenge_target:
                for e in enemies_elsewhere:
                    if e.get("id") == _revenge_target:
                        target_enemy = e
                        break
            
            if not target_enemy and _hunting_target:
                for e in enemies_elsewhere:
                    if e.get("id") == _hunting_target.get("id"):
                        target_enemy = e
                        break
            
            if not target_enemy:
                target_enemy = min(enemies_elsewhere, key=lambda e: e.get("hp", 999))
            
            if target_enemy:
                target_region = target_enemy.get("regionId")
                if target_region and target_region not in danger_ids:
                    log.warning(f"🎯 MOVE TO ENEMY: {target_region} (HP={target_enemy.get('hp', 100)})")
                    return {"action": "move", "data": {"regionId": target_region}, "reason": "SEEK_ENEMY"}
        
        # Move to any connected region (explore)
        for conn in connections:
            if isinstance(conn, dict):
                rid = conn.get("id", "")
                if rid and not conn.get("isDeathZone") and rid != region_id:
                    log.warning(f"🚶 MOVE to {rid}")
                    return {"action": "move", "data": {"regionId": rid}, "reason": "EXPLORE"}
            elif isinstance(conn, str):
                if conn and conn != region_id:
                    log.warning(f"🚶 MOVE to {conn}")
                    return {"action": "move", "data": {"regionId": conn}, "reason": "EXPLORE"}

    # ================================================================
    # PRIORITY 6: PICKUP ITEMS
    # ================================================================
    if visible_items:
        # Buat self_data sementara untuk pickup
        temp_self = {"hp": hp}
        pickup_action = _smart_pickup(visible_items, inventory, region_id, equipped)
        if pickup_action:
            return pickup_action

    # ================================================================
    # PRIORITY 7: FARM MONSTERS (if no enemies)
    # ================================================================
    if monsters and not enemies_here and ep >= 1:
        target = _select_weakest(monsters)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            log.warning(f"🐾 FARM MONSTER: HP={target.get('hp', 0)}")
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "monster"},
                    "reason": "FARM"}

    # ================================================================
    # PRIORITY 8: REST ONLY IF EP IS ZERO
    # ================================================================
    if ep < 2 and not enemies_here:
        log.warning(f"😴 REST: EP={ep}/{max_ep}")
        return {"action": "rest", "data": {}, "reason": "REST"}

    # ================================================================
    # DEFAULT: NO ACTION YET
    # ================================================================
    log.warning(f"⚠️ No action determined - HP={hp} EP={ep} enemies={len(enemies_here)}")
    return None
