"""
Strategy brain v1.6.2 — aggressive counter-attack + full enhancement suite.
Based on v1.6.1 with all recommended improvements implemented.

New features:
- Auto-reset on game ID change
- EP validation for energy drink
- Fallback attacker (unknown → nearest enemy)
- Avoid storm region when low EP
- learn_from_map auto-trigger
- Megaphone lure in endgame
- Explored region memory
- Camping heal in endgame safe zones
- Facility optimization (watchtower early, broadcast for lure)
- Desperate flee when HP < 20 and no healing items
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

# ── Global state ──────────────────────────────────────────────────────
_game_id: str = None                # track current game to auto-reset
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
_explored_regions: set = set()       # regions we've visited
_map_used_this_tick: bool = False   # flag to trigger learn_from_map

# ── Damage calculation ────────────────────────────────────────────────

def calc_damage(atk: int, weapon_bonus: int, target_def: int,
                weather: str = "clear") -> int:
    """Damage formula per combat-items.md + game-systems.md weather penalty."""
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
    """Reset all per-game state. Called manually or on game change."""
    global _known_agents, _map_knowledge, _combat_history, _explored_regions, _map_used_this_tick
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
    log.info("Strategy brain reset for new game (v1.6.2)")


# ── Main decision engine ──────────────────────────────────────────────

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine v1.6.2 — full enhancement suite.

    Priority chain (revised):
    1.  DEATHZONE ESCAPE (overrides everything)
    1b. Pre-escape pending DZ
    1c. DESPERATE FLEE (HP < 20, no healing, enemies near)
    1d. COUNTER-ATTACK (retaliate if just damaged)
    2.  [DISABLED] Curse resolution
    2b. Guardian threat evasion (lowered threshold)
    3.  Free actions: pickup, equip
    3b. Use utility items (Map, Megaphone)
    [cooldown gate]
    4.  Critical healing (HP < 25)
    4b. Proactive EP management (energy drink if EP < 3 and not full)
    5.  Guardian farming (lowered HP threshold)
    6.  Endgame hunt mode (alive_count < 10)
    6b. Favorable agent combat (lowered HP threshold)
    7.  Monster farming
    7b. Opportunistic heal / camping heal (safe area, HP < 100 if endgame)
    8.  Facility interaction (optimized)
    9.  Strategic movement (storm avoidance, explored region memory)
    10. Rest (EP < 3, truly safe)
    """

    global _game_id, _map_used_this_tick, _explored_regions

    # ── Auto-reset on game change ───────────────────────────────────
    new_game_id = view.get("gameId", "")
    if new_game_id and new_game_id != _game_id:
        reset_game_state()
        _game_id = new_game_id

    self_data       = view.get("self", {})
    region          = view.get("currentRegion", {})
    hp              = self_data.get("hp", 100)
    ep              = self_data.get("ep", 10)
    max_ep          = self_data.get("maxEp", 10)
    atk             = self_data.get("atk", 10)
    defense         = self_data.get("def", 5)
    is_alive        = self_data.get("isAlive", True)
    inventory       = self_data.get("inventory", [])
    equipped        = self_data.get("equippedWeapon")
    my_id           = self_data.get("id", "")

    visible_agents   = view.get("visibleAgents", [])
    visible_monsters = view.get("visibleMonsters", [])
    visible_items_raw= view.get("visibleItems", [])
    visible_regions  = view.get("visibleRegions", [])
    connected_regions= view.get("connectedRegions", [])
    pending_dz       = view.get("pendingDeathzones", [])
    alive_count      = view.get("aliveCount", 100)
    recent_logs      = view.get("recentLogs", [])

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

    connections   = connected_regions or region.get("connections", [])
    interactables = region.get("interactables", [])
    region_id     = region.get("id", "")
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

    # ── Build danger map ───────────────────────────────────────────
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

    # Track visible agents
    _track_agents(visible_agents, my_id, region_id)

    # ── Detect incoming damage + attacker ──────────────────────────
    _update_combat_history(hp, recent_logs, my_id)

    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # ── Priority 1: DEATHZONE ESCAPE ───────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 IN DEATH ZONE! Escaping to %s (HP=%d)", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In death zone! HP={hp}"}
        elif not safe:
            log.error("🚨 DZ but NO SAFE REGION!")

    # ── Priority 1b: Pre-escape pending DZ ─────────────────────────
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("⚠️ Region becoming DZ! Pre-escaping to %s", safe)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region becoming DZ"}

    # ── Build lists of enemies (PvP) and guardians here ────────────
    enemies_alive = [a for a in visible_agents
                     if not a.get("isGuardian", False) and a.get("isAlive", True)
                     and a.get("id") != my_id]
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]

    # ── Priority 1c: DESPERATE FLEE (HP < 20, no healing, enemies) ──
    has_healing_items = any(
        isinstance(i, dict) and i.get("typeId", "").lower() in RECOVERY_ITEMS
        and RECOVERY_ITEMS[i.get("typeId", "").lower()] > 0
        for i in inventory
    )
    if hp < 20 and not has_healing_items and (enemies_alive or guardians_here):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:  # try to move even without EP check (server may still allow)
            log.warning("🆘 DESPERATE FLEE! HP=%d, no heals, enemies present.", hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"DESPERATE FLEE: HP={hp}, no healing items"}

    # ── Priority 1d: COUNTER-ATTACK ────────────────────────────────
    if _combat_history.get("damage_this_tick"):
        attacker_id = _combat_history["last_attacker_id"]
        # If unknown attacker, fallback to nearest enemy
        if attacker_id == "unknown" or not attacker_id:
            # Pick closest enemy: player in same region, then guardian
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
                log.warning("⚔️ COUNTER-ATTACK! Retaliating against %s (dmg=%d)",
                            attacker.get("id", "unknown")[:8], my_dmg)
                _combat_history["damage_this_tick"] = False
                return {"action": "attack",
                        "data": {"targetId": attacker["id"], "targetType": "agent"},
                        "reason": f"COUNTER-ATTACK: Just damaged"}
            else:
                move = _move_toward_target(attacker, connections, danger_ids, view)
                if move:
                    log.info("🏃 CHASING attacker to %s", move[:8])
                    _combat_history["damage_this_tick"] = False
                    return {"action": "move", "data": {"regionId": move},
                            "reason": f"CHASE: Pursuing attacker"}

    # ── Priority 2b: Guardian threat evasion (lowered threshold) ───
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

    # ── Priority 3: FREE ACTIONS ────────────────────────────────────
    pickup_action = _check_pickup(visible_items, inventory, region_id)
    if pickup_action:
        return pickup_action

    equip_action = _check_equip(inventory, equipped)
    if equip_action:
        return equip_action

    # ── Priority 3b: Use utility items (Map, Megaphone) ─────────────
    util_action = _use_utility_item(inventory, hp, ep, alive_count)
    if util_action:
        if util_action.get("data", {}).get("itemType") == "map":
            _map_used_this_tick = True  # trigger learn_from_map next tick
        return util_action

    # ── Cooldown gate ───────────────────────────────────────────────
    if not can_act:
        return None

    # ── Priority 4: Critical healing (HP < 25) ─────────────────────
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

    # ── Priority 4b: Proactive EP management ───────────────────────
    if ep <= 2 and ep < max_ep:
        energy_drink = _find_energy_drink(inventory)
        if energy_drink:
            return {"action": "use_item", "data": {"itemId": energy_drink["id"]},
                    "reason": f"EP RECOVERY: EP={ep}/{max_ep}"}

    # ── Priority 5: Guardian farming ───────────────────────────────
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians and ep >= 2 and hp >= 25:
        target = _select_best_combat_target(guardians, atk, equipped, defense, region_weather)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            g_dmg = calc_damage(target.get("atk", 10),
                                _estimate_enemy_weapon_bonus(target),
                                defense, region_weather)
            if my_dmg >= g_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: 120 sMoltz, dmg={my_dmg}"}
        else:
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost and hp >= 35:
                return {"action": "move", "data": {"regionId": move},
                        "reason": "APPROACH GUARDIAN"}

    # ── Priority 6: Endgame hunt mode ──────────────────────────────
    if alive_count <= 10 and enemies_alive and ep >= 2 and hp >= 20:
        target = _select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "agent"},
                    "reason": f"ENDGAME HUNT: alive={alive_count}"}
        else:
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost:
                return {"action": "move", "data": {"regionId": move},
                        "reason": "ENDGAME CHASE"}

    # ── Priority 6b: Favorable agent combat ────────────────────────
    hp_threshold = 25 if alive_count > 20 else 20
    if enemies_alive and ep >= 2 and hp >= hp_threshold:
        target = _select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather)
        w_range = get_weapon_range(equipped)
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

    # ── Priority 7: Monster farming ─────────────────────────────────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2 and hp >= 20:
        target = _select_best_combat_target(monsters, atk, equipped, defense, region_weather)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            m_dmg  = calc_damage(target.get("atk", 10), 0, defense, region_weather)
            if my_dmg >= m_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "monster"},
                        "reason": f"MONSTER FARM"}

    # ── Priority 7b: Opportunistic heal / camping heal ──────────────
    # v1.6.2: if endgame and safe, heal to full even if not critical
    if hp < 75 and not enemies_alive and not guardians_here:
        heal = _find_healing_item(inventory, critical=(hp < 25))
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, area safe"}
    elif hp < 100 and not enemies_alive and not guardians_here and alive_count <= 10:
        # Camping heal: heal to full when safe in endgame
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CAMPING HEAL: HP={hp}, endgame safe"}

    # ── Priority 8: Facility interaction (optimized) ────────────────
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep, alive_count)
        if facility:
            return {"action": "interact",
                    "data": {"interactableId": facility["id"]},
                    "reason": f"FACILITY: {facility.get('type')}"}

    # ── Priority 9: Strategic movement ──────────────────────────────
    if ep >= move_ep_cost and connections:
        move_target = _choose_move_target(connections, danger_ids,
                                          region, visible_items, alive_count,
                                          enemies_alive, ep)
        if move_target:
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "EXPLORE"}

    # ── Priority 10: Rest ───────────────────────────────────────────
    if ep < 3 and not enemies_alive and not guardians_here \
            and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}"}

    return None


# ── Helper functions (updated for v1.6.2) ────────────────────────────

def _update_combat_history(current_hp: int, recent_logs: list, my_id: str):
    """Track HP changes AND identify who attacked us; fallback to 'unknown'."""
    global _combat_history
    last = _combat_history.get("last_hp", current_hp)

    if current_hp < last:
        _combat_history["consecutive_damage_ticks"] += 1
        _combat_history["damage_this_tick"] = True

        # Try to identify attacker from recentLogs
        attacker_found = False
        for log_entry in recent_logs:
            if not isinstance(log_entry, dict):
                continue
            if log_entry.get("type") in ("damage", "attack") or \
               "damage" in str(log_entry.get("message", "")).lower():
                attacker_id = log_entry.get("attackerId") or log_entry.get("sourceId") or ""
                target_id = log_entry.get("targetId") or ""
                if target_id == my_id and attacker_id and attacker_id != my_id:
                    _combat_history["last_attacker_id"] = attacker_id
                    attacker_found = True
                    log.info("🩸 DAMAGE from %s — HP: %d → %d", attacker_id[:8], last, current_hp)
                    break
        if not attacker_found:
            _combat_history["last_attacker_id"] = "unknown"
            log.info("🩸 DAMAGE detected but attacker unknown")
    else:
        _combat_history["consecutive_damage_ticks"] = 0
        _combat_history["damage_this_tick"] = False
        # Keep last_attacker_id for a while (decay later if needed)

    _combat_history["last_hp"] = current_hp


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


def _select_best_combat_target(targets: list, my_atk: int, equipped,
                                my_def: int, weather: str) -> dict:
    best = None
    best_score = -9999
    my_bonus = get_weapon_bonus(equipped)
    for t in targets:
        if not isinstance(t, dict):
            continue
        t_hp  = max(t.get("hp", 100), 1)
        t_def = t.get("def", 5)
        t_atk = t.get("atk", 10)
        t_bonus = _estimate_enemy_weapon_bonus(t)
        my_dmg   = calc_damage(my_atk, my_bonus, t_def, weather)
        their_dmg = calc_damage(t_atk, t_bonus, my_def, weather)
        kill_speed  = (my_dmg / t_hp) * 100
        threat      = their_dmg * 0.5
        score       = kill_speed - threat
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
        }
    # Cleanup dead
    _known_agents = {k: v for k, v in _known_agents.items() if v.get("isAlive", True)}


def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    """Use Map (always) or Megaphone (endgame lure)."""
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
        if type_id == "map":
            log.info("🗺️ Using Map!")
            return {"action": "use_item", "data": {"itemId": item["id"], "itemType": "map"},
                    "reason": "UTILITY: Using Map"}
    # Megaphone lure in endgame when alone and healthy
    if alive_count <= 5 and hp > 50:
        for item in inventory:
            if not isinstance(item, dict):
                continue
            if item.get("typeId", "").lower() == "megaphone":
                log.info("📢 Using Megaphone to lure enemies (endgame)")
                return {"action": "use_item", "data": {"itemId": item["id"], "itemType": "megaphone"},
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
            terrain_value = {"hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1}.get(terrain, 0)
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
    best = local_items[0]
    score = _pickup_score(best, inventory, heal_count)
    if score <= 0:
        return None
    type_id = best.get("typeId", "item")
    if len(inventory) >= 10:
        drop = _find_droppable_item(inventory, best)
        if drop:
            log.info("INVENTORY FULL: dropping %s for %s", drop.get("typeId"), type_id)
            return {"action": "drop_item", "data": {"itemId": drop["id"]},
                    "reason": f"MAKE ROOM"}
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
            bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
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
            rid = conn.get("id", "")
            is_dz = conn.get("isDeathZone", False)
            if rid and not is_dz and rid not in danger_ids:
                terrain = conn.get("terrain", "").lower()
                score = {"hills": 3, "plains": 2, "ruins": 1, "forest": 0, "water": -2}.get(terrain, 0)
                safe_regions.append((rid, score))
    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]
    # fallback
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
    if critical:
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), reverse=True)
    else:
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0))
    return heals[0]


def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None


def _select_facility(interactables: list, hp: int, ep: int, alive_count: int) -> dict | None:
    """Optimized: watchtower early, broadcast only for endgame lure."""
    best = None
    best_priority = -1
    for fac in interactables:
        if not isinstance(fac, dict) or fac.get("isUsed"):
            continue
        ftype = fac.get("type", "").lower()
        priority = -1
        if ftype == "medical_facility" and hp < 80:
            priority = 10
        elif ftype == "watchtower" and alive_count > 15:  # early/mid game
            priority = 8
        elif ftype == "supply_cache":
            priority = 7
        elif ftype == "broadcast_station" and alive_count <= 5:  # lure only
            priority = 5
        elif ftype == "broadcast_station":
            continue  # avoid broadcast otherwise
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
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        if rid == target_region and rid not in danger_ids:
            is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
            if not is_dz:
                return rid
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        if rid and rid not in danger_ids:
            is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
            if not is_dz:
                return rid
    return None


def _choose_move_target(connections, danger_ids: set, current_region: dict,
                         visible_items: list, alive_count: int,
                         enemies_visible: list = None, current_ep: int = 999) -> str | None:
    """Strategic movement with storm avoidance and explored region memory."""
    global _explored_regions
    candidates = []
    item_regions = set()
    for item in visible_items:
        if isinstance(item, dict):
            item_regions.add(item.get("regionId", ""))

    enemy_regions = set()
    if enemies_visible:
        for e in enemies_visible:
            if isinstance(e, dict):
                enemy_regions.add(e.get("regionId", ""))

    for conn in connections:
        rid = None
        conn_dict = None
        if isinstance(conn, str):
            if conn in danger_ids:
                continue
            rid = conn
            conn_dict = None
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue
            conn_dict = conn

        if rid in _map_knowledge.get("death_zones", set()):
            continue

        # Base score from terrain/weather if dict available, else default 1
        score = 1
        if conn_dict:
            terrain = conn_dict.get("terrain", "").lower()
            weather = conn_dict.get("weather", "").lower()
            score += {"hills": 4, "plains": 2, "ruins": 2, "forest": 1, "water": -3}.get(terrain, 0)
            score += {"clear": 1, "rain": 0, "fog": -1, "storm": -2}.get(weather, 0)

            # v1.6.2: avoid storm if EP < 4 (and not DZ escape — but DZ handled earlier)
            if weather == "storm" and current_ep < 4:
                score -= 100

        # Attraction to items
        if rid in item_regions:
            score += 5

        # Endgame enemy chase
        if rid in enemy_regions and alive_count <= 10:
            score += 4

        # Facilities
        if conn_dict:
            facs = conn_dict.get("interactables", [])
            if facs:
                unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
                score += len(unused) * 2

        # Late game move toward center
        if alive_count < 30:
            score += 3

        # Map knowledge center preference
        if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
            score += 5

        # v1.6.2: penalize already explored regions (reduce score by 5)
        if rid in _explored_regions:
            score -= 5

        candidates.append((rid, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]