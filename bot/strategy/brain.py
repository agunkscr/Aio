"""
Strategy brain v2.2.0 — PvP & Looting Focus.

Fokus utama:
1. SERANG PLAYER (bukan guardian/monster) dengan threshold HP rendah (≥15)
2. LOOTING SENJATA & HEALING (buang item tidak berguna)
3. Hapus fitur tidak relevan: monster farming, guardian farming, facility, third-party
4. Inventory management agresif: drop item bernilai rendah demi senjata/healing
5. Endgame hunt aktif sejak alive ≤ 20
"""

import math
from bot.utils.logger import get_logger

log = get_logger(__name__)

# ── Weapon stats ──────────────────────────────────────────────────────
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

# ── Item priority (hanya senjata, healing, utility penting) ──────────
ITEM_PRIORITY = {
    "rewards":         300,
    "katana":          100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger":          80,  "bow": 75,
    "medkit":          70,  "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars":      55,
    "map":             52,
    "megaphone":       40,
}

# ── Item drop value (semakin rendah semakin siap dibuang) ────────────
ITEM_DROP_VALUE = {
    "rewards":         -1,
    "katana":          10,  "sniper": 9.5, "sword": 9,   "pistol": 8.5,
    "dagger":          8,   "bow": 7.5,
    "medkit":          7,   "bandage": 6.5, "emergency_food": 6, "energy_drink": 5.8,
    "binoculars":      5.5,
    "map":             5.2,
    "megaphone":       4,
    "fist":            0,
}

RECOVERY_ITEMS = {
    "medkit":         50,
    "bandage":        30,
    "emergency_food": 20,
}

WEATHER_COMBAT_PENALTY = {
    "clear": 0.0,
    "rain":  0.05,
    "fog":   0.10,
    "storm": 0.15,
}

# ── Tuning constants ──────────────────────────────────────────────────
AGENT_STALE_TICKS        = 20
MEGAPHONE_MIN_EP         = 4
THREAT_WEIGHT            = 0.8
VULNERABLE_TTL           = 2
DEFAULT_INVENTORY_CAP    = 10
FINISH_OFF_HP_THRESHOLD  = 30   # buru player dengan HP <= 30
FINISH_OFF_BONUS         = 80

EARLY_GAME_TICKS         = 25   # dipersingkat
EARLY_GAME_MIN_ALIVE     = 20
EARLY_GAME_ITEM_BOOST    = 80
EARLY_GAME_MOVE_BOOST    = 10

# ── Global state ──────────────────────────────────────────────────────
_game_id: str  = None
_tick_counter: int = 0

_known_agents: dict = {}
_map_knowledge: dict = {
    "revealed":    False,
    "death_zones": set(),
    "safe_center": [],
}
_combat_history: dict = {
    "last_hp":                 100,
    "consecutive_damage_ticks": 0,
    "last_attacker_id":        "",
    "damage_this_tick":        False,
}
_explored_regions: set  = set()
_map_used_this_tick: bool = False
_map_item_used_ids: set   = set()

_vulnerable_agents: dict           = {}
_visible_region_cache: dict[str, dict] = {}

_last_region_id:    str = ""
_current_target_id: str = ""


# ─────────────────────────────────────────────────────────────────────
# Damage calculation
# ─────────────────────────────────────────────────────────────────────

def calc_damage(atk: int, weapon_bonus: int, target_def: int,
                weather: str = "clear") -> int:
    base    = atk + weapon_bonus - int(target_def * 0.5)
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


# ─────────────────────────────────────────────────────────────────────
# Region helpers
# ─────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────

def reset_game_state():
    global _known_agents, _map_knowledge, _combat_history, _explored_regions
    global _map_used_this_tick, _map_item_used_ids, _tick_counter
    global _vulnerable_agents, _visible_region_cache, _last_region_id
    global _current_target_id
    _tick_counter        = 0
    _known_agents        = {}
    _map_knowledge       = {"revealed": False, "death_zones": set(), "safe_center": []}
    _combat_history      = {
        "last_hp":                  100,
        "consecutive_damage_ticks":   0,
        "last_attacker_id":          "",
        "damage_this_tick":         False,
    }
    _explored_regions    = set()
    _map_used_this_tick  = False
    _map_item_used_ids   = set()
    _vulnerable_agents   = {}
    _visible_region_cache = {}
    _last_region_id      = ""
    _current_target_id   = ""
    log.info("Strategy brain reset: PvP & Looting Focus mode")


def _prepare_move(region_id: str):
    global _last_region_id
    _last_region_id = region_id


def _is_early_game(alive_count: int) -> bool:
    return _tick_counter <= EARLY_GAME_TICKS and alive_count > EARLY_GAME_MIN_ALIVE


# ─────────────────────────────────────────────────────────────────────
# Main decision engine
# ─────────────────────────────────────────────────────────────────────

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    global _game_id, _map_used_this_tick, _explored_regions, _tick_counter
    global _vulnerable_agents, _visible_region_cache, _map_item_used_ids
    global _last_region_id, _current_target_id

    new_game_id = view.get("gameId", "")
    if new_game_id and new_game_id != _game_id:
        reset_game_state()
        _game_id = new_game_id

    _tick_counter += 1

    self_data     = view.get("self", {})
    region        = view.get("currentRegion", {})
    hp            = self_data.get("hp", 100)
    ep            = self_data.get("ep", 10)
    max_ep        = self_data.get("maxEp", 10)
    atk           = self_data.get("atk", 10)
    defense       = self_data.get("def", 5)
    is_alive      = self_data.get("isAlive", True)
    inventory     = self_data.get("inventory", [])
    equipped      = self_data.get("equippedWeapon")
    my_id         = self_data.get("id", "")
    inv_cap       = self_data.get("inventoryCapacity", DEFAULT_INVENTORY_CAP)

    visible_agents    = view.get("visibleAgents", [])
    visible_items_raw = view.get("visibleItems", [])
    visible_regions   = view.get("visibleRegions", [])
    connected_regions = view.get("connectedRegions", [])
    pending_dz        = view.get("pendingDeathzones", [])
    alive_count       = view.get("aliveCount", 100)
    recent_logs       = view.get("recentLogs", [])

    early_game = _is_early_game(alive_count)

    # Cache visible regions
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
    region_id      = region.get("id", "")
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""

    if not is_alive:
        return None

    if region_id:
        _explored_regions.add(region_id)

    if _map_used_this_tick:
        _map_used_this_tick = False
        learn_from_map(view)

    current_item_ids = {i.get("id", "") for i in inventory if isinstance(i, dict)}
    _map_item_used_ids.intersection_update(current_item_ids)

    # Danger zones
    danger_ids: set = set()
    for dz in pending_dz:
        if isinstance(dz, dict):
            danger_ids.add(dz.get("id", ""))
        elif isinstance(dz, str):
            danger_ids.add(dz)
    for conn in connections:
        resolved = _resolve_region(conn, view)
        if resolved and resolved.get("isDeathZone"):
            danger_ids.add(resolved.get("id", ""))

    _track_agents(visible_agents, my_id, region_id)
    _update_combat_history(hp, recent_logs, my_id)
    _detect_vulnerable_agents(recent_logs, my_id)

    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)
    w_range      = get_weapon_range(equipped)

    # Hanya target PLAYER (bukan guardian)
    enemies_alive = [
        a for a in visible_agents
        if not a.get("isGuardian", False)
        and a.get("isAlive", True)
        and a.get("id") != my_id
    ]

    has_healing_items = any(
        isinstance(i, dict) and i.get("typeId", "").lower() in RECOVERY_ITEMS
        for i in inventory
    )

    # ─────────────────────────────────────────────────────────────
    # ══════════════════  EARLY GAME  ════════════════════════════
    # ─────────────────────────────────────────────────────────────
    if early_game:
        # DZ escape
        if region.get("isDeathZone", False):
            safe = _find_safe_region(connections, danger_ids, view)
            if safe and ep >= move_ep_cost:
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe}, "reason": "EARLY:DZ"}

        # Desperate flee
        if hp < 20 and not has_healing_items and enemies_alive:
            if ep >= move_ep_cost:
                safe = _find_safe_region(connections, danger_ids, view)
                if safe:
                    _prepare_move(region_id)
                    return {"action": "move", "data": {"regionId": safe}, "reason": "EARLY:FLEE"}

        # Pickup (boosted)
        pickup = _check_pickup(visible_items, inventory, region_id, hp, ep,
                               inv_cap=inv_cap, early_game=True)
        if pickup:
            return pickup

        # Equip best weapon
        equip = _check_equip(inventory, equipped, view, region_id, connections)
        if equip:
            return equip

        # Utility
        util = _use_utility_item(inventory, hp, ep, alive_count)
        if util:
            if util.get("data", {}).get("itemType") == "map":
                _map_used_this_tick = True
            return util

        if not can_act:
            return None

        # Heal if needed
        if hp < 60:
            heal = _find_healing_item(inventory, critical=(hp < 25))
            if heal:
                return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "EARLY:HEAL"}

        # Move toward items
        if ep >= move_ep_cost and connections:
            move_target, score = _choose_move_target(
                connections, danger_ids, region, visible_items,
                alive_count, enemies_alive, ep, early_game=True
            )
            if move_target and score > 0:
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move_target}, "reason": "EARLY:MOVE"}

        # Rest
        if ep < max_ep and not region.get("isDeathZone"):
            return {"action": "rest", "data": {}, "reason": "EARLY:REST"}

        return None

    # ─────────────────────────────────────────────────────────────
    # ══════════════════  NORMAL GAME (PvP focus)  ═══════════════
    # ─────────────────────────────────────────────────────────────

    # 1. DEATHZONE ESCAPE
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": safe}, "reason": "DZ:ESCAPE"}

    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": safe}, "reason": "PRE:DZ"}

    # 2. DESPERATE FLEE / ATTACK
    if hp < 20 and not has_healing_items and enemies_alive:
        if ep >= move_ep_cost:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe}, "reason": "DESP:FLEE"}
        else:
            nearest = _select_weakest_target(enemies_alive)
            if nearest and _is_in_range(nearest, region_id, w_range, connections):
                return {"action": "attack", "data": {"targetId": nearest["id"], "targetType": "agent"},
                        "reason": "DESP:ATTACK"}

    # 3. COUNTER-ATTACK (jika kena damage dan HP cukup)
    if _combat_history.get("damage_this_tick") and hp >= 20:
        attacker_id = _combat_history["last_attacker_id"]
        attacker = None
        if attacker_id and attacker_id != "unknown":
            attacker = next((a for a in visible_agents if a.get("id") == attacker_id and a.get("isAlive", True)), None)
        if not attacker and enemies_alive:
            attacker = enemies_alive[0]
        if attacker and _is_in_range(attacker, region_id, w_range, connections):
            _combat_history["damage_this_tick"] = False
            _current_target_id = attacker.get("id", "")
            return {"action": "attack", "data": {"targetId": attacker["id"], "targetType": "agent"},
                    "reason": "COUNTER"}
        elif attacker and ep >= move_ep_cost:
            move = _move_toward_target(attacker, connections, danger_ids, view)
            if move:
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move}, "reason": "CHASE"}

    # 4. FREE ACTIONS: Pickup & Equip
    pickup = _check_pickup(visible_items, inventory, region_id, hp, ep,
                           inv_cap=inv_cap, early_game=False)
    if pickup:
        return pickup

    equip = _check_equip(inventory, equipped, view, region_id, connections)
    if equip:
        return equip

    util = _use_utility_item(inventory, hp, ep, alive_count)
    if util:
        if util.get("data", {}).get("itemType") == "map":
            _map_used_this_tick = True
        return util

    # COOLDOWN GATE
    if not can_act:
        return None

    # 5. HEALING (kritis dulu)
    if hp < 25:
        heal = _find_healing_item(inventory, critical=True)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "CRIT:HEAL"}
    elif hp < 60:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "HEAL"}

    # 6. EP MANAGEMENT
    energy_drink_count = sum(1 for i in inventory if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink")
    if ep <= 2 and ep < max_ep:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]}, "reason": "EP:LOW"}
    elif energy_drink_count >= 2 and ep <= max_ep - 4:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]}, "reason": "EP:BOOST"}

    # 7. AGGRESSIVE PLAYER HUNT (priority utama)
    #    Serang player jika ada di range dan HP >= 15
    if enemies_alive and ep >= 2 and hp >= 15:
        target = _select_best_combat_target(
            enemies_alive, atk, equipped, defense, region_weather,
            recent_logs=recent_logs, prefer_id=_current_target_id
        )
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), target.get("def", 5), region_weather)
            e_dmg  = calc_damage(target.get("atk", 10), _estimate_enemy_weapon_bonus(target), defense, region_weather)
            hits   = math.ceil(target.get("hp", 100) / max(my_dmg, 1))
            proj_dmg_taken = hits * e_dmg

            # Finish off low HP player
            if target.get("hp", 100) <= FINISH_OFF_HP_THRESHOLD:
                _current_target_id = target.get("id", "")
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"FINISH:HP={target.get('hp')}"}
            # Favorable trade jika damage kita >= 70% damage musuh
            if my_dmg >= e_dmg * 0.7:
                _current_target_id = target.get("id", "")
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"PVP:my={my_dmg} vs {e_dmg}"}
            # Ranged advantage
            if w_range >= 1 and my_dmg >= e_dmg * 0.6:
                _current_target_id = target.get("id", "")
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"RANGED:range={w_range}"}
        else:
            # Chase player
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost:
                _current_target_id = target.get("id", "")
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move}, "reason": "CHASE:PLAYER"}

    # 8. ENDGAME HUNT (alive <= 20, lebih agresif)
    if alive_count <= 20 and enemies_alive and ep >= 2 and hp >= 15:
        target = _select_best_combat_target(
            enemies_alive, atk, equipped, defense, region_weather,
            recent_logs=recent_logs, prefer_id=_current_target_id
        )
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), target.get("def", 5), region_weather)
            e_dmg  = calc_damage(target.get("atk", 10), _estimate_enemy_weapon_bonus(target), defense, region_weather)
            if my_dmg >= e_dmg * 0.6 or target.get("hp", 100) <= FINISH_OFF_HP_THRESHOLD:
                _current_target_id = target.get("id", "")
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"ENDGAME:alive={alive_count}"}
        else:
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost:
                _current_target_id = target.get("id", "")
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move}, "reason": "ENDGAME:CHASE"}

    # 9. STRATEGIC MOVEMENT (cari player atau item)
    if ep >= move_ep_cost and connections:
        move_target, best_score = _choose_move_target(
            connections, danger_ids, region, visible_items,
            alive_count, enemies_alive, ep, early_game=False
        )
        if move_target and best_score > 0:
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": move_target}, "reason": "MOVE"}

    # 10. REST (jika tidak ada yang bisa dilakukan)
    if ep < max_ep and not region.get("isDeathZone") and not enemies_alive:
        return {"action": "rest", "data": {}, "reason": "REST"}

    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Equip
# ─────────────────────────────────────────────────────────────────────

def _check_equip(inventory: list, equipped, view: dict,
                 region_id: str, connections=None) -> dict | None:
    nearby_enemy = _is_enemy_nearby(view, region_id, connections)
    current_type = equipped.get("typeId", "").lower() if equipped else "fist"
    current_id   = equipped.get("id", "") if equipped else ""

    best = None
    best_score = get_weapon_bonus(equipped)

    for item in inventory:
        if not isinstance(item, dict) or item.get("category") != "weapon":
            continue
        if item.get("id") == current_id:
            continue
        type_id = item.get("typeId", "").lower()
        bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
        rng = WEAPONS.get(type_id, {}).get("range", 0)
        score = bonus
        if nearby_enemy and rng >= 1:
            score += 40
        if score > best_score:
            best_score = score
            best = item

    if best:
        log.info("EQUIP: %s", best.get("typeId"))
        return {"action": "equip", "data": {"itemId": best["id"]}, "reason": "EQUIP"}
    return None


def _is_enemy_nearby(view: dict, my_region: str, connections) -> bool:
    visible = view.get("visibleAgents", [])
    adj_ids = set()
    for conn in connections or []:
        cid = conn if isinstance(conn, str) else conn.get("id", "")
        if cid:
            adj_ids.add(cid)
    for agent in visible:
        if agent.get("isGuardian", False) or not agent.get("isAlive", True):
            continue
        target_region = agent.get("regionId", "")
        if target_region and target_region != my_region and target_region in adj_ids:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Helper — Pickup dengan inventory cleanup agresif
# ─────────────────────────────────────────────────────────────────────

def _check_pickup(items: list, inventory: list, region_id: str,
                  hp: int = 100, ep: int = 10,
                  inv_cap: int = DEFAULT_INVENTORY_CAP,
                  early_game: bool = False) -> dict | None:
    local_items = [i for i in items if isinstance(i, dict) and i.get("id") and i.get("regionId") == region_id]
    if not local_items:
        return None

    heal_count = sum(1 for i in inventory if isinstance(i, dict) and i.get("typeId", "").lower() in RECOVERY_ITEMS)

    def score_fn(item):
        base = _pickup_score(item, inventory, heal_count, hp, ep)
        if early_game and base > 0:
            return base + EARLY_GAME_ITEM_BOOST
        return base

    local_items.sort(key=score_fn, reverse=True)
    best = local_items[0]
    score = score_fn(best)

    if score <= 0:
        return None

    type_id = best.get("typeId", "item")
    if len(inventory) >= inv_cap:
        # Cari item paling tidak berguna untuk dibuang
        drop = _find_worst_item(inventory, best)
        if drop:
            log.info("DROP %s untuk %s", drop.get("typeId"), type_id)
            return {"action": "drop_item", "data": {"itemId": drop["id"]}, "reason": f"MAKE ROOM for {type_id}"}
        return None

    log.info("PICKUP: %s (score=%d)", type_id, score)
    return {"action": "pickup", "data": {"itemId": best["id"]}, "reason": f"PICKUP {type_id}"}


def _pickup_score(item: dict, inventory: list, heal_count: int,
                  hp: int = 100, ep: int = 10) -> int:
    type_id = item.get("typeId", "").lower()
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
        return (100 + bonus) if bonus > current_best else 0

    # Healing items
    if type_id in RECOVERY_ITEMS:
        if heal_count >= 3 and hp >= 70:
            return 20
        dynamic = 100 if (hp < 50) else 0
        return ITEM_PRIORITY.get(type_id, 0) + (10 if heal_count < 4 else 0) + dynamic

    if type_id == "energy_drink":
        dynamic = 100 if (ep < 4) else 0
        return ITEM_PRIORITY.get(type_id, 0) + dynamic

    if type_id == "binoculars":
        has = any(i.get("typeId", "").lower() == "binoculars" for i in inventory if isinstance(i, dict))
        return 55 if not has else 0

    if type_id == "map":
        return 52

    return ITEM_PRIORITY.get(type_id, 0)


def _find_worst_item(inventory: list, target_item: dict) -> dict | None:
    """Cari item dengan nilai drop terendah (paling tidak berguna) untuk dibuang."""
    target_score = ITEM_DROP_VALUE.get(target_item.get("typeId", "").lower(), 1)
    candidates = []
    for item in inventory:
        if not isinstance(item, dict):
            continue
        tid = item.get("typeId", "").lower()
        cat = item.get("category", "").lower()
        # Jangan buang rewards / currency
        if cat == "currency" or tid == "rewards":
            continue
        drop_val = ITEM_DROP_VALUE.get(tid, 1)
        if drop_val < target_score:
            candidates.append((item, drop_val))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def _find_healing_item(inventory: list, critical: bool = False) -> dict | None:
    heals = [i for i in inventory if isinstance(i, dict) and i.get("typeId", "").lower() in RECOVERY_ITEMS]
    if not heals:
        return None
    heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), reverse=critical)
    return heals[0]


def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Movement
# ─────────────────────────────────────────────────────────────────────

def _choose_move_target(connections, danger_ids: set, current_region: dict,
                        visible_items: list, alive_count: int,
                        enemies_visible: list = None, current_ep: int = 999,
                        early_game: bool = False):
    global _explored_regions, _known_agents, _last_region_id

    candidates = []
    item_regions = {item.get("regionId", "") for item in visible_items if isinstance(item, dict)}
    enemy_regions = set()
    enemy_threat_map = {}

    if enemies_visible:
        for e in enemies_visible:
            reg = e.get("regionId", "")
            e_id = e.get("id", "")
            enemy_regions.add(reg)
            threat = e.get("atk", 10) + e.get("def", 5)
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
            score += 5 if not early_game else 5 + EARLY_GAME_MOVE_BOOST

        # Prioritaskan region yang ada player (untuk PvP)
        if rid in enemy_regions and alive_count <= 20:
            score += 15

        if rid in _explored_regions:
            score -= 5

        if rid == _last_region_id:
            score -= 50

        candidates.append((rid, score))

    if not candidates:
        return None, -999
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0], candidates[0][1]


def _move_toward_target(target: dict, connections, danger_ids: set, view: dict) -> str | None:
    target_region = target.get("regionId", "")
    if not target_region:
        return None
    safe_conn_ids = []
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and rid not in danger_ids and not is_dz:
            safe_conn_ids.append(rid)
    if target_region in safe_conn_ids:
        return target_region
    # 2-hop
    for step1_id in safe_conn_ids:
        step1_region = _visible_region_cache.get(step1_id)
        if not step1_region:
            continue
        for step2 in step1_region.get("connections", []):
            step2_id = step2 if isinstance(step2, str) else step2.get("id", "")
            if step2_id == target_region:
                return step1_id
    return safe_conn_ids[0] if safe_conn_ids else None


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
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            return rid
    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Combat target selection (PvP focused)
# ─────────────────────────────────────────────────────────────────────

def _select_best_combat_target(targets: list, my_atk: int, equipped,
                                my_def: int, weather: str,
                                recent_logs: list = None,
                                prefer_id: str = "") -> dict:
    global _vulnerable_agents
    best = None
    best_score = -9999
    my_bonus = get_weapon_bonus(equipped)

    for t in targets:
        t_id = t.get("id", "")
        t_hp = max(t.get("hp", 100), 1)
        t_def = t.get("def", 5)
        t_atk = t.get("atk", 10)
        t_bonus = _estimate_enemy_weapon_bonus(t)

        my_dmg = calc_damage(my_atk, my_bonus, t_def, weather)
        their_dmg = calc_damage(t_atk, t_bonus, my_def, weather)
        kill_speed = (my_dmg / t_hp) * 100
        threat = their_dmg * THREAT_WEIGHT
        score = kill_speed - threat

        # Bonus besar untuk player (bukan guardian)
        if not t.get("isGuardian", False):
            score += 50

        if t_id in _vulnerable_agents:
            score += 40
        if recent_logs and _is_agent_fighting(t_id, recent_logs):
            score += 30
        if t_hp <= FINISH_OFF_HP_THRESHOLD:
            score += FINISH_OFF_BONUS
        if prefer_id and t_id == prefer_id:
            score += 20

        if score > best_score:
            best_score = score
            best = t

    return best if best else min(targets, key=lambda t: t.get("hp", 999))


def _select_weakest_target(targets: list) -> dict | None:
    return min(targets, key=lambda t: t.get("hp", 999)) if targets else None


def _is_in_range(target: dict, my_region: str, weapon_range: int, connections=None) -> bool:
    target_region = target.get("regionId", "")
    if not target_region or target_region == my_region:
        return True
    if weapon_range >= 1 and connections:
        adj_ids = set()
        for conn in connections:
            rid = conn if isinstance(conn, str) else conn.get("id", "")
            if rid:
                adj_ids.add(rid)
        return target_region in adj_ids
    return False


# ─────────────────────────────────────────────────────────────────────
# Helper — Agent tracking & utilities
# ─────────────────────────────────────────────────────────────────────

def _track_agents(visible_agents: list, my_id: str, my_region: str):
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
            "def": agent.get("def", 5),
            "isGuardian": agent.get("isGuardian", False),
            "equippedWeapon": agent.get("equippedWeapon"),
            "lastSeen": my_region,
            "isAlive": agent.get("isAlive", True),
            "lastSeenTick": _tick_counter,
        }
    stale_cutoff = _tick_counter - AGENT_STALE_TICKS
    _known_agents = {k: v for k, v in _known_agents.items() if v.get("isAlive", True) and v.get("lastSeenTick", 0) >= stale_cutoff}


def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    global _map_item_used_ids
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
        if type_id == "map":
            item_id = item.get("id", "")
            if item_id and item_id not in _map_item_used_ids:
                _map_item_used_ids.add(item_id)
                log.info("Using Map")
                return {"action": "use_item", "data": {"itemId": item_id, "itemType": "map"}, "reason": "MAP"}
    if alive_count <= 5 and hp > 50 and ep >= MEGAPHONE_MIN_EP:
        for item in inventory:
            if isinstance(item, dict) and item.get("typeId", "").lower() == "megaphone":
                log.info("Megaphone lure")
                return {"action": "use_item", "data": {"itemId": item["id"], "itemType": "megaphone"}, "reason": "MEGAPHONE"}
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
    log.info("Map learned: %d DZ", len(_map_knowledge["death_zones"]))


def _update_combat_history(current_hp: int, recent_logs: list, my_id: str):
    global _combat_history
    last = _combat_history.get("last_hp", current_hp)
    if current_hp < last:
        _combat_history["consecutive_damage_ticks"] += 1
        _combat_history["damage_this_tick"] = True
        for log_entry in recent_logs:
            if not isinstance(log_entry, dict):
                continue
            if log_entry.get("type") in ("damage", "attack"):
                attacker_id = log_entry.get("attackerId") or log_entry.get("sourceId") or ""
                target_id = log_entry.get("targetId") or ""
                if target_id == my_id and attacker_id and attacker_id != my_id:
                    _combat_history["last_attacker_id"] = attacker_id
                    break
        else:
            _combat_history["last_attacker_id"] = "unknown"
    else:
        _combat_history["consecutive_damage_ticks"] = 0
        _combat_history["damage_this_tick"] = False
    _combat_history["last_hp"] = current_hp


def _detect_vulnerable_agents(recent_logs: list, my_id: str):
    global _vulnerable_agents
    _vulnerable_agents = {k: v for k, v in _vulnerable_agents.items() if _tick_counter - v < VULNERABLE_TTL}
    for entry in recent_logs:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "use_item":
            user_id = entry.get("agentId") or entry.get("userId", "")
            item_name = entry.get("itemName", "").lower()
            if any(h in item_name for h in ("medkit", "bandage", "food", "emergency")):
                if user_id and user_id != my_id:
                    _vulnerable_agents[user_id] = _tick_counter


def _is_agent_fighting(agent_id: str, recent_logs: list) -> bool:
    for entry in recent_logs:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") in ("attack", "damage"):
            if entry.get("attackerId") == agent_id or entry.get("sourceId") == agent_id or entry.get("targetId") == agent_id:
                return True
    return False


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