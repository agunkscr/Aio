"""
Strategy brain v2.0.0 — Full upgrade & bug fix + aggressive player hunting.

Perubahan dari v1.9.2 + aggressive mod:
────────────────────────────────────────────────────────────────────────
[NEW] Lower HP threshold for initiating combat (15 instead of 20-25)
[NEW] Favorable combat condition: my_dmg >= e_dmg * 0.8 (was > e_dmg)
[NEW] Endgame hunt starts at alive <= 15 (was <= 10)
[NEW] Player targets get +20 bonus in selection
[NEW] Finish-off bonus increased to 70
[NEW] Ranged attack threshold lowered to 0.7 (was 0.85)
[NEW] Aggressive combat block moved earlier (after binoculars)
────────────────────────────────────────────────────────────────────────
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

# ── Item priority ─────────────────────────────────────────────────────
ITEM_PRIORITY = {
    "rewards":         300,
    "katana":          100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger":          80,  "bow": 75,
    "medkit":          70,  "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars":      55,
    "map":             52,
    "megaphone":       40,
}

# ── Item drop value ───────────────────────────────────────────────────
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

# ── Recovery items (nilai HP yang dipulihkan; 0 = EP item) ───────────
RECOVERY_ITEMS = {
    "medkit":         50,
    "bandage":        30,
    "emergency_food": 20,
    # energy_drink tidak masuk sini — ditangani sendiri via _find_energy_drink
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
FINISH_OFF_HP_THRESHOLD  = 25   # musuh HP <= ini mendapat bonus finish-off
FINISH_OFF_BONUS         = 70   # [AGGRESSIVE] dinaikkan dari 50

# ── Early game constants ──────────────────────────────────────────────
EARLY_GAME_TICKS         = 30   # durasi fase early game
EARLY_GAME_MIN_ALIVE     = 15   # keluar early game jika sisa pemain <= ini
EARLY_GAME_ITEM_BOOST    = 80   # bonus pickup score di early game
EARLY_GAME_MOVE_BOOST    = 10   # bonus movement ke region berisi item

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
_current_target_id: str = ""   # persistent target untuk kill consistency


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
    log.info("Strategy brain reset for new game (v2.0.0 aggressive)")


def _prepare_move(region_id: str):
    global _last_region_id
    _last_region_id = region_id


def _is_early_game(alive_count: int) -> bool:
    """True selama fase early game: tick awal DAN pemain masih banyak."""
    return _tick_counter <= EARLY_GAME_TICKS and alive_count > EARLY_GAME_MIN_ALIVE


# ─────────────────────────────────────────────────────────────────────
# Main decision engine
# ─────────────────────────────────────────────────────────────────────

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine v2.0.0 aggressive.

    Priority chain:
    [EARLY GAME] Looting phase — jika _is_early_game() True
      EG1. Pickup (boosted score, region saat ini saja)
      EG2. Equip senjata terbaik
      EG3. Utility items (map, megaphone)
      [cooldown gate]
      EG4. Heal jika HP < 60
      EG5. Movement agresif menuju item
      EG6. Rest

    [NORMAL GAME]
    1.   DEATHZONE ESCAPE
    1b.  Pre-escape pending DZ
    1c.  DESPERATE FLEE
    1d.  COUNTER-ATTACK (HP >= 25)
    2b.  Guardian threat evasion
    3.   Free actions: pickup (region-only), equip (anti-loop)
    3b.  Utility items
    [cooldown gate]
    4.   Critical healing (HP < 25)
    4b.  Proactive EP management
    4c.  Binoculars ranged harassment
    [AGGRESSIVE] 4c+. Favorable agent combat (hp>=15, my_dmg>=0.8*e_dmg)
    4d.  Third-Party Cleanup
    5.   Guardian farming (cek flee-threshold sebelum approach)
    6.   Endgame hunt (alive <= 15, favorability check)
    6b.  Favorable agent combat (secondary, with projected damage)
    7.   Monster farming
    7b.  Opportunistic / camping heal
    8.   Facility interaction
    8.5  Endgame Camping
    8.6  Hold Position
    9.   Strategic movement
    10.  Rest
    """

    global _game_id, _map_used_this_tick, _explored_regions, _tick_counter
    global _vulnerable_agents, _visible_region_cache, _map_item_used_ids
    global _last_region_id, _current_target_id

    # ── Auto-reset on game change ─────────────────────────────────
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
    visible_monsters  = view.get("visibleMonsters", [])
    visible_items_raw = view.get("visibleItems", [])
    visible_regions   = view.get("visibleRegions", [])
    connected_regions = view.get("connectedRegions", [])
    pending_dz        = view.get("pendingDeathzones", [])
    alive_count       = view.get("aliveCount", 100)
    recent_logs       = view.get("recentLogs", [])

    early_game = _is_early_game(alive_count)
    if early_game and _tick_counter == 1:
        log.info("🎮 EARLY GAME PHASE: fokus looting %d tick (alive=%d)", EARLY_GAME_TICKS, alive_count)
    elif not early_game and _tick_counter == EARLY_GAME_TICKS + 1:
        log.info("⚔️ Keluar EARLY GAME → mode combat (tick=%d, alive=%d)", _tick_counter, alive_count)

    # ── Cache visible regions ─────────────────────────────────────
    _visible_region_cache = {}
    for r in visible_regions:
        if isinstance(r, dict) and r.get("id"):
            _visible_region_cache[r["id"]] = r

    # ── Unwrap visibleItems ───────────────────────────────────────
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

    if region_id:
        _explored_regions.add(region_id)

    if _map_used_this_tick:
        _map_used_this_tick = False
        learn_from_map(view)

    current_item_ids = {i.get("id", "") for i in inventory if isinstance(i, dict)}
    _map_item_used_ids.intersection_update(current_item_ids)

    # ── Danger map ────────────────────────────────────────────────
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

    enemies_alive = [
        a for a in visible_agents
        if not a.get("isGuardian", False)
        and a.get("isAlive", True)
        and a.get("id") != my_id
    ]
    guardians_here = [
        a for a in visible_agents
        if a.get("isGuardian", False)
        and a.get("isAlive", True)
        and a.get("regionId") == region_id
    ]

    has_healing_items = any(
        isinstance(i, dict)
        and i.get("typeId", "").lower() in RECOVERY_ITEMS
        for i in inventory
    )

    # ─────────────────────────────────────────────────────────────
    # ════════════════  EARLY GAME PHASE  ═════════════════════════
    # ─────────────────────────────────────────────────────────────
    if early_game:
        # EG-Safety: DZ escape tetap aktif
        if region.get("isDeathZone", False):
            safe = _find_safe_region(connections, danger_ids, view)
            if safe and ep >= move_ep_cost:
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": "EARLY: DZ ESCAPE"}

        # EG-Safety: desperate flee tetap aktif
        if hp < 20 and not has_healing_items and (enemies_alive or guardians_here):
            if ep >= move_ep_cost:
                safe = _find_safe_region(connections, danger_ids, view)
                if safe:
                    _prepare_move(region_id)
                    return {"action": "move", "data": {"regionId": safe},
                            "reason": f"EARLY: DESPERATE FLEE HP={hp}"}

        # EG1. Pickup (boosted, region saat ini saja)
        pickup = _check_pickup(visible_items, inventory, region_id, hp, ep,
                               inv_cap=inv_cap, early_game=True)
        if pickup:
            return pickup

        # EG2. Equip terbaik
        equip = _check_equip(inventory, equipped, view, region_id, connections)
        if equip:
            return equip

        # EG3. Utility (map dulu kalau ada)
        util = _use_utility_item(inventory, hp, ep, alive_count)
        if util:
            if util.get("data", {}).get("itemType") == "map":
                _map_used_this_tick = True
            return util

        # ── Cooldown gate ─────────────────────────────────────────
        if not can_act:
            return None

        # EG4. Heal jika HP < 60
        if hp < 60:
            heal = _find_healing_item(inventory, critical=(hp < 25))
            if heal:
                return {"action": "use_item", "data": {"itemId": heal["id"]},
                        "reason": f"EARLY HEAL: HP={hp}"}

        # EG5. Gerak menuju item
        if ep >= move_ep_cost and connections:
            move_target, score = _choose_move_target(
                connections, danger_ids, region, visible_items,
                alive_count, enemies_alive, ep, early_game=True
            )
            if move_target and score > 0:
                log.info("🎒 EARLY MOVE: → %s (score=%d)", move_target[:8], score)
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move_target},
                        "reason": f"EARLY LOOT MOVE: score={score}"}

        # EG6. Rest
        if ep < max_ep and not region.get("isDeathZone"):
            return {"action": "rest", "data": {}, "reason": f"EARLY REST: EP={ep}/{max_ep}"}

        return None

    # ─────────────────────────────────────────────────────────────
    # ══════════════════  NORMAL GAME  ════════════════════════════
    # ─────────────────────────────────────────────────────────────

    # ── Priority 1: DEATHZONE ESCAPE ─────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 IN DEATH ZONE! Escaping → %s (HP=%d)", safe, hp)
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In death zone! HP={hp}"}

    # ── Priority 1b: Pre-escape pending DZ ───────────────────────
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("⚠️ Region becoming DZ! Pre-escaping → %s", safe)
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region becoming DZ"}

    # ── Priority 1c: DESPERATE FLEE ──────────────────────────────
    if hp < 20 and not has_healing_items and (enemies_alive or guardians_here):
        if ep >= move_ep_cost:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                log.warning("🆘 DESPERATE FLEE! HP=%d", hp)
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"DESPERATE FLEE: HP={hp}"}
        else:
            # Tidak bisa lari, serang target terlemah
            all_threats = enemies_alive + guardians_here
            nearest = _select_weakest_target(all_threats)
            if nearest and _is_in_range(nearest, region_id, w_range, connections):
                log.warning("🆘 DESPERATE ATTACK → %s", nearest.get("id", "?")[:8])
                return {"action": "attack",
                        "data": {"targetId": nearest["id"], "targetType": "agent"},
                        "reason": "DESPERATE ATTACK"}
            if not region.get("isDeathZone") and region_id not in danger_ids:
                log.warning("🆘 DESPERATE REST (HP=%d)", hp)
                return {"action": "rest", "data": {}, "reason": "DESPERATE REST"}

    # ── Priority 1d: COUNTER-ATTACK ──────────────────────────────
    # [FIX v2.0.0] Butuh HP >= 25 agar tidak mati saat harusnya heal
    if _combat_history.get("damage_this_tick") and hp >= 25:
        attacker_id = _combat_history["last_attacker_id"]
        if attacker_id == "unknown" or not attacker_id:
            attacker = enemies_alive[0] if enemies_alive else (
                guardians_here[0] if guardians_here else None)
        else:
            attacker = next(
                (a for a in visible_agents
                 if a.get("id") == attacker_id and a.get("isAlive", True)),
                None
            )
        if attacker:
            if _is_in_range(attacker, region_id, w_range, connections):
                my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                     attacker.get("def", 5), region_weather)
                log.warning("⚔️ COUNTER-ATTACK! → %s (dmg=%d)",
                            attacker.get("id", "?")[:8], my_dmg)
                _combat_history["damage_this_tick"] = False
                _current_target_id = attacker.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": attacker["id"], "targetType": "agent"},
                        "reason": "COUNTER-ATTACK: Just damaged"}
            else:
                if ep >= move_ep_cost:
                    move = _move_toward_target(attacker, connections, danger_ids, view)
                    if move:
                        log.info("🏃 CHASING attacker → %s", move[:8])
                        _combat_history["damage_this_tick"] = False
                        _current_target_id = attacker.get("id", "")
                        _prepare_move(region_id)
                        return {"action": "move", "data": {"regionId": move},
                                "reason": "CHASE: Pursuing attacker"}

    # ── Priority 2b: Guardian threat evasion ─────────────────────
    if guardians_here and ep >= move_ep_cost:
        threat_guardian = max(guardians_here, key=lambda g: g.get("atk", 10))
        g_dmg = calc_damage(threat_guardian.get("atk", 10),
                            _estimate_enemy_weapon_bonus(threat_guardian),
                            defense, region_weather)
        flee_hp_threshold = max(25, int(g_dmg * 1.5))
        if hp < flee_hp_threshold:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                log.warning("⚠️ Guardian threat! HP=%d < %d, fleeing",
                            hp, flee_hp_threshold)
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"GUARDIAN FLEE: HP={hp} < {flee_hp_threshold}"}

    # ── Priority 3: FREE ACTIONS ──────────────────────────────────
    # [FIX v2.0.0] Pickup tidak fallback ke region lain
    pickup = _check_pickup(visible_items, inventory, region_id, hp, ep,
                           inv_cap=inv_cap, early_game=False)
    if pickup:
        return pickup

    equip = _check_equip(inventory, equipped, view, region_id, connections)
    if equip:
        return equip

    # ── Priority 3b: Utility items ────────────────────────────────
    util = _use_utility_item(inventory, hp, ep, alive_count)
    if util:
        if util.get("data", {}).get("itemType") == "map":
            _map_used_this_tick = True
        return util

    # ════════════════  COOLDOWN GATE  ════════════════════════════
    if not can_act:
        return None

    # ── Priority 4: Critical healing ─────────────────────────────
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

    # ── Priority 4b: EP management ────────────────────────────────
    energy_drink_count = sum(
        1 for i in inventory
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink"
    )
    if ep <= 2 and ep < max_ep:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]},
                    "reason": f"EP RECOVERY: EP={ep}/{max_ep}"}
    elif energy_drink_count >= 2 and ep <= max_ep - 4:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]},
                    "reason": f"AGGRESSIVE EP RECOVERY: EP={ep}/{max_ep}"}

    # ── Priority 4c: Binoculars ranged harassment ─────────────────
    has_binos = any(
        isinstance(i, dict) and i.get("typeId", "").lower() == "binoculars"
        for i in inventory
    )
    if has_binos and w_range >= 1 and enemies_alive and hp >= 30:
        for enemy in enemies_alive:
            if _is_in_range(enemy, region_id, w_range, connections):
                my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                     enemy.get("def", 5), region_weather)
                e_dmg  = calc_damage(enemy.get("atk", 10),
                                     _estimate_enemy_weapon_bonus(enemy),
                                     defense, region_weather)
                if my_dmg > e_dmg or enemy.get("hp", 100) <= my_dmg * 2:
                    log.info("🔭 Binoculars harassment → %s", enemy.get("id", "")[:8])
                    _current_target_id = enemy.get("id", "")
                    return {"action": "attack",
                            "data": {"targetId": enemy["id"], "targetType": "agent"},
                            "reason": "BINOCULARS RANGED HARASSMENT"}
                break

    # ── [AGGRESSIVE] Priority 4c+: Favorable agent combat (early)  ──
    # Lower threshold dan lebih agresif, dipindah ke atas sebelum third-party
    if enemies_alive and ep >= 2 and hp >= 15:   # [AGGRESSIVE] HP threshold turun
        target = _select_best_combat_target(
            enemies_alive, atk, equipped, defense, region_weather,
            recent_logs=recent_logs, prefer_id=_current_target_id
        )
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            e_dmg  = calc_damage(target.get("atk", 10),
                                 _estimate_enemy_weapon_bonus(target),
                                 defense, region_weather)
            hits          = math.ceil(target.get("hp", 100) / max(my_dmg, 1))
            proj_dmg_taken = hits * e_dmg

            # Lebih agresif: my_dmg >= 0.8 * e_dmg (sebelumnya > e_dmg)
            if my_dmg >= e_dmg * 0.8:
                _current_target_id = target.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"AGGRESSIVE COMBAT: dmg={my_dmg} vs {e_dmg}"}
            # Finish off target yang hampir mati (tetap)
            elif target.get("hp", 100) <= my_dmg * 2 and hp - proj_dmg_taken > 10:
                _current_target_id = target.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"FINISH OFF: target HP={target.get('hp')}"}
            # Ranged attack threshold diturunkan (0.7 dari 0.85)
            elif w_range >= 1 and my_dmg >= e_dmg * 0.7:
                _current_target_id = target.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"RANGED ATTACK: range={w_range}"}

    # ── Priority 4d: Third-Party Cleanup ─────────────────────────
    if hp >= 60 and ep >= move_ep_cost + 2 and alive_count > 5:
        nearby_fight = _detect_nearby_fight(
            recent_logs, my_id, view, connections, danger_ids, region_id, alive_count
        )
        if nearby_fight:
            fake_target = {"regionId": nearby_fight}
            move = _move_toward_target(fake_target, connections, danger_ids, view)
            if move:
                log.info("🥷 Third-party → %s via %s", nearby_fight[:8], move[:8])
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move},
                        "reason": f"THIRD-PARTY: Fight at {nearby_fight[:8]}"}

    # ── Priority 5: Guardian farming ──────────────────────────────
    guardians_all = [
        a for a in visible_agents
        if a.get("isGuardian", False) and a.get("isAlive", True)
    ]
    if guardians_all and ep >= 2 and hp >= 35:
        target = _select_best_combat_target(
            guardians_all, atk, equipped, defense, region_weather,
            recent_logs=recent_logs
        )
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            g_dmg  = calc_damage(target.get("atk", 10),
                                 _estimate_enemy_weapon_bonus(target),
                                 defense, region_weather)
            if my_dmg >= g_dmg or target.get("hp", 100) <= my_dmg * 3:
                _current_target_id = target.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: dmg={my_dmg}"}
        else:
            # Cek flee-threshold sebelum approach
            t_dmg = calc_damage(target.get("atk", 10),
                                _estimate_enemy_weapon_bonus(target),
                                defense, region_weather)
            safe_hp = max(35, int(t_dmg * 1.5))
            if ep >= move_ep_cost and hp >= safe_hp:
                move = _move_toward_target(target, connections, danger_ids, view)
                if move:
                    _prepare_move(region_id)
                    return {"action": "move", "data": {"regionId": move},
                            "reason": "APPROACH GUARDIAN"}

    # ── Priority 6: Endgame hunt (alive <= 15) ─────────────────────
    # [AGGRESSIVE] alive threshold 15, HP threshold 15
    if alive_count <= 15 and enemies_alive and ep >= 2 and hp >= 15:
        target = _select_best_combat_target(
            enemies_alive, atk, equipped, defense, region_weather,
            recent_logs=recent_logs, prefer_id=_current_target_id
        )
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            e_dmg  = calc_damage(target.get("atk", 10),
                                 _estimate_enemy_weapon_bonus(target),
                                 defense, region_weather)
            hits   = math.ceil(target.get("hp", 100) / max(my_dmg, 1))
            proj_dmg_taken = hits * e_dmg
            if my_dmg >= e_dmg * 0.7 or target.get("hp", 100) <= FINISH_OFF_HP_THRESHOLD:
                if hp - proj_dmg_taken > 0 or target.get("hp", 100) <= my_dmg:
                    _current_target_id = target.get("id", "")
                    return {"action": "attack",
                            "data": {"targetId": target["id"], "targetType": "agent"},
                            "reason": f"ENDGAME HUNT: alive={alive_count}"}
        else:
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost:
                _current_target_id = target.get("id", "")
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move},
                        "reason": "ENDGAME CHASE"}

    # ── Priority 6b: Secondary favorable combat (dengan projected damage) ─
    # Tetap dipertahankan untuk kondisi yang tidak masuk early aggressive block
    hp_threshold = 15 if alive_count > 20 else 15   # [AGGRESSIVE] turunkan semua
    if enemies_alive and ep >= 2 and hp >= hp_threshold:
        target = _select_best_combat_target(
            enemies_alive, atk, equipped, defense, region_weather,
            recent_logs=recent_logs, prefer_id=_current_target_id
        )
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            e_dmg  = calc_damage(target.get("atk", 10),
                                 _estimate_enemy_weapon_bonus(target),
                                 defense, region_weather)
            hits          = math.ceil(target.get("hp", 100) / max(my_dmg, 1))
            proj_dmg_taken = hits * e_dmg

            if my_dmg > e_dmg and hp - proj_dmg_taken > 15:
                _current_target_id = target.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"COMBAT: dmg={my_dmg} vs {e_dmg}"}
            elif target.get("hp", 100) <= my_dmg * 2 and hp - proj_dmg_taken > 10:
                _current_target_id = target.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"FINISH OFF: target HP={target.get('hp')}"}
            elif w_range >= 1 and my_dmg >= e_dmg * 0.7:
                _current_target_id = target.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"RANGED ATTACK: range={w_range}"}

    # ── Priority 7: Monster farming ──────────────────────────────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2 and hp >= 25:
        target = _select_best_combat_target(
            monsters, atk, equipped, defense, region_weather
        )
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            m_dmg  = calc_damage(target.get("atk", 10), 0, defense, region_weather)
            if my_dmg >= m_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "monster"},
                        "reason": "MONSTER FARM"}

    # ── Priority 7b: Heal when safe ───────────────────────────────
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

    # ── Priority 8: Facility interaction ─────────────────────────
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep, alive_count)
        if facility:
            return {"action": "interact",
                    "data": {"interactableId": facility["id"]},
                    "reason": f"FACILITY: {facility.get('type')}"}

    # ── Priority 8.5: Endgame Camping ────────────────────────────
    if (alive_count <= 5
            and _map_knowledge.get("revealed")
            and region_id in _map_knowledge.get("safe_center", [])):
        if not enemies_alive and not guardians_here and ep < max_ep:
            log.info("🏕️ Endgame Camping (alive=%d)", alive_count)
            return {"action": "rest", "data": {},
                    "reason": f"ENDGAME CAMP: safe centre, EP={ep}/{max_ep}"}

    # ── Priority 8.6: Hold Position ──────────────────────────────
    if not enemies_alive and not guardians_here and ep < max_ep:
        all_explored = all(
            (c if isinstance(c, str) else c.get("id", "")) in _explored_regions
            for c in connections
        )
        no_items_here = not any(
            it.get("regionId") == region_id for it in visible_items
        )
        if all_explored and no_items_here and not region.get("isDeathZone"):
            log.info("🛑 Hold Position: resting")
            return {"action": "rest", "data": {}, "reason": "HOLD: No incentive to move"}

    # ── Priority 9: Strategic movement ───────────────────────────
    if ep >= move_ep_cost and connections:
        move_target, best_score = _choose_move_target(
            connections, danger_ids, region, visible_items,
            alive_count, enemies_alive, ep, early_game=False
        )
        if move_target and best_score > 0:
            log.info("🚶 Moving → %s (score=%d)", move_target[:8], best_score)
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "EXPLORE"}

    # ── Priority 10: Rest ─────────────────────────────────────────
    if (ep < 3
            and not enemies_alive
            and not guardians_here
            and not region.get("isDeathZone")
            and region_id not in danger_ids):
        return {"action": "rest", "data": {}, "reason": f"REST: EP={ep}/{max_ep}"}

    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Equip
# ─────────────────────────────────────────────────────────────────────

def _check_equip(inventory: list, equipped, view: dict,
                 region_id: str, connections=None) -> dict | None:
    """
    Smart weapon swap dengan anti-loop fix (v1.9.2).
    [FIX v2.0.0] _is_enemy_nearby dikurangi jadi 1-hop.
    """
    nearby_enemy = _is_enemy_nearby(view, region_id, connections)
    current_type = equipped.get("typeId", "").lower() if equipped else "fist"
    current_id   = equipped.get("id", "") if equipped else ""

    best       = None
    best_score = get_weapon_bonus(equipped)

    for item in inventory:
        if not isinstance(item, dict) or item.get("category") != "weapon":
            continue
        if item.get("id") == current_id:
            continue
        if item.get("typeId", "").lower() == current_type:
            continue

        type_id = item.get("typeId", "").lower()
        bonus   = WEAPONS.get(type_id, {}).get("bonus", 0)
        rng     = WEAPONS.get(type_id, {}).get("range", 0)
        score   = bonus
        if nearby_enemy and rng >= 1:
            score += 40
        if score > best_score:
            best_score = score
            best = item

    if best:
        new_bonus = WEAPONS.get(best.get("typeId", "").lower(), {}).get("bonus", 0)
        log.info("EQUIP: %s (+%d ATK) chosen (nearby enemy=%s)",
                 best.get("typeId"), new_bonus, nearby_enemy)
        return {"action": "equip",
                "data": {"itemId": best["id"]},
                "reason": f"SMART EQUIP: {best.get('typeId', 'weapon')}"}
    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Enemy nearby (1-hop only)
# ─────────────────────────────────────────────────────────────────────

def _is_enemy_nearby(view: dict, my_region: str, connections) -> bool:
    """
    [FIX v2.0.0] Hanya cek 1-hop (sebelumnya 2-hop, terlalu agresif untuk
    scoring senjata ranged).
    """
    visible = view.get("visibleAgents", [])
    adj_ids: set = set()
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
# Helper — Pickup
# ─────────────────────────────────────────────────────────────────────

def _check_pickup(items: list, inventory: list, region_id: str,
                  hp: int = 100, ep: int = 10,
                  inv_cap: int = DEFAULT_INVENTORY_CAP,
                  early_game: bool = False) -> dict | None:
    """
    [FIX v2.0.0] Tidak fallback ke item di region lain.
    [NEW] early_game=True menerapkan boost score.
    """
    local_items = [
        i for i in items
        if isinstance(i, dict) and i.get("id") and i.get("regionId") == region_id
    ]
    if not local_items:
        return None  # tidak ada item di region ini → skip

    heal_count = sum(
        1 for i in inventory
        if isinstance(i, dict)
        and i.get("typeId", "").lower() in RECOVERY_ITEMS
    )

    def score_fn(item):
        base = _pickup_score(item, inventory, heal_count, hp, ep)
        if early_game and base > 0:
            type_id = item.get("typeId", "").lower()
            cat     = item.get("category", "").lower()
            if cat == "weapon" or type_id in RECOVERY_ITEMS:
                return base + EARLY_GAME_ITEM_BOOST
            return base + EARLY_GAME_ITEM_BOOST // 2
        return base

    local_items.sort(key=score_fn, reverse=True)
    best  = local_items[0]
    score = score_fn(best)

    if score <= 0:
        return None

    type_id = best.get("typeId", "item")
    if len(inventory) >= inv_cap:
        drop = _find_droppable_item(inventory, best)
        if drop:
            prefix = "EARLY " if early_game else ""
            log.info("%sPICKUP: drop %s untuk %s",
                     prefix, drop.get("typeId"), type_id)
            return {"action": "drop_item",
                    "data": {"itemId": drop["id"]},
                    "reason": f"{prefix}MAKE ROOM for {type_id}"}
        return None

    prefix = "EARLY " if early_game else ""
    log.info("%sPICKUP: %s (score=%d)", prefix, type_id, score)
    return {"action": "pickup",
            "data": {"itemId": best["id"]},
            "reason": f"{prefix}PICKUP: {type_id}"}


def _pickup_score(item: dict, inventory: list, heal_count: int,
                  hp: int = 100, ep: int = 10) -> int:
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
        return (100 + bonus) if bonus > current_best else 0

    dynamic_bonus = 0
    need_healing  = (hp < 50) and (type_id in RECOVERY_ITEMS)
    need_energy   = (ep < 4)  and (type_id == "energy_drink")
    if need_healing or need_energy:
        dynamic_bonus = 100

    if type_id == "binoculars":
        has_binos = any(
            isinstance(i, dict) and i.get("typeId", "").lower() == "binoculars"
            for i in inventory
        )
        return 55 if not has_binos else 0

    if type_id == "map":
        return 52

    if type_id in RECOVERY_ITEMS:
        # [FIX v2.0.0] Cap healing score saat HP & stok healing sudah cukup
        if heal_count >= 3 and hp >= 70:
            return 20
        return ITEM_PRIORITY.get(type_id, 0) + (10 if heal_count < 4 else 0) + dynamic_bonus

    if type_id == "energy_drink":
        return ITEM_PRIORITY.get(type_id, 0) + dynamic_bonus

    return ITEM_PRIORITY.get(type_id, 0)


# ─────────────────────────────────────────────────────────────────────
# Helper — Movement
# ─────────────────────────────────────────────────────────────────────

def _choose_move_target(connections, danger_ids: set, current_region: dict,
                        visible_items: list, alive_count: int,
                        enemies_visible: list = None, current_ep: int = 999,
                        early_game: bool = False):
    """
    Returns (region_id, score). score <= 0 berarti tidak ada tujuan yang worthwhile.
    [FIX v2.0.0] alive_count < 30 bonus dihapus (selalu true, jadi noise).
    [NEW] early_game=True: boost item region, bonus ruins.
    """
    global _explored_regions, _known_agents, _last_region_id

    candidates  = []
    item_regions = {
        item.get("regionId", "") for item in visible_items
        if isinstance(item, dict)
    }
    enemy_regions    = set()
    enemy_threat_map = {}

    if enemies_visible:
        for e in enemies_visible:
            reg  = e.get("regionId", "")
            e_id = e.get("id", "")
            enemy_regions.add(reg)
            agent_data = _known_agents.get(e_id, {})
            threat     = agent_data.get("atk", e.get("atk", 10)) + \
                         agent_data.get("def", e.get("def", 5))
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
            score  += {"hills": 4, "plains": 2, "ruins": 2, "forest": 1,
                       "water": -3}.get(terrain, 0)
            score  += {"clear": 1, "rain": 0, "fog": -1,
                       "storm": -2}.get(weather, 0)
            if weather == "storm" and current_ep < 4:
                score -= 100

            facs   = conn_dict.get("interactables", [])
            unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
            score += len(unused) * 2

        if rid in item_regions:
            score += 5 if not early_game else 5 + EARLY_GAME_MOVE_BOOST

        if early_game and conn_dict:
            terrain = conn_dict.get("terrain", "").lower()
            if terrain == "ruins":
                score += 4   # ruins biasanya banyak loot

        if rid in enemy_regions and alive_count <= 15 and not early_game:  # [AGGRESSIVE] 15 instead of 10
            score += 4
        if alive_count > 10 and not early_game:
            threat_level = enemy_threat_map.get(rid, 0)
            if threat_level > 25:
                score -= 50
            elif threat_level > 0:
                score -= max(0, threat_level * 2)

        if _map_knowledge.get("revealed") and \
                rid in _map_knowledge.get("safe_center", []):
            score += 5

        if rid in _explored_regions:
            penalty = 3 if early_game else 5
            score  -= penalty

        if rid == _last_region_id:
            score -= 50

        candidates.append((rid, score))

    if not candidates:
        return None, -999

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0], candidates[0][1]


# ─────────────────────────────────────────────────────────────────────
# Helper — Combat target selection
# ─────────────────────────────────────────────────────────────────────

def _select_best_combat_target(targets: list, my_atk: int, equipped,
                                my_def: int, weather: str,
                                recent_logs: list = None,
                                prefer_id: str = "") -> dict:
    """
    Pilih target terbaik berdasarkan kill_speed - threat score.
    [NEW] prefer_id: prioritaskan target yang sedang dikejar (persistent target).
    [NEW] bonus finish-off untuk target HP <= FINISH_OFF_HP_THRESHOLD.
    [AGGRESSIVE] Bonus +20 untuk player (bukan guardian).
    """
    global _vulnerable_agents
    best       = None
    best_score = -9999
    my_bonus   = get_weapon_bonus(equipped)

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

        # [AGGRESSIVE] Bonus untuk player (bukan guardian)
        if not t.get("isGuardian", False):
            score += 20

        # Bonus: target sedang vulnerable (habis heal)
        if t_id in _vulnerable_agents:
            score += 30
        # Bonus: target sedang fighting someone else
        if recent_logs and _is_agent_fighting(t_id, recent_logs):
            score += 25
        # [NEW] Bonus: finish-off target hampir mati
        if t_hp <= FINISH_OFF_HP_THRESHOLD:
            score += FINISH_OFF_BONUS
        # [NEW] Bonus: persistent target consistency
        if prefer_id and t_id == prefer_id:
            score += 15

        if score > best_score:
            best_score = score
            best = t

    return best if best else min(targets, key=lambda t: t.get("hp", 999))


def _select_weakest_target(targets: list) -> dict | None:
    """[NEW] Pilih target HP terendah — untuk desperate attack."""
    if not targets:
        return None
    return min(targets, key=lambda t: t.get("hp", 999))


# ─────────────────────────────────────────────────────────────────────
# Helper — Agent tracking
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


# ─────────────────────────────────────────────────────────────────────
# Helper — Utility items
# ─────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────
# Helper — Map learning
# ─────────────────────────────────────────────────────────────────────

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
            conns   = region.get("connections", [])
            terrain = region.get("terrain", "").lower()
            terrain_value = {
                "hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1
            }.get(terrain, 0)
            score = len(conns) + terrain_value
            safe_regions.append((rid, score))
    safe_regions.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]
    log.info("🗺️ MAP LEARNED: %d DZ, top centre: %s",
             len(_map_knowledge["death_zones"]),
             _map_knowledge["safe_center"][:3])


# ─────────────────────────────────────────────────────────────────────
# Helper — Item management
# ─────────────────────────────────────────────────────────────────────

def _find_droppable_item(inventory: list, target_item: dict) -> dict | None:
    target_score = ITEM_DROP_VALUE.get(target_item.get("typeId", "").lower(), 1)
    candidates   = []
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


def _find_healing_item(inventory: list, critical: bool = False) -> dict | None:
    heals = [
        i for i in inventory
        if isinstance(i, dict)
        and i.get("typeId", "").lower() in RECOVERY_ITEMS
    ]
    if not heals:
        return None
    heals.sort(
        key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0),
        reverse=critical
    )
    return heals[0]


def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Facility
# ─────────────────────────────────────────────────────────────────────

def _select_facility(interactables: list, hp: int, ep: int,
                     alive_count: int) -> dict | None:
    best          = None
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
            best          = fac
            best_priority = priority
    return best


# ─────────────────────────────────────────────────────────────────────
# Helper — Range & movement
# ─────────────────────────────────────────────────────────────────────

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
    # 2-hop path
    best_hop = None
    for step1_id in safe_conn_ids:
        step1_region = _visible_region_cache.get(step1_id)
        if not step1_region:
            continue
        for step2 in step1_region.get("connections", []):
            step2_id = step2 if isinstance(step2, str) else step2.get("id", "")
            if step2_id == target_region:
                if best_hop is None or step1_id not in _explored_regions:
                    best_hop = step1_id
                break
    if best_hop:
        log.info("🧭 2-hop: %s → %s → target", best_hop[:8], target_region[:8])
        return best_hop
    return safe_conn_ids[0] if safe_conn_ids else None


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
                score   = {"hills": 3, "plains": 2, "ruins": 1,
                           "forest": 0, "water": -2}.get(terrain, 0)
                safe_regions.append((rid, score))
    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]
    for conn in connections:
        rid   = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            log.warning("No fully safe region! Fallback: %s", rid[:8])
            return rid
    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Combat history & detection
# ─────────────────────────────────────────────────────────────────────

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
                attacker_id = (log_entry.get("attackerId")
                               or log_entry.get("sourceId") or "")
                target_id   = log_entry.get("targetId") or ""
                if target_id == my_id and attacker_id and attacker_id != my_id:
                    _combat_history["last_attacker_id"] = attacker_id
                    attacker_found = True
                    log.info("🩸 DAMAGE from %s — HP: %d → %d",
                             attacker_id[:8], last, current_hp)
                    break
        if not attacker_found:
            _combat_history["last_attacker_id"] = "unknown"
            log.info("🩸 DAMAGE detected, attacker unknown")
    else:
        _combat_history["consecutive_damage_ticks"] = 0
        _combat_history["damage_this_tick"] = False
    _combat_history["last_hp"] = current_hp


def _detect_vulnerable_agents(recent_logs: list, my_id: str):
    global _vulnerable_agents
    _vulnerable_agents = {
        k: v for k, v in _vulnerable_agents.items()
        if _tick_counter - v < VULNERABLE_TTL
    }
    for entry in recent_logs:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "use_item":
            user_id   = entry.get("agentId") or entry.get("userId", "")
            item_name = entry.get("itemName", "").lower()
            if any(h in item_name for h in ("medkit", "bandage", "food", "emergency")):
                if user_id and user_id != my_id:
                    _vulnerable_agents[user_id] = _tick_counter
                    log.info("🎯 Marked %s as vulnerable (healed)", user_id[:8])


def _detect_nearby_fight(recent_logs: list, my_id: str, view: dict,
                          connections: list, danger_ids: set,
                          region_id: str, alive_count: int) -> str | None:
    global _visible_region_cache
    fight_region = None
    for entry in recent_logs:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type", "")
        if etype in ("attack", "damage"):
            attacker = entry.get("attackerId") or entry.get("sourceId") or ""
            target   = entry.get("targetId") or ""
            if attacker == my_id or target == my_id:
                continue
            if attacker and target:
                visible = view.get("visibleAgents", [])
                for agent in visible:
                    if agent.get("id") in (attacker, target) and agent.get("isAlive", True):
                        fight_region = agent.get("regionId", "")
                        break
                if fight_region:
                    break
    if not fight_region or fight_region == region_id or fight_region in danger_ids:
        return None
    # Cek reachable (1 atau 2 hop)
    reachable = False
    for conn in connections:
        cid = conn if isinstance(conn, str) else conn.get("id", "")
        if cid == fight_region:
            reachable = True
            break
    if not reachable:
        for conn in connections:
            cid = conn if isinstance(conn, str) else conn.get("id", "")
            if cid not in danger_ids:
                region_obj = _visible_region_cache.get(cid)
                if region_obj:
                    for c2 in region_obj.get("connections", []):
                        c2id = c2 if isinstance(c2, str) else c2.get("id", "")
                        if c2id == fight_region:
                            reachable = True
                            break
                if reachable:
                    break
    return fight_region if reachable else None


def _is_agent_fighting(agent_id: str, recent_logs: list) -> bool:
    for entry in recent_logs:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") in ("attack", "damage"):
            if (entry.get("attackerId") == agent_id
                    or entry.get("sourceId") == agent_id
                    or entry.get("targetId") == agent_id):
                return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Helper — Misc
# ─────────────────────────────────────────────────────────────────────

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