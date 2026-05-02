"""
Strategy brain v1.8.1 — threat‑aware, opportunistic combat & refined survival.
Based on v1.8.0 with requested fixes and improvements.

Changes:
- Fixed missing closing brace in Priority 8 facility interaction
- Desperate flee now fights back or rests instead of doing nothing when EP=0
- Binoculars harassment now requires favorable damage comparison
- Aggressive EP threshold raised to max_ep - 4
- Threat avoidance penalty disabled in endgame (alive_count <= 10)
- Cached w_range in Priority 6b
"""

from bot.utils.logger import get_logger

log = get_logger(__name__)

# ── Weapon stats from combat-items.md ─────────────────────────────────
WEAPONS = {
    "fist":   {"bonus": 0,  "range": 0},
    "dagger": {"bonus": 10, "range": 0},
    "sword":  {"bonus": 20, "range": 0},
    "katana": {"bonus": 35, "range": 0},
    "bow":    {"bonus": 5,  "range": 1},
    "pistol": {"bonus": 10, "range": 1},
    "sniper": {"bonus": 28, "range": 2},
}

WEAPON_PRIORITY = ["katana", "sniper", "sword", "pistol", "dagger", "bow", "fist"]

# ── Item priority for pickup ──────────────────────────────────────────
ITEM_PRIORITY = {
    "rewards":         300,
    "katana":          100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger":          80,  "bow": 75,
    "medkit":          70,  "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars":      55,
    "map":             52,
    "megaphone":       40,
}

# ── Item value for drop decisions ─────────────────────────────────────
ITEM_DROP_VALUE = {
    "rewards":         -1,
    "katana":          10, "sniper": 9.5, "sword": 9, "pistol": 8.5,
    "dagger":          8,  "bow": 7.5,
    "medkit":          7,  "bandage": 6.5, "emergency_food": 6, "energy_drink": 5.8,
    "binoculars":      5.5,
    "map":             5.2,
    "megaphone":       4,
    "fist":            0,
}

# ── Recovery items ────────────────────────────────────────────────────
RECOVERY_ITEMS = {
    "medkit":          50, "bandage": 30, "emergency_food": 20,
    "energy_drink":    0,
}

WEATHER_COMBAT_PENALTY = {
    "clear": 0.0,
    "rain":  0.05,
    "fog":   0.10,
    "storm": 0.15,
}

# ── Tuning constants ──────────────────────────────────────────────────
AGENT_STALE_TICKS   = 20
MEGAPHONE_MIN_EP    = 4
THREAT_WEIGHT       = 0.8
VULNERABLE_TTL      = 2         # ticks a vulnerable mark lasts

# ── Global state ──────────────────────────────────────────────────────
_game_id: str = None
_tick_counter: int = 0

_known_agents: dict = {}
_map_knowledge: dict = {
    "revealed": False,
    "death_zones": set(),
    "safe_center": []
}
_combat_history: dict = {
    "last_hp": 100,
    "consecutive_damage_ticks": 0,
    "last_attacker_id": "",
    "damage_this_tick": False,
}
_explored_regions: set = set()
_map_used_this_tick: bool = False
_map_item_used_ids: set = set()

_vulnerable_agents: dict = {}   # id → last_tick_seen_vulnerable
_visible_region_cache: dict[str, dict] = {}  # region_id → region object


# ── Damage calculation ────────────────────────────────────────────────

def calc_damage(atk: int, weapon_bonus: int, target_def: int,
                weather: str = "clear") -> int:
    base = atk + weapon_bonus - int(target_def * 0.5)
    penalty = WEATHER_COMBAT_PENALTY.get(weather, 0.0)
    return max(1, int(base * (1 - penalty)))


def get_weapon_bonus(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("bonus", 0)


def get_weapon_range(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("range", 0)


# ── Region helpers ────────────────────────────────────────────────────

def _resolve_region(entry, view: dict):
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        for r in view.get("visibleRegions", []):
            if isinstance(r, dict) and r.get("id") == entry:
                return r
    return None


def _get_region_id(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("id", "")
    return ""


def reset_game_state():
    global _known_agents, _map_knowledge, _combat_history, _explored_regions
    global _map_used_this_tick, _map_item_used_ids, _tick_counter
    global _vulnerable_agents, _visible_region_cache
    _tick_counter = 0
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _combat_history = {
        "last_hp": 100,
        "consecutive_damage_ticks": 0,
        "last_attacker_id": "",
        "damage_this_tick": False,
    }
    _explored_regions = set()
    _map_used_this_tick = False
    _map_item_used_ids = set()
    _vulnerable_agents = {}
    _visible_region_cache = {}
    log.info("Strategy brain reset for new game (v1.8.1)")


# ── Main decision engine ──────────────────────────────────────────────

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine v1.8.1.

    Priority chain:
    1.  DEATHZONE ESCAPE
    1b. Pre-escape pending DZ
    1c. DESPERATE FLEE (HP<20, no heals, enemies) – with EP‑gated move, fightback, or rest
    1d. COUNTER-ATTACK (retaliate if just damaged)
    2.  [DISABLED] Curse
    2b. Guardian threat evasion
    3.  Free actions: pickup, equip
    3b. Utility items (Map dedup, Megaphone lure)
    [cooldown gate]
    4.  Critical healing (HP<25)
    4b. Proactive EP management (aggressive if many energy drinks)
    4c. Binoculars ranged harassment (with damage comparison)
    5.  Guardian farming
    6.  Endgame hunt (alive_count ≤ 10)
    6b. Favorable agent combat (vulnerable bonus, cached w_range)
    7.  Monster farming
    7b. Opportunistic / camping heal
    8.  Facility interaction (fixed missing brace)
    9.  Strategic movement (threat avoidance disabled in endgame)
    10. Rest
    """

    global _game_id, _map_used_this_tick, _explored_regions, _tick_counter
    global _vulnerable_agents, _visible_region_cache, _map_item_used_ids

    # ── Auto-reset on game change ───────────────────────────────────
    new_game_id = view.get("gameId", "")
    if new_game_id and new_game_id != _game_id:
        reset_game_state()
        _game_id = new_game_id

    _tick_counter += 1

    self_data        = view.get("self", {})
    region           = view.get("currentRegion", {})
    hp               = self_data.get("hp", 100)
    ep               = self_data.get("ep", 10)
    max_ep           = self_data.get("maxEp", 10)
    atk              = self_data.get("atk", 10)
    defense          = self_data.get("def", 5)
    is_alive         = self_data.get("isAlive", True)
    inventory        = self_data.get("inventory", [])
    equipped         = self_data.get("equippedWeapon")
    my_id            = self_data.get("id", "")

    visible_agents   = view.get("visibleAgents", [])
    visible_monsters = view.get("visibleMonsters", [])
    visible_items_raw= view.get("visibleItems", [])
    visible_regions  = view.get("visibleRegions", [])
    connected_regions= view.get("connectedRegions", [])
    pending_dz       = view.get("pendingDeathzones", [])
    alive_count      = view.get("aliveCount", 100)
    recent_logs      = view.get("recentLogs", [])

    # Cache visible regions for later lookup
    _visible_region_cache = {}
    for r in visible_regions:
        if isinstance(r, dict) and r.get("id"):
            _visible_region_cache[r["id"]] = r

    # Unwrap visibleItems
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

    connections    = connected_regions or region.get("connections", [])
    interactables  = region.get("interactables", [])
    region_id      = region.get("id", "")
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""

    if not is_alive:
        return None

    # ── Mark current region as explored ────────────────────────────
    if region_id:
        _explored_regions.add(region_id)

    # ── Trigger learn_from_map if Map was used last tick ───────────
    if _map_used_this_tick:
        _map_used_this_tick = False
        learn_from_map(view)

    # ── Cleanup map item ids not in inventory anymore ─────────────
    current_item_ids = {i.get("id", "") for i in inventory if isinstance(i, dict)}
    _map_item_used_ids.intersection_update(current_item_ids)

    # ── Build danger map ──────────────────────────────────────────
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

    # Track visible agents (with staleness tick)
    _track_agents(visible_agents, my_id, region_id)

    # ── Detect damage + identify vulnerable agents from logs ──────
    _update_combat_history(hp, recent_logs, my_id)
    _detect_vulnerable_agents(recent_logs, my_id)

    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # ── Priority 1: DEATHZONE ESCAPE ──────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 IN DEATH ZONE! Escaping to %s (HP=%d)", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In death zone! HP={hp}"}
        elif not safe:
            log.error("🚨 DZ but NO SAFE REGION!")

    # ── Priority 1b: Pre-escape pending DZ ────────────────────────
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("⚠️ Region becoming DZ! Pre-escaping to %s", safe)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region becoming DZ"}

    # ── Build lists of enemies (PvP) and guardians ────────────────
    enemies_alive = [a for a in visible_agents
                     if not a.get("isGuardian", False) and a.get("isAlive", True)
                     and a.get("id") != my_id]
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]

    # ── Priority 1c: DESPERATE FLEE (HP<20, no heals, enemies) ───
    has_healing_items = any(
        isinstance(i, dict) and i.get("typeId", "").lower() in RECOVERY_ITEMS
        and RECOVERY_ITEMS[i.get("typeId", "").lower()] > 0
        for i in inventory
    )
    if hp < 20 and not has_healing_items and (enemies_alive or guardians_here):
        if ep >= move_ep_cost:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                log.warning("🆘 DESPERATE FLEE! HP=%d", hp)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"DESPERATE FLEE: HP={hp}"}
        else:
            # Not enough EP to flee – try to attack nearest enemy if possible, else rest
            w_range = get_weapon_range(equipped)
            nearest = None
            if enemies_alive:
                nearest = enemies_alive[0]
            elif guardians_here:
                nearest = guardians_here[0]
            if nearest and _is_in_range(nearest, region_id, w_range, connections):
                my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                     nearest.get("def", 5), region_weather)
                log.warning("🆘 DESPERATE ATTACK on %s (dmg=%d)",
                            nearest.get("id", "?")[:8], my_dmg)
                return {"action": "attack",
                        "data": {"targetId": nearest["id"], "targetType": "agent"},
                        "reason": "DESPERATE ATTACK: no EP to flee"}
            # If cannot attack, attempt to rest (even if enemies present – last resort)
            if not region.get("isDeathZone") and region_id not in danger_ids:
                log.warning("🆘 DESPERATE REST (HP=%d, no other option)", hp)
                return {"action": "rest", "data": {},
                        "reason": "DESPERATE REST: no EP, no attack possible"}

    # ── Priority 1d: COUNTER-ATTACK ───────────────────────────────
    if _combat_history.get("damage_this_tick"):
        attacker_id = _combat_history["last_attacker_id"]
        if attacker_id == "unknown" or not attacker_id:
            if enemies_alive:
                attacker = enemies_alive[0]
            elif guardians_here:
                attacker = guardians_here[0]
            else:
                attacker = None
        else:
            attacker = None
            for agent in visible_agents:
                if agent.get("id") == attacker_id and agent.get("isAlive", True):
                    attacker = agent
                    break

        if attacker:
            w_range = get_weapon_range(equipped)
            if _is_in_range(attacker, region_id, w_range, connections):
                my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                     attacker.get("def", 5), region_weather)
                log.warning("⚔️ COUNTER-ATTACK! vs %s (dmg=%d)",
                            attacker.get("id", "unknown")[:8], my_dmg)
                _combat_history["damage_this_tick"] = False
                return {"action": "attack",
                        "data": {"targetId": attacker["id"], "targetType": "agent"},
                        "reason": "COUNTER-ATTACK: Just damaged"}
            else:
                if ep >= move_ep_cost:
                    move = _move_toward_target(attacker, connections, danger_ids, view)
                    if move:
                        log.info("🏃 CHASING attacker to %s", move[:8])
                        _combat_history["damage_this_tick"] = False
                        return {"action": "move", "data": {"regionId": move},
                                "reason": "CHASE: Pursuing attacker"}

    # ── Priority 2b: Guardian threat evasion ──────────────────────
    if guardians_here and ep >= move_ep_cost:
        threat_guardian = max(guardians_here, key=lambda g: g.get("atk", 10))
        g_dmg = calc_damage(threat_guardian.get("atk", 10),
                            _estimate_enemy_weapon_bonus(threat_guardian),
                            defense, region_weather)
        flee_hp_threshold = max(25, int(g_dmg * 1.5))
        if hp < flee_hp_threshold:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                log.warning("⚠️ Guardian threat! HP=%d, fleeing", hp)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"GUARDIAN FLEE: HP={hp} < {flee_hp_threshold}"}

    # ── Priority 3: FREE ACTIONS (pickup + equip) ─────────────────
    pickup_action = _check_pickup(visible_items, inventory, region_id)
    if pickup_action:
        return pickup_action

    equip_action = _check_equip(inventory, equipped)
    if equip_action:
        return equip_action

    # ── Priority 3b: Utility items (Map, Megaphone) ───────────────
    util_action = _use_utility_item(inventory, hp, ep, alive_count)
    if util_action:
        if util_action.get("data", {}).get("itemType") == "map":
            _map_used_this_tick = True
        return util_action

    # ── Cooldown gate ─────────────────────────────────────────────
    if not can_act:
        return None

    # ── Priority 4: Critical healing (HP < 25) ────────────────────
    if hp < 25:
        heal = _find_healing_item(inventory, critical=True)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL: HP={hp}"}
    elif hp < 60:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}"}

    # ── Priority 4b: Proactive EP management (aggressive) ────────
    energy_drink_count = sum(1 for i in inventory
                             if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink")
    # Standard threshold: EP ≤ 2 and not full
    if ep <= 2 and ep < max_ep:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]},
                    "reason": f"EP RECOVERY: EP={ep}/{max_ep}"}
    # Aggressive threshold: if we have ≥2 drinks, use when EP ≤ max_ep - 4
    elif energy_drink_count >= 2 and ep <= max_ep - 4:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]},
                    "reason": f"AGGRESSIVE EP RECOVERY: EP={ep}/{max_ep}, {energy_drink_count} drinks"}

    # ── Priority 4c: Binoculars + ranged harassment (with dmg check) ─
    has_binos = any(isinstance(i, dict) and i.get("typeId", "").lower() == "binoculars"
                    for i in inventory)
    w_range = get_weapon_range(equipped)
    if has_binos and w_range >= 1 and enemies_alive and hp >= 30:
        # Find an enemy in range that we can profitably hit
        for enemy in enemies_alive:
            if _is_in_range(enemy, region_id, w_range, connections):
                my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                     enemy.get("def", 5), region_weather)
                e_dmg  = calc_damage(enemy.get("atk", 10),
                                     _estimate_enemy_weapon_bonus(enemy),
                                     defense, region_weather)
                # Only fire if our damage is higher or enemy is near death
                if my_dmg > e_dmg or enemy.get("hp", 100) <= my_dmg * 2:
                    log.info("🔭 Binoculars ranged attack on %s (dmg=%d)", enemy.get("id", "")[:8], my_dmg)
                    return {"action": "attack",
                            "data": {"targetId": enemy["id"], "targetType": "agent"},
                            "reason": "BINOCULARS RANGED HARASSMENT"}
                break  # only check first eligible; we don't want to loop through all

    # ── Priority 5: Guardian farming ──────────────────────────────
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians and ep >= 2 and hp >= 25:
        target = _select_best_combat_target(guardians, atk, equipped, defense, region_weather)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            g_dmg  = calc_damage(target.get("atk", 10),
                                 _estimate_enemy_weapon_bonus(target),
                                 defense, region_weather)
            if my_dmg >= g_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: dmg={my_dmg}"}
        else:
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost and hp >= 35:
                return {"action": "move", "data": {"regionId": move},
                        "reason": "APPROACH GUARDIAN"}

    # ── Priority 6: Endgame hunt mode ─────────────────────────────
    if alive_count <= 10 and enemies_alive and ep >= 2 and hp >= 20:
        target = _select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather)
        if _is_in_range(target, region_id, w_range, connections):
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "agent"},
                    "reason": f"ENDGAME HUNT: alive={alive_count}"}
        else:
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost:
                return {"action": "move", "data": {"regionId": move},
                        "reason": "ENDGAME CHASE"}

    # ── Priority 6b: Favorable agent combat (vulnerable bonus, cached w_range) ─
    hp_threshold = 25 if alive_count > 20 else 20
    if enemies_alive and ep >= 2 and hp >= hp_threshold:
        target = _select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather)
        # w_range already computed above, reuse it
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            e_dmg  = calc_damage(target.get("atk", 10),
                                 _estimate_enemy_weapon_bonus(target),
                                 defense, region_weather)
            if my_dmg > e_dmg or target.get("hp", 100) <= my_dmg * 2:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"COMBAT: dmg={my_dmg} vs {e_dmg}"}
            elif w_range >= 1 and my_dmg >= e_dmg * 0.7:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"RANGED ATTACK: range={w_range}"}

    # ── Priority 7: Monster farming ────────────────────────────────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2 and hp >= 20:
        target = _select_best_combat_target(monsters, atk, equipped, defense, region_weather)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            m_dmg  = calc_damage(target.get("atk", 10), 0, defense, region_weather)
            if my_dmg >= m_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "monster"},
                        "reason": "MONSTER FARM"}

    # ── Priority 7b: Opportunistic / camping heal ──────────────────
    if hp < 75 and not enemies_alive and not guardians_here:
        heal = _find_healing_item(inventory, critical=(hp < 25))
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, area safe"}
    elif hp < 100 and not enemies_alive and not guardians_here and alive_count <= 10:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CAMPING HEAL: HP={hp}, endgame safe"}

    # ── Priority 8: Facility interaction ──────────────────────────
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep, alive_count)
        if facility:
            return {"action": "interact",
                    "data": {"interactableId": facility["id"]},
                    "reason": f"FACILITY: {facility.get('type')}"}

    # ── Priority 9: Strategic movement (threat avoidance) ─────────
    if ep >= move_ep_cost and connections:
        move_target = _choose_move_target(connections, danger_ids,
                                          region, visible_items, alive_count,
                                          enemies_alive, ep)
        if move_target:
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "EXPLORE"}

    # ── Priority 10: Rest ─────────────────────────────────────────
    if ep < 3 and not enemies_alive and not guardians_here \
            and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}"}

    return None


# ── Helper functions (updated / new for v1.8.1) ──────────────────────

def _detect_vulnerable_agents(recent_logs: list, my_id: str):
    """Detect enemies that just used healing items and mark them vulnerable."""
    global _vulnerable_agents
    _vulnerable_agents = {k: v for k, v in _vulnerable_agents.items()
                          if _tick_counter - v < VULNERABLE_TTL}
    for entry in recent_logs:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "use_item":
            user_id = entry.get("agentId") or entry.get("userId", "")
            item_name = entry.get("itemName", "").lower()
            if any(heal in item_name for heal in ("medkit", "bandage", "food", "emergency")):
                if user_id and user_id != my_id:
                    _vulnerable_agents[user_id] = _tick_counter
                    log.info("🎯 Marked %s as vulnerable (healed)", user_id[:8])


def _update_combat_history(current_hp: int, recent_logs: list, my_id: str):
    global _combat_history
    last = _combat_history.get("last_hp", current_hp)
    if current_hp < last:
        _combat_history["consecutive_damage_ticks"] += 1
        _combat_history["damage_this_tick"] = True
        attacker_found = False
        for log_entry in recent_logs:
            if not isinstance(log_entry, dict):
                continue
            if log_entry.get("type") in ("damage", "attack") or \
               "damage" in str(log_entry.get("message", "")).lower():
                attacker_id = log_entry.get("attackerId") or log_entry.get("sourceId") or ""
                target_id   = log_entry.get("targetId") or ""
                if target_id == my_id and attacker_id and attacker_id != my_id:
                    _combat_history["last_attacker_id"] = attacker_id
                    attacker_found = True
                    log.info("🩸 DAMAGE from %s — HP: %d → %d",
                             attacker_id[:8], last, current_hp)
                    break
        if not attacker_found:
            _combat_history["last_attacker_id"] = "unknown"
            log.info("🩸 DAMAGE detected but attacker unknown")
    else:
        _combat_history["consecutive_damage_ticks"] = 0
        _combat_history["damage_this_tick"] = False
    _combat_history["last_hp"] = current_hp


def _get_move_ep_cost(terrain: str, weather: str) -> int:
    if terrain == "water" or weather == "storm":
        return 3
    return 2


def _estimate_enemy_weapon_bonus(agent: dict) -> int:
    weapon = agent.get("equippedWeapon")
    if not weapon:
        return 0
    type_id = weapon.get("typeId", "").lower() if isinstance(weapon, dict) else ""
    return WEAPONS.get(type_id, {}).get("bonus", 0)


def _select_best_combat_target(targets: list, my_atk: int, equipped,
                                my_def: int, weather: str) -> dict:
    """
    Score = kill_speed - (threat * THREAT_WEIGHT) + vulnerable_bonus.
    Vulnerable agents get a flat +30 score bonus.
    """
    global _vulnerable_agents
    best = None
    best_score = -9999
    my_bonus = get_weapon_bonus(equipped)
    for t in targets:
        if not isinstance(t, dict):
            continue
        t_id    = t.get("id", "")
        t_hp    = max(t.get("hp", 100), 1)
        t_def   = t.get("def", 5)
        t_atk   = t.get("atk", 10)
        t_bonus = _estimate_enemy_weapon_bonus(t)
        my_dmg    = calc_damage(my_atk, my_bonus, t_def, weather)
        their_dmg = calc_damage(t_atk, t_bonus, my_def, weather)
        kill_speed = (my_dmg / t_hp) * 100
        threat     = their_dmg * THREAT_WEIGHT
        score      = kill_speed - threat
        if t_id in _vulnerable_agents:
            score += 30
        if score > best_score:
            best_score = score
            best = t
    return best if best else min(targets, key=lambda t: t.get("hp", 999))


def _track_agents(visible_agents: list, my_id: str, my_region: str):
    global _known_agents
    for agent in visible_agents:
        if not isinstance(agent, dict):
            continue
        aid = agent.get("id", "")
        if not aid or aid == my_id:
            continue
        _known_agents[aid] = {
            "hp":             agent.get("hp", 100),
            "atk":            agent.get("atk", 10),
            "def":            agent.get("def", 5),
            "isGuardian":     agent.get("isGuardian", False),
            "equippedWeapon": agent.get("equippedWeapon"),
            "lastSeen":       my_region,
            "isAlive":        agent.get("isAlive", True),
            "lastSeenTick":   _tick_counter,
        }
    stale_cutoff = _tick_counter - AGENT_STALE_TICKS
    _known_agents = {
        k: v for k, v in _known_agents.items()
        if v.get("isAlive", True) and v.get("lastSeenTick", 0) >= stale_cutoff
    }


def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    global _map_item_used_ids
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
        if type_id == "map":
            item_id = item.get("id", "")
            if item_id and item_id in _map_item_used_ids:
                continue
            log.info("🗺️ Using Map! (id=%s)", item_id[:8] if item_id else "?")
            _map_item_used_ids.add(item_id)
            return {"action": "use_item",
                    "data": {"itemId": item_id, "itemType": "map"},
                    "reason": "UTILITY: Using Map"}
    if alive_count <= 5 and hp > 50 and ep >= MEGAPHONE_MIN_EP:
        for item in inventory:
            if not isinstance(item, dict):
                continue
            if item.get("typeId", "").lower() == "megaphone":
                log.info("📢 Megaphone lure (alive=%d, EP=%d)", alive_count, ep)
                return {"action": "use_item",
                        "data": {"itemId": item["id"], "itemType": "megaphone"},
                        "reason": "UTILITY: Megaphone lure"}
    return None


def learn_from_map(view: dict):
    global _map_knowledge
    visible_regions = view.get("visibleRegions", [])
    if not visible_regions:
        return
    _map_knowledge["revealed"] = True
    safe_regions = []
    for region in visible_regions:
        if not isinstance(region, dict):
            continue
        rid = region.get("id", "")
        if not rid:
            continue
        if region.get("isDeathZone"):
            _map_knowledge["death_zones"].add(rid)
        else:
            conns = region.get("connections", [])
            terrain = region.get("terrain", "").lower()
            terrain_value = {
                "hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1
            }.get(terrain, 0)
            score = len(conns) + terrain_value
            safe_regions.append((rid, score))
    safe_regions.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]
    log.info("🗺️ MAP LEARNED: %d DZ, top center: %s",
             len(_map_knowledge["death_zones"]), _map_knowledge["safe_center"][:3])


def _check_pickup(items: list, inventory: list, region_id: str) -> dict | None:
    local_items = [i for i in items if isinstance(i, dict) and i.get("id")
                   and i.get("regionId") == region_id]
    if not local_items:
        local_items = [i for i in items if isinstance(i, dict) and i.get("id")]
    if not local_items:
        return None
    heal_count = sum(1 for i in inventory if isinstance(i, dict)
                     and i.get("typeId", "").lower() in RECOVERY_ITEMS
                     and RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0) > 0)
    local_items.sort(key=lambda i: _pickup_score(i, inventory, heal_count), reverse=True)
    best  = local_items[0]
    score = _pickup_score(best, inventory, heal_count)
    if score <= 0:
        return None
    type_id = best.get("typeId", "item")
    if len(inventory) >= 10:
        drop = _find_droppable_item(inventory, best)
        if drop:
            log.info("INVENTORY FULL: dropping %s for %s", drop.get("typeId"), type_id)
            return {"action": "drop_item", "data": {"itemId": drop["id"]},
                    "reason": "MAKE ROOM"}
        return None
    log.info("PICKUP: %s", type_id)
    return {"action": "pickup", "data": {"itemId": best["id"]},
            "reason": f"PICKUP: {type_id}"}


def _pickup_score(item: dict, inventory: list, heal_count: int) -> int:
    type_id  = item.get("typeId", "").lower()
    category = item.get("category", "").lower()
    if type_id == "rewards" or category == "currency":
        return 300
    if category == "weapon":
        bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
        current_best = 0
        for inv_item in inventory:
            if isinstance(inv_item, dict) and inv_item.get("category") == "weapon":
                cb = WEAPONS.get(inv_item.get("typeId", "").lower(), {}).get("bonus", 0)
                current_best = max(current_best, cb)
        if bonus > current_best:
            return 100 + bonus
        return 0
    if type_id == "binoculars":
        has_binos = any(isinstance(i, dict) and i.get("typeId", "").lower() == "binoculars"
                        for i in inventory)
        return 55 if not has_binos else 0
    if type_id == "map":
        return 52
    if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
        return ITEM_PRIORITY.get(type_id, 0) + (10 if heal_count < 4 else 0)
    if type_id == "energy_drink":
        return 58
    return ITEM_PRIORITY.get(type_id, 0)


def _find_droppable_item(inventory: list, target_item: dict) -> dict | None:
    target_score = ITEM_DROP_VALUE.get(target_item.get("typeId", "").lower(), 1)
    candidates = []
    for item in inventory:
        if not isinstance(item, dict):
            continue
        tid = item.get("typeId", "").lower()
        cat = item.get("category", "").lower()
        if cat == "currency" or tid == "rewards":
            continue
        drop_val = ITEM_DROP_VALUE.get(tid, 1)
        if drop_val < target_score:
            candidates.append((item, drop_val))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def _check_equip(inventory: list, equipped) -> dict | None:
    current_bonus = get_weapon_bonus(equipped) if equipped else 0
    best = None
    best_bonus = current_bonus
    for item in inventory:
        if not isinstance(item, dict):
            continue
        if item.get("category") == "weapon":
            type_id = item.get("typeId", "").lower()
            bonus   = WEAPONS.get(type_id, {}).get("bonus", 0)
            if bonus > best_bonus:
                best = item
                best_bonus = bonus
    if best:
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"EQUIP: {best.get('typeId','weapon')} (+{best_bonus})"}
    return None


def _find_safe_region(connections, danger_ids: set, view: dict = None) -> str | None:
    safe_regions = []
    for conn in connections:
        if isinstance(conn, str):
            if conn not in danger_ids:
                safe_regions.append((conn, 0))
        elif isinstance(conn, dict):
            rid   = conn.get("id", "")
            is_dz = conn.get("isDeathZone", False)
            if rid and not is_dz and rid not in danger_ids:
                terrain = conn.get("terrain", "").lower()
                score   = {"hills": 3, "plains": 2, "ruins": 1, "forest": 0, "water": -2}.get(terrain, 0)
                safe_regions.append((rid, score))
    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]
    for conn in connections:
        rid   = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            log.warning("No fully safe region! Using fallback: %s", rid[:8])
            return rid
    return None


def _find_healing_item(inventory: list, critical: bool = False) -> dict | None:
    heals = [i for i in inventory
             if isinstance(i, dict)
             and i.get("typeId", "").lower() in RECOVERY_ITEMS
             and RECOVERY_ITEMS[i.get("typeId", "").lower()] > 0]
    if not heals:
        return None
    heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0),
               reverse=critical)
    return heals[0]


def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None


def _select_facility(interactables: list, hp: int, ep: int, alive_count: int) -> dict | None:
    best = None
    best_priority = -1
    for fac in interactables:
        if not isinstance(fac, dict) or fac.get("isUsed"):
            continue
        ftype    = fac.get("type", "").lower()
        priority = -1
        if ftype == "medical_facility" and hp < 80:
            priority = 10
        elif ftype == "watchtower" and alive_count > 15:
            priority = 8
        elif ftype == "supply_cache":
            priority = 7
        elif ftype == "broadcast_station" and alive_count <= 5:
            priority = 5
        elif ftype == "broadcast_station":
            continue
        if priority > best_priority:
            best = fac
            best_priority = priority
    return best


def _is_in_range(target: dict, my_region: str, weapon_range: int,
                  connections=None) -> bool:
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


def _move_toward_target(target: dict, connections, danger_ids: set,
                         view: dict) -> str | None:
    target_region = target.get("regionId", "")
    if not target_region:
        return None
    safe_conn_ids = []
    for conn in connections:
        rid   = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and rid not in danger_ids and not is_dz:
            safe_conn_ids.append(rid)
    if target_region in safe_conn_ids:
        return target_region
    # 2-hop BFS using visible region cache
    best_hop = None
    for step1_id in safe_conn_ids:
        step1_region = _visible_region_cache.get(step1_id)
        if not step1_region:
            continue
        step1_conns = step1_region.get("connections", [])
        for step2 in step1_conns:
            step2_id = step2 if isinstance(step2, str) else step2.get("id", "")
            if step2_id == target_region:
                if best_hop is None or step1_id not in _explored_regions:
                    best_hop = step1_id
                break
    if best_hop:
        log.info("🧭 2-hop path: %s → %s → target", best_hop[:8], target_region[:8])
        return best_hop
    # Fallback: any safe neighbor
    for rid in safe_conn_ids:
        return rid
    return None


def _choose_move_target(connections, danger_ids: set, current_region: dict,
                         visible_items: list, alive_count: int,
                         enemies_visible: list = None, current_ep: int = 999) -> str | None:
    global _explored_regions, _known_agents
    candidates = []
    item_regions = {item.get("regionId", "") for item in visible_items
                    if isinstance(item, dict)}
    enemy_regions = set()
    enemy_threat_map = {}
    if enemies_visible:
        for e in enemies_visible:
            reg = e.get("regionId", "")
            enemy_regions.add(reg)
            e_id = e.get("id", "")
            agent_data = _known_agents.get(e_id, {})
            e_atk = agent_data.get("atk", e.get("atk", 10))
            e_def = agent_data.get("def", e.get("def", 5))
            threat = e_atk + e_def
            if reg not in enemy_threat_map or threat > enemy_threat_map[reg]:
                enemy_threat_map[reg] = threat

    for conn in connections:
        rid = None
        conn_dict = None
        if isinstance(conn, str):
            if conn in danger_ids:
                continue
            rid = conn
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue
            conn_dict = conn
        if rid in _map_knowledge.get("death_zones", set()):
            continue
        score = 1
        if conn_dict:
            terrain = conn_dict.get("terrain", "").lower()
            weather = conn_dict.get("weather", "").lower()
            score += {"hills": 4, "plains": 2, "ruins": 2, "forest": 1, "water": -3}.get(terrain, 0)
            score += {"clear": 1, "rain": 0, "fog": -1, "storm": -2}.get(weather, 0)
            if weather == "storm" and current_ep < 4:
                score -= 100
        if rid in item_regions:
            score += 5
        if rid in enemy_regions and alive_count <= 10:
            score += 4
        # Threat avoidance: only apply if not in endgame (alive_count > 10)
        if alive_count > 10:
            threat_level = enemy_threat_map.get(rid, 0)
            if threat_level > 0:
                if threat_level > 25:
                    score -= 50
                else:
                    score -= max(0, threat_level * 2)
        if conn_dict:
            facs   = conn_dict.get("interactables", [])
            unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
            score += len(unused) * 2
        if alive_count < 30:
            score += 3
        if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
            score += 5
        if rid in _explored_regions:
            score -= 5
        candidates.append((rid, score))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]