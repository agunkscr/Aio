"""
Strategy brain — main decision engine with priority-based action selection.
Implements the game-loop.md priority chain for high win rate.

v1.6.0 improvements over v1.5.2:
- FIX: removed duplicate _known_agents global definition (bug)
- COMBAT: smarter target selection — kill probability + threat weighting
- CHASE: move-to-attack when enemy visible but out of melee range
- FLEE: adaptive flee threshold — factors in enemy ATK, not just fixed HP=40
- INVENTORY: drop lowest-value item when full to pick up better items
- EP: proactive energy drink use before combat (EP < 3)
- ENDGAME: hunt mode when alive_count < 10 — actively chase enemies
- HEALING: heal after kill if HP < 75 (opportunistic recovery)
- COMBAT HISTORY: track recent damage to detect losing fights and flee
- RANGED POSITIONING: with sniper/bow, prefer staying in adjacent region to attack
- GUARDIAN: smarter range-approach logic (move toward guardian if profitable)
- REST: smarter EP threshold — rest when truly safe AND EP critically low

v1.5.2 changes (preserved):
- Guardians now ATTACK player agents directly (hostile combatants)
- Curse is TEMPORARILY DISABLED (no whisper Q&A flow)
- Free room: 5 guardians (reduced from 30), each drops 120 sMoltz
- connectedRegions: either full Region objects OR bare string IDs — type-check!
- pendingDeathzones: entries are {id, name} objects
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
    "rewards":         300,   # Moltz/sMoltz — ALWAYS pickup first
    "katana":          100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger":          80,  "bow": 75,
    "medkit":          70,  "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars":      55,  # Passive: vision +1 permanent, always pickup
    "map":             52,  # Use immediately to reveal entire map
    "megaphone":       40,
}

# ── Item value for drop decisions (lower = drop first) ────────────────
ITEM_DROP_VALUE = {
    "rewards":         -1,    # NEVER drop currency
    "katana":          10, "sniper": 9.5, "sword": 9, "pistol": 8.5,
    "dagger":          8,  "bow": 7.5,
    "medkit":          7,  "bandage": 6.5, "emergency_food": 6, "energy_drink": 5.8,
    "binoculars":      5.5,
    "map":             5.2,
    "megaphone":       4,
    "fist":            0,    # Bare hands — drop anything before this
}

# ── Recovery items (HP) ───────────────────────────────────────────────
RECOVERY_ITEMS = {
    "medkit":          50, "bandage": 30, "emergency_food": 20,
    "energy_drink":    0,   # EP restore, not HP
}

# Weather combat penalty per game-systems.md
WEATHER_COMBAT_PENALTY = {
    "clear": 0.0,
    "rain":  0.05,
    "fog":   0.10,
    "storm": 0.15,
}

# ── Global state ──────────────────────────────────────────────────────
_known_agents: dict = {}
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": []}
# Combat history: track recent HP snapshots to detect losing fights
_combat_history: dict = {"last_hp": 100, "consecutive_damage_ticks": 0}


# ── Damage calculation ────────────────────────────────────────────────

def calc_damage(atk: int, weapon_bonus: int, target_def: int,
                weather: str = "clear") -> int:
    """Damage formula per combat-items.md + game-systems.md weather penalty.
    Base: ATK + bonus - (DEF * 0.5), min 1.
    Weather penalty applied multiplicatively.
    """
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
    """Resolve connectedRegions entry to full Region object or None."""
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
    """Reset per-game tracking state. Call when game ends."""
    global _known_agents, _map_knowledge, _combat_history
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _combat_history = {"last_hp": 100, "consecutive_damage_ticks": 0}
    log.info("Strategy brain reset for new game")


# ── Main decision engine ──────────────────────────────────────────────

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine. Returns action dict or None (wait).

    Priority chain (v1.6.0):
    1.  DEATHZONE ESCAPE (overrides everything — 1.34 HP/sec!)
    1b. Pre-escape pending death zone
    2.  [DISABLED] Curse resolution
    2b. Guardian threat evasion (adaptive threshold based on enemy ATK)
    2c. Losing-fight detection — flee if taking heavy consecutive damage
    3.  Free actions: pickup, equip  ← always check first (no cooldown cost)
    3b. Use utility items (Map)
    [cooldown gate]
    4.  Critical healing (HP < 30)
    4b. Proactive EP management (energy drink before combat if EP < 3)
    5.  Guardian farming (120 sMoltz — approach if needed, fight if favorable)
    6.  Endgame hunt mode (alive_count < 10: actively chase enemies)
    6b. Favorable agent combat (normal game)
    7.  Monster farming
    7b. Opportunistic heal (HP < 75, area safe)
    8.  Facility interaction
    9.  Strategic movement (NEVER into DZ or pending DZ)
    10. Rest (EP < 3, truly safe)
    """
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

    connections  = connected_regions or region.get("connections", [])
    interactables= region.get("interactables", [])
    region_id    = region.get("id", "")
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""

    if not is_alive:
        return None

    # ── Build danger map (DZ + pending DZ) ───────────────────────────
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

    # ── Update combat history (losing-fight detection) ────────────────
    _update_combat_history(hp)

    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # ── Priority 1: DEATHZONE ESCAPE ─────────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 IN DEATH ZONE! Escaping to %s (HP=%d)", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In death zone! HP={hp} dropping fast (1.34/sec)"}
        elif not safe:
            log.error("🚨 IN DEATH ZONE but NO SAFE REGION found!")

    # ── Priority 1b: Pre-escape pending death zone ────────────────────
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("⚠️ Region %s becoming DZ soon! Pre-escaping to %s", region_id[:8], safe)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region becoming death zone soon"}

    # ── Priority 2: Curse — DISABLED in v1.5.2 ───────────────────────

    # ── Priority 2b: Guardian threat evasion (adaptive) ──────────────
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]
    if guardians_here and ep >= move_ep_cost:
        # Adaptive flee threshold based on guardian's ATK
        threat_guardian = max(guardians_here, key=lambda g: g.get("atk", 10))
        g_dmg = calc_damage(threat_guardian.get("atk", 10),
                            _estimate_enemy_weapon_bonus(threat_guardian),
                            defense, region_weather)
        # Flee if we'd take more than 30% of current HP per hit, or HP already low
        flee_hp_threshold = max(40, g_dmg * 2)
        if hp < flee_hp_threshold:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                log.warning("⚠️ Guardian threat! HP=%d, g_dmg=%d, fleeing", hp, g_dmg)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"GUARDIAN FLEE: HP={hp} < threshold={flee_hp_threshold}"}

    # ── Priority 2c: Losing-fight detection ──────────────────────────
    # If we've taken consecutive damage AND HP is dangerously low, flee
    if _combat_history["consecutive_damage_ticks"] >= 2 and hp < 35:
        enemies_here = [a for a in visible_agents
                        if not a.get("isGuardian", False) and a.get("isAlive", True)
                        and a.get("id") != my_id and a.get("regionId") == region_id]
        if enemies_here and ep >= move_ep_cost:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                log.warning("⚠️ Losing fight! HP=%d, consecutive dmg ticks=%d. Fleeing.",
                            hp, _combat_history["consecutive_damage_ticks"])
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"LOSING FIGHT FLEE: HP={hp}, took damage {_combat_history['consecutive_damage_ticks']} ticks"}

    # ── Priority 3: FREE ACTIONS (pickup + equip — no cooldown) ──────
    pickup_action = _check_pickup(visible_items, inventory, region_id)
    if pickup_action:
        return pickup_action

    equip_action = _check_equip(inventory, equipped)
    if equip_action:
        return equip_action

    # ── Priority 3b: Use utility items ───────────────────────────────
    util_action = _use_utility_item(inventory, hp, ep, alive_count)
    if util_action:
        return util_action

    # ── Cooldown gate ─────────────────────────────────────────────────
    if not can_act:
        return None

    # ── Priority 4: Critical healing ─────────────────────────────────
    if hp < 30:
        heal = _find_healing_item(inventory, critical=True)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL: HP={hp}, using {heal.get('typeId', 'heal')}"}

    elif hp < 70:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, using {heal.get('typeId', 'heal')}"}

    # ── Priority 4b: Proactive EP management ─────────────────────────
    # Use energy drink proactively when EP is low AND combat/movement needed
    if ep <= 2:
        energy_drink = _find_energy_drink(inventory)
        if energy_drink:
            return {"action": "use_item", "data": {"itemId": energy_drink["id"]},
                    "reason": f"EP RECOVERY: EP={ep}, using energy drink proactively (+5 EP)"}

    # ── Priority 5: Guardian farming (120 sMoltz per kill!) ──────────
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians and ep >= 2 and hp >= 35:
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
                        "reason": f"GUARDIAN FARM: HP={target.get('hp','?')} "
                                  f"dmg={my_dmg} vs {g_dmg} (120 sMoltz!)"}
        else:
            # Out of range — move toward guardian if profitable
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost and hp >= 50:
                return {"action": "move", "data": {"regionId": move},
                        "reason": f"APPROACH GUARDIAN: moving closer for 120 sMoltz kill"}

    # ── Priority 6: Endgame hunt mode (alive_count < 10) ─────────────
    enemies_alive = [a for a in visible_agents
                     if not a.get("isGuardian", False) and a.get("isAlive", True)
                     and a.get("id") != my_id]
    if alive_count <= 10 and enemies_alive and ep >= 2 and hp >= 30:
        # Late game: be aggressive — hunt weakest enemy regardless of DPS parity
        target = _select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "agent"},
                    "reason": f"ENDGAME HUNT: alive={alive_count}, target HP={target.get('hp','?')}, dmg={my_dmg}"}
        else:
            # Chase them
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost:
                return {"action": "move", "data": {"regionId": move},
                        "reason": f"ENDGAME CHASE: alive={alive_count}, hunting last enemies"}

    # ── Priority 6b: Favorable agent combat (normal game) ────────────
    hp_threshold = 40 if alive_count > 20 else 30
    if enemies_alive and ep >= 2 and hp >= hp_threshold:
        target = _select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            e_dmg  = calc_damage(target.get("atk", 10),
                                 _estimate_enemy_weapon_bonus(target),
                                 defense, region_weather)
            # Attack if: we deal more damage, OR target is near death (finish off)
            if my_dmg > e_dmg or target.get("hp", 100) <= my_dmg * 2:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"COMBAT: Target HP={target.get('hp','?')}, "
                                  f"dmg={my_dmg} vs enemy_dmg={e_dmg}"}
            # With ranged weapon, attack even if slightly disadvantaged (stay at range)
            elif w_range >= 1 and my_dmg >= e_dmg * 0.8:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"RANGED ATTACK: dmg={my_dmg} acceptable at range={w_range}"}

    # ── Priority 7: Monster farming ───────────────────────────────────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2 and hp >= 30:
        target = _select_best_combat_target(monsters, atk, equipped, defense, region_weather)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            m_dmg  = calc_damage(target.get("atk", 10), 0, defense, region_weather)
            # Only fight monster if manageable damage exchange
            if my_dmg >= m_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "monster"},
                        "reason": f"MONSTER FARM: {target.get('name','?')} HP={target.get('hp','?')}"}

    # ── Priority 7b: Opportunistic healing (safe area) ────────────────
    # Heal back up to 75 when area is clear (broader threshold than critical)
    if hp < 75 and not enemies_alive and not guardians_here:
        heal = _find_healing_item(inventory, critical=(hp < 30))
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, area safe, using {heal.get('typeId', 'heal')}"}

    # ── Priority 8: Facility interaction ─────────────────────────────
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep)
        if facility:
            return {"action": "interact",
                    "data": {"interactableId": facility["id"]},
                    "reason": f"FACILITY: {facility.get('type', 'unknown')}"}

    # ── Priority 9: Strategic movement ───────────────────────────────
    if ep >= move_ep_cost and connections:
        move_target = _choose_move_target(connections, danger_ids,
                                          region, visible_items, alive_count,
                                          enemies_alive)
        if move_target:
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "EXPLORE: Moving to better position"}

    # ── Priority 10: Rest (EP < 3, truly safe) ───────────────────────
    if ep < 3 and not enemies_alive and not guardians_here \
            and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}, area is safe (+1 bonus EP)"}

    return None


# ── Helper functions ──────────────────────────────────────────────────

def _update_combat_history(current_hp: int):
    """Track HP changes to detect consecutive damage (losing fights)."""
    global _combat_history
    last = _combat_history.get("last_hp", current_hp)
    if current_hp < last:
        _combat_history["consecutive_damage_ticks"] += 1
    else:
        _combat_history["consecutive_damage_ticks"] = 0
    _combat_history["last_hp"] = current_hp


def _get_move_ep_cost(terrain: str, weather: str) -> int:
    """Calculate move EP cost per game-systems.md."""
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
    """
    Select optimal combat target using kill-probability scoring.

    Score = (our_dmg / target_hp) * 100  — how fast can we kill them?
    Penalty  = their estimated DPS against us (threat level)
    Final score = kill_speed - threat_weight

    Ties broken by lowest HP (finish them off first).
    """
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
        threat      = their_dmg * 0.5   # Partial weight — killing fast matters more
        score       = kill_speed - threat

        if score > best_score:
            best_score = score
            best = t

    return best if best else min(targets, key=lambda t: t.get("hp", 999))


def _track_agents(visible_agents: list, my_id: str, my_region: str):
    """Track observed agents for threat assessment."""
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
    if len(_known_agents) > 50:
        dead = [k for k, v in _known_agents.items() if not v.get("isAlive", True)]
        for d in dead:
            del _known_agents[d]


def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    """Use utility items immediately after pickup.
    Map: reveals entire map (1-time consumable).
    Binoculars: PASSIVE (vision+1, no use_item needed).
    """
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
        if type_id == "map":
            log.info("🗺️ Using Map! Will reveal entire map.")
            return {"action": "use_item", "data": {"itemId": item["id"]},
                    "reason": "UTILITY: Using Map — reveals entire map for DZ tracking"}
    return None


def learn_from_map(view: dict):
    """Called after Map is used — learn entire map layout."""
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
    """Smart pickup: Moltz > weapons > utility > healing.
    Max inventory = 10. If full, drop lowest-value item to grab better one.
    """
    local_items = [i for i in items if isinstance(i, dict) and i.get("id")
                   and i.get("regionId") == region_id]
    if not local_items:
        # Fallback: use all visible items if regionId not set
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

    # Inventory full — try to drop worst item if best pickup is clearly better
    if len(inventory) >= 10:
        drop = _find_droppable_item(inventory, best)
        if drop:
            log.info("INVENTORY FULL: dropping %s to pick up %s",
                     drop.get("typeId"), type_id)
            # Drop the low-value item (game must support drop action)
            # If drop is not supported, skip pickup
            return {"action": "drop_item", "data": {"itemId": drop["id"]},
                    "reason": f"MAKE ROOM: dropping {drop.get('typeId','?')} for {type_id}"}
        return None  # Inventory full, nothing worth dropping

    log.info("PICKUP: %s (score=%d, heal_stock=%d)", type_id, score, heal_count)
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
    """Find the lowest-value item in inventory to drop, if target_item is clearly better.
    Never drop currency. Return None if no suitable item to drop.
    """
    target_score = ITEM_DROP_VALUE.get(target_item.get("typeId", "").lower(), 1)
    candidates = []
    for item in inventory:
        if not isinstance(item, dict):
            continue
        tid = item.get("typeId", "").lower()
        cat = item.get("category", "").lower()
        if cat == "currency" or tid == "rewards":
            continue  # Never drop currency
        drop_val = ITEM_DROP_VALUE.get(tid, 1)
        if drop_val < target_score:
            candidates.append((item, drop_val))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def _check_equip(inventory: list, equipped) -> dict | None:
    """Auto-equip best weapon from inventory."""
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
                "reason": f"EQUIP: {best.get('typeId','weapon')} (+{best_bonus} ATK)"}
    return None


def _find_safe_region(connections, danger_ids: set, view: dict = None) -> str | None:
    """Find nearest non-DZ, non-pending-DZ connected region."""
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
    # Last resort fallback
    for conn in connections:
        rid   = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            log.warning("No fully safe region! Using fallback: %s", rid[:8])
            return rid
    return None


def _find_healing_item(inventory: list, critical: bool = False) -> dict | None:
    """Find best healing item based on urgency."""
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


def _select_facility(interactables: list, hp: int, ep: int) -> dict | None:
    """Select best facility to interact with."""
    for fac in interactables:
        if not isinstance(fac, dict) or fac.get("isUsed"):
            continue
        ftype = fac.get("type", "").lower()
        if ftype == "medical_facility" and hp < 80:
            return fac
        if ftype == "supply_cache":
            return fac
        if ftype == "watchtower":
            return fac
        if ftype == "broadcast_station":
            return fac
    return None


def _is_in_range(target: dict, my_region: str, weapon_range: int,
                  connections=None) -> bool:
    """Check if target is in weapon range."""
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
    """Move one step toward target's region, avoiding danger zones.
    Returns regionId to move to, or None if not possible.
    """
    target_region = target.get("regionId", "")
    if not target_region:
        return None
    # If target is in an adjacent region, move there (if safe)
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        if rid == target_region and rid not in danger_ids:
            is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
            if not is_dz:
                return rid
    # Target is farther — move toward a region that's closer to target
    # (best approximation without full path-finding: pick any non-danger neighbor)
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        if rid and rid not in danger_ids:
            is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
            if not is_dz:
                return rid
    return None


def _choose_move_target(connections, danger_ids: set, current_region: dict,
                         visible_items: list, alive_count: int,
                         enemies_visible: list = None) -> str | None:
    """Choose best region to move to — NEVER into DZ or pending DZ."""
    candidates = []

    item_regions = set()
    for item in visible_items:
        if isinstance(item, dict):
            item_regions.add(item.get("regionId", ""))

    # Set of enemy regions (attract in endgame, mild attraction normally)
    enemy_regions = set()
    if enemies_visible:
        for e in enemies_visible:
            if isinstance(e, dict):
                enemy_regions.add(e.get("regionId", ""))

    for conn in connections:
        if isinstance(conn, str):
            if conn in danger_ids:
                continue
            score = 1
            if conn in item_regions:
                score += 5
            if conn in enemy_regions and alive_count <= 10:
                score += 4  # Hunt in endgame
            candidates.append((conn, score))

        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue
            if rid in _map_knowledge.get("death_zones", set()):
                continue

            terrain = conn.get("terrain", "").lower()
            weather = conn.get("weather", "").lower()
            score   = 0

            score += {"hills": 4, "plains": 2, "ruins": 2, "forest": 1, "water": -3}.get(terrain, 0)
            score += {"clear": 1, "rain": 0, "fog": -1, "storm": -2}.get(weather, 0)

            if rid in item_regions:
                score += 5
            if rid in enemy_regions and alive_count <= 10:
                score += 4  # Endgame: chase enemies

            facs = conn.get("interactables", [])
            if facs:
                unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
                score += len(unused) * 2

            if alive_count < 30:
                score += 3  # Late game: move toward center

            if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                score += 5  # Prefer map-knowledge center regions

            candidates.append((rid, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


"""
View fields from api-summary.md (all implemented — v1.6.0):
✅ self             — hp, ep, atk, def, inventory, equippedWeapon, isAlive
✅ currentRegion    — id, name, terrain, weather, connections, interactables, isDeathZone
✅ connectedRegions — full Region objects OR bare string IDs (type-safe via _resolve_region)
✅ visibleRegions   — connectedRegions fallback + region ID lookup
✅ visibleAgents    — guardians (HOSTILE!) + enemies, combat targeting + threat scoring
✅ visibleMonsters  — monster farming
✅ visibleNPCs      — acknowledged (NPCs flavor only)
✅ visibleItems     — pickup + movement attraction scoring + inventory drop decisions
✅ pendingDeathzones — {id, name} entries for pre-escape + movement planning
✅ recentLogs       — available for analysis
✅ recentMessages   — communication (curse disabled v1.5.2)
✅ aliveCount       — adaptive aggression, endgame hunt mode, late-game positioning

v1.6.0 new global state:
✅ _combat_history  — consecutive damage tracking for losing-fight flee detection
"""
