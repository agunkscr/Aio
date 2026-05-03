"""
Strategy brain v3.0.0 — Hyper Aggro Edition.

Perubahan dari v2.1.0:
──────────────────────────────────────────────────────────────────────
[AGGRO] THREAT_WEIGHT: 0.8 → 0.4 — bot lebih berani ngambil risiko
[AGGRO] PLAYER_BONUS: 20 → 150 — selalu prioritas player vs guardian
[AGGRO] FINISH_OFF_HP_THRESHOLD: 25 → 45 — lebih agresif kejar KO
[AGGRO] FINISH_OFF_BONUS: 70 → 130 — nilai finish-off jauh lebih tinggi
[AGGRO] LOOTING_HUNTER_BONUS: 80 → 50 — looter = target utama
[AGGRO] EP_MOVE_MIN: 3 → 2 — bergerak lebih agresif
[AGGRO] EP_REST_UNTIL: 5 → 3 — tidak rebahan, cukup EP langsung jalan
[AGGRO] Combat ratio P4c: 0.8 → 0.55 — serang bahkan kalau sedikit kalah
[AGGRO] Combat ratio P6b: > e_dmg → >= e_dmg*0.75, margin HP 15→5
[AGGRO] Ranged condition P6b: 0.75 → 0.5 — tembak bahkan tidak ideal
[AGGRO] Endgame hunt: alive<=15 → alive<=20, HP min 15→10, ratio 0.7→0.5
[AGGRO] Guardian farm: HP min 35→20, ratio >=g_dmg → >=g_dmg*0.7
[AGGRO] EG self-defense: HP>=40 → HP>=20, ratio 0.8 → 0.55
[NEW]   P4d: Proactive Player Hunt — kejar player tanpa harus endgame
        (jika punya senjata bagus & HP cukup, langsung hajar)
[NEW]   LOOTING movement bonus: +15 → +35
[NEW]   Enemy region pull (non-looting): +2 → +8 di endgame
[NEW]   Counter-attack ratio check dihapus — balas tanpa syarat damage
──────────────────────────────────────────────────────────────────────
"""

import math
from collections import deque
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

# ── Item priority & drop value ────────────────────────────────────────
ITEM_PRIORITY = {
    "rewards":        300,
    "katana":         100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger":          80, "bow": 75,
    "medkit":          70, "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars":      55,
    "map":             52,
    "megaphone":       40,
}

ITEM_DROP_VALUE = {
    "rewards":        -1,
    "katana":         10,  "sniper": 9.5, "sword": 9,  "pistol": 8.5,
    "dagger":          8,  "bow": 7.5,
    "medkit":          7,  "bandage": 6.5, "emergency_food": 6, "energy_drink": 5.8,
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
    "clear": 0.0, "rain": 0.05, "fog": 0.10, "storm": 0.15,
}

# ── Tuning constants ──────────────────────────────────────────────────
AGENT_STALE_TICKS       = 20
MEGAPHONE_MIN_EP        = 4
THREAT_WEIGHT           = 0.4   # [AGGRO] lebih berani ngambil risiko
VULNERABLE_TTL          = 2
DEFAULT_INVENTORY_CAP   = 10
FINISH_OFF_HP_THRESHOLD = 45
FINISH_OFF_BONUS        = 130

# [AGGRO] Looting hunter bonus — player looting = target utama
LOOTING_HUNTER_BONUS    = 50
PLAYER_BONUS            = 150   # [AGGRO] player >> guardian

# ── Early game constants ──────────────────────────────────────────────
EARLY_GAME_TICKS        = 30
EARLY_GAME_MIN_ALIVE    = 5    # [FIX] 15→5
EARLY_GAME_ITEM_BOOST   = 80
EARLY_GAME_MOVE_BOOST   = 10
EARLY_GAME_MOVE_EP_MIN  = 2    # [FIX] cek EP sebelum move di early game

# ── EP recovery constants ─────────────────────────────────────────────
EP_MOVE_MIN   = 2   # [AGGRO] bergerak lebih agresif
EP_REST_UNTIL = 3   # [AGGRO] tidak rebahan, cukup EP langsung jalan

# ── Global state ──────────────────────────────────────────────────────
_game_id:            str  = None
_tick_counter:       int  = 0
_known_agents:       dict = {}
_map_knowledge:      dict = {"revealed": False, "death_zones": set(), "safe_center": []}
_combat_history:     dict = {
    "last_hp": 100, "consecutive_damage_ticks": 0,
    "last_attacker_id": "", "damage_this_tick": False,
}
_explored_regions:   set  = set()
_map_used_this_tick: bool = False
_map_item_used_ids:  set  = set()
_vulnerable_agents:  dict = {}
_visible_region_cache: dict[str, dict] = {}
_last_region_id:     str  = ""
_current_target_id:  str  = ""
_recent_regions:   deque  = deque(maxlen=6)   # [FIX] anti ping-pong multi-region


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
    return WEAPONS.get(equipped_weapon.get("typeId", "").lower(), {}).get("bonus", 0)


def get_weapon_range(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    return WEAPONS.get(equipped_weapon.get("typeId", "").lower(), {}).get("range", 0)


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


# ─────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────

def reset_game_state():
    global _known_agents, _map_knowledge, _combat_history, _explored_regions
    global _map_used_this_tick, _map_item_used_ids, _tick_counter
    global _vulnerable_agents, _visible_region_cache, _last_region_id
    global _current_target_id, _recent_regions, _game_id
    _tick_counter         = 0
    _known_agents         = {}
    _map_knowledge        = {"revealed": False, "death_zones": set(), "safe_center": []}
    _combat_history       = {
        "last_hp": 100, "consecutive_damage_ticks": 0,
        "last_attacker_id": "", "damage_this_tick": False,
    }
    _explored_regions     = set()
    _map_used_this_tick   = False
    _map_item_used_ids    = set()
    _vulnerable_agents    = {}
    _visible_region_cache = {}
    _last_region_id       = ""
    _current_target_id    = ""
    _recent_regions       = deque(maxlen=6)
    log.info("Brain reset → v3.0.0 Hyper Aggro")


def _prepare_move(from_region: str):
    global _last_region_id, _recent_regions
    _recent_regions.append(from_region)
    _last_region_id = from_region


def _is_early_game(alive_count: int) -> bool:
    return _tick_counter <= EARLY_GAME_TICKS and alive_count > EARLY_GAME_MIN_ALIVE


# ─────────────────────────────────────────────────────────────────────
# Main decision engine
# ─────────────────────────────────────────────────────────────────────

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision engine v2.1.0 — Looting Hunter Edition.

    Priority chain:
    ─── EARLY GAME (tick<=30, alive>5) ───
    EG-S1. DZ escape
    EG-S2. Desperate flee (HP<20, no heals)
    EG-S3. Self-defense counter (HP>=20, diserang, semi-favorable)
    EG1.   Pickup (boosted, local only)
    EG2.   Equip terbaik
    EG3.   Utility + Supply/Medical facility
    [cooldown]
    EG4.   Heal (HP<60)
    EG5.   Move toward items (EP>=2)
    EG6.   Rest

    ─── NORMAL GAME ───
    1.   DZ escape
    1b.  Pre-escape pending DZ
    1c.  Desperate flee
    1d.  Counter-attack (HP>=25, NO damage ratio check)
    2b.  Guardian threat evasion
    3.   Pickup (local), Equip, Utility [free]
    [cooldown]
    4.   Heal (HP<25 critical / HP<60 normal)
    4b.  EP management
    4c.  LOOTING HUNTER → attack/chase player yang sedang looting
    4d.  PROACTIVE HUNT → kejar player terdekat bila senjata superior
    5.   Guardian farming (agresif)
    6.   Endgame hunt (alive<=100 = SELALU AKTIF)
    6b.  Favorable combat (threshold diturunkan)
    7.   Monster farming
    7b.  Heal when safe (HP<75)
    8.   Facility (supply_cache, medical, watchtower)
    9.   Strategic movement (EP>=EP_MOVE_MIN)
    10.  Rest (EP<EP_REST_UNTIL)
    """

    global _game_id, _map_used_this_tick, _explored_regions, _tick_counter
    global _vulnerable_agents, _visible_region_cache, _map_item_used_ids
    global _last_region_id, _current_target_id, _recent_regions

    new_game_id = view.get("gameId", "")
    if new_game_id and new_game_id != _game_id:
        reset_game_state()
        _game_id = new_game_id

    _tick_counter += 1

    self_data  = view.get("self", {})
    region     = view.get("currentRegion", {})
    hp         = self_data.get("hp", 100)
    ep         = self_data.get("ep", 10)
    max_ep     = self_data.get("maxEp", 10)
    atk        = self_data.get("atk", 10)
    defense    = self_data.get("def", 5)
    is_alive   = self_data.get("isAlive", True)
    inventory  = self_data.get("inventory", [])
    equipped   = self_data.get("equippedWeapon")
    my_id      = self_data.get("id", "")
    inv_cap    = self_data.get("inventoryCapacity", DEFAULT_INVENTORY_CAP)

    visible_agents    = view.get("visibleAgents", [])
    visible_monsters  = view.get("visibleMonsters", [])
    visible_items_raw = view.get("visibleItems", [])
    visible_regions   = view.get("visibleRegions", [])
    connected_regions = view.get("connectedRegions", [])
    pending_dz        = view.get("pendingDeathzones", [])
    alive_count       = view.get("aliveCount", 100)
    recent_logs       = view.get("recentLogs", [])

    if not is_alive:
        return None

    early_game = _is_early_game(alive_count)
    if early_game and _tick_counter == 1:
        log.info("🎮 EARLY GAME: looting phase (tick=%d, alive=%d)", EARLY_GAME_TICKS, alive_count)
    elif not early_game and _tick_counter == EARLY_GAME_TICKS + 1:
        log.info("⚔️ EXIT EARLY → combat mode (tick=%d, alive=%d)", _tick_counter, alive_count)

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

    connections   = connected_regions or region.get("connections", [])
    interactables = region.get("interactables", [])
    region_id     = region.get("id", "")
    r_terrain     = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    r_weather     = region.get("weather", "").lower() if isinstance(region, dict) else ""

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

    move_ep_cost = _get_move_ep_cost(r_terrain, r_weather)
    w_range      = get_weapon_range(equipped)

    enemies_alive = [
        a for a in visible_agents
        if not a.get("isGuardian", False) and a.get("isAlive", True) and a.get("id") != my_id
    ]
    guardians_here = [
        a for a in visible_agents
        if a.get("isGuardian", False) and a.get("isAlive", True) and a.get("regionId") == region_id
    ]
    has_heals = any(
        isinstance(i, dict) and i.get("typeId", "").lower() in RECOVERY_ITEMS
        for i in inventory
    )

    # [NEW] Region yang ada item visible — kunci looting hunter
    looting_region_ids = {
        item.get("regionId", "") for item in visible_items
        if isinstance(item, dict) and item.get("regionId")
    }
    # Player yang sedang ada di region berisi item = sedang looting
    looting_players = [
        e for e in enemies_alive
        if e.get("regionId") in looting_region_ids
    ]

    # ─────────────────────────────────────────────────────────────
    # ════════════════  EARLY GAME  ═══════════════════════════════
    # ─────────────────────────────────────────────────────────────
    if early_game:
        # EG-S1: DZ escape
        if region.get("isDeathZone", False):
            safe = _find_safe_region(connections, danger_ids, view)
            if safe and ep >= move_ep_cost:
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe}, "reason": "EARLY: DZ ESCAPE"}

        # ── Situational awareness: hitung distinct attacker dari log ──
        distinct_attackers = {
            entry.get("attackerId") or entry.get("sourceId", "")
            for entry in recent_logs
            if isinstance(entry, dict)
            and entry.get("type") in ("damage", "attack")
            and entry.get("targetId") == my_id
            and (entry.get("attackerId") or entry.get("sourceId", ""))
        }
        consecutive = _combat_history.get("consecutive_damage_ticks", 0)
        under_heavy_fire = len(distinct_attackers) >= 2 or consecutive >= 2

        # EG-S2: Flee — diserang banyak orang, atau HP kritis, atau sudah kena
        #         berturut-turut meski punya heal (heal tidak akan cukup)
        flee_triggered = (
            (under_heavy_fire and (enemies_alive or guardians_here))
            or (hp < 30 and (enemies_alive or guardians_here))
            or (hp < 20 and not has_heals and (enemies_alive or guardians_here))
        )
        if flee_triggered and ep >= move_ep_cost:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                log.info("🏃 EARLY FLEE: HP=%d, attackers=%d, consec=%d",
                         hp, len(distinct_attackers), consecutive)
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"EARLY: FLEE HP={hp} attackers={len(distinct_attackers)}"}

        # EG-S3: Self-defense — diserang, HP masih aman, fight 1v1 favorable
        if _combat_history.get("damage_this_tick") and hp >= 35 and enemies_alive \
                and not under_heavy_fire:
            attacker_id = _combat_history.get("last_attacker_id", "")
            attacker = next(
                (a for a in enemies_alive if a.get("id") == attacker_id),
                enemies_alive[0]
            )
            if attacker and _is_in_range(attacker, region_id, w_range, connections):
                my_dmg = calc_damage(atk, get_weapon_bonus(equipped), attacker.get("def", 5), r_weather)
                e_dmg  = calc_damage(attacker.get("atk", 10), _est_enemy_bonus(attacker), defense, r_weather)
                if my_dmg >= e_dmg * 0.55:
                    _combat_history["damage_this_tick"] = False
                    _current_target_id = attacker.get("id", "")
                    return {"action": "attack",
                            "data": {"targetId": attacker["id"], "targetType": "agent"},
                            "reason": f"EARLY SELF-DEFENSE: HP={hp}"}
                else:
                    # Ratio gagal → juga flee, jangan lanjut loot
                    if ep >= move_ep_cost:
                        safe = _find_safe_region(connections, danger_ids, view)
                        if safe:
                            log.info("🏃 EARLY FLEE (outgunned): HP=%d", hp)
                            _prepare_move(region_id)
                            return {"action": "move", "data": {"regionId": safe},
                                    "reason": f"EARLY: FLEE OUTGUNNED HP={hp}"}

        # EG1: Pickup lokal (boosted)
        pickup = _check_pickup(visible_items, inventory, region_id, hp, ep, inv_cap, early_game=True)
        if pickup:
            return pickup

        # EG2: Equip terbaik
        equip = _check_equip(inventory, equipped, view, region_id, connections)
        if equip:
            return equip

        # EG3: Map / Supply Cache / Medical
        util = _use_utility_item(inventory, hp, ep, alive_count)
        if util:
            if util.get("data", {}).get("itemType") == "map":
                _map_used_this_tick = True
            return util
        for fac in interactables:
            if not isinstance(fac, dict) or fac.get("isUsed"):
                continue
            if fac.get("type", "").lower() in ("supply_cache", "medical_facility") and ep >= 2:
                log.info("🎒 EARLY FACILITY: %s", fac.get("type"))
                return {"action": "interact", "data": {"interactableId": fac["id"]},
                        "reason": f"EARLY FACILITY: {fac.get('type')}"}

        # [cooldown gate]
        if not can_act:
            return None

        # EG4: Heal
        if hp < 60:
            heal = _find_healing_item(inventory, critical=(hp < 25))
            if heal:
                return {"action": "use_item", "data": {"itemId": heal["id"]},
                        "reason": f"EARLY HEAL: HP={hp}"}

        # EG5: Move menuju item (EP >= EARLY_GAME_MOVE_EP_MIN)
        if ep >= EARLY_GAME_MOVE_EP_MIN and ep >= move_ep_cost and connections:
            move_target, score = _choose_move_target(
                connections, danger_ids, region, visible_items,
                alive_count, enemies_alive, ep, looting_region_ids, early_game=True
            )
            if move_target and score > 0:
                log.info("🎒 EARLY MOVE → %s (score=%d)", move_target[:8], score)
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move_target},
                        "reason": f"EARLY LOOT MOVE: score={score}"}

        # EG6: Rest
        if not region.get("isDeathZone"):
            if ep < EARLY_GAME_MOVE_EP_MIN:
                return {"action": "rest", "data": {},
                        "reason": f"EARLY REST: EP rendah ({ep}/{max_ep})"}
            if ep < max_ep:
                return {"action": "rest", "data": {},
                        "reason": f"EARLY REST: EP={ep}/{max_ep}"}
        return None

    # ─────────────────────────────────────────────────────────────
    # ════════════════  NORMAL GAME  ══════════════════════════════
    # ─────────────────────────────────────────────────────────────

    # P1: DZ escape
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In DZ! HP={hp}"}

    # P1b: Pre-escape pending DZ
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": safe}, "reason": "PRE-ESCAPE: DZ incoming"}

    # P1c: Desperate flee
    if hp < 20 and not has_heals and (enemies_alive or guardians_here):
        if ep >= move_ep_cost:
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"DESPERATE FLEE: HP={hp}"}
        else:
            all_threats = enemies_alive + guardians_here
            nearest = _select_weakest(all_threats)
            if nearest and _is_in_range(nearest, region_id, w_range, connections):
                return {"action": "attack",
                        "data": {"targetId": nearest["id"], "targetType": "agent"},
                        "reason": "DESPERATE ATTACK"}

    # ── Distinct attacker detection (normal game) ─────────────────
    distinct_attackers_normal = {
        entry.get("attackerId") or entry.get("sourceId", "")
        for entry in recent_logs
        if isinstance(entry, dict)
        and entry.get("type") in ("damage", "attack")
        and entry.get("targetId") == my_id
        and (entry.get("attackerId") or entry.get("sourceId", ""))
    }
    consecutive_normal = _combat_history.get("consecutive_damage_ticks", 0)
    multi_threat = len(distinct_attackers_normal) >= 2 or consecutive_normal >= 3

    # Flee multi-threat bahkan di normal game (jangan nekat 1 vs banyak)
    if multi_threat and hp < 50 and ep >= move_ep_cost and (enemies_alive or guardians_here):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            log.info("🏃 MULTI-THREAT FLEE: HP=%d, attackers=%d", hp, len(distinct_attackers_normal))
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"MULTI-THREAT FLEE: {len(distinct_attackers_normal)} attackers"}

    # P1d: Counter-attack (HP >= 25) — [AGGRO] balas tanpa ratio check
    #       tapi cek dulu tidak sedang di-围 banyak orang
    if _combat_history.get("damage_this_tick") and hp >= 25 and not multi_threat:
        a_id     = _combat_history["last_attacker_id"]
        attacker = next((a for a in visible_agents if a.get("id") == a_id and a.get("isAlive", True)), None)
        if not attacker:
            attacker = enemies_alive[0] if enemies_alive else (guardians_here[0] if guardians_here else None)
        if attacker:
            if _is_in_range(attacker, region_id, w_range, connections):
                _combat_history["damage_this_tick"] = False
                _current_target_id = attacker.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": attacker["id"], "targetType": "agent"},
                        "reason": "COUNTER-ATTACK"}
            elif ep >= move_ep_cost:
                move = _move_toward_target(attacker, connections, danger_ids, view)
                if move:
                    _combat_history["damage_this_tick"] = False
                    _current_target_id = attacker.get("id", "")
                    _prepare_move(region_id)
                    return {"action": "move", "data": {"regionId": move}, "reason": "CHASE attacker"}

    # P2b: Guardian threat evasion
    if guardians_here and ep >= move_ep_cost:
        worst = max(guardians_here, key=lambda g: g.get("atk", 10))
        g_dmg = calc_damage(worst.get("atk", 10), _est_enemy_bonus(worst), defense, r_weather)
        if hp < max(25, int(g_dmg * 1.5)):
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"GUARDIAN FLEE: HP={hp}"}

    # P3: Free actions — pickup, equip, utility
    pickup = _check_pickup(visible_items, inventory, region_id, hp, ep, inv_cap, early_game=False)
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

    # ════ COOLDOWN GATE ════
    if not can_act:
        return None

    # P4: Heal
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

    # P4b: EP management
    ed_count = sum(1 for i in inventory if isinstance(i, dict) and i.get("typeId","").lower() == "energy_drink")
    if ep <= 2 and ep < max_ep:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]},
                    "reason": f"EP RECOVERY: {ep}/{max_ep}"}
    elif ed_count >= 2 and ep <= max_ep - 4:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]},
                    "reason": f"EP RECOVERY (stocked): {ep}/{max_ep}"}

    # ── P4c: LOOTING HUNTER ──────────────────────────────────────
    # Deteksi player yang sedang looting (ada di region berisi item)
    # dan serang/kejar sebelum mereka selesai equipped.
    if looting_players and ep >= 2 and hp >= 25:
        target = _select_best_combat_target(
            looting_players, atk, equipped, defense, r_weather,
            prefer_id=_current_target_id, looting_ids={e.get("id","") for e in looting_players}
        )
        if target:
            if _is_in_range(target, region_id, w_range, connections):
                my_dmg = calc_damage(atk, get_weapon_bonus(equipped), target.get("def", 5), r_weather)
                e_dmg  = calc_damage(target.get("atk", 10), _est_enemy_bonus(target), defense, r_weather)
                hits   = math.ceil(target.get("hp", 100) / max(my_dmg, 1))
                proj   = hits * e_dmg
                # [AGGRO] ratio: 0.8→0.55, lebih berani masuk fight tidak ideal
                if my_dmg >= e_dmg * 0.55 or target.get("hp", 100) <= my_dmg * 2:
                    if hp - proj > 0 or target.get("hp", 100) <= my_dmg:
                        _current_target_id = target.get("id", "")
                        log.info("🎯 LOOTING HUNTER → %s (hp=%d, looting)",
                                 target.get("id","")[:8], target.get("hp", 0))
                        return {"action": "attack",
                                "data": {"targetId": target["id"], "targetType": "agent"},
                                "reason": f"LOOTING HUNTER: target looting HP={target.get('hp')}"}
            elif ep >= move_ep_cost:
                # Kejar player yang sedang looting
                move = _move_toward_target(target, connections, danger_ids, view)
                if move:
                    _current_target_id = target.get("id", "")
                    log.info("🏃 CHASE LOOTER → %s via %s", target.get("id","")[:8], move[:8])
                    _prepare_move(region_id)
                    return {"action": "move", "data": {"regionId": move},
                            "reason": "CHASE LOOTER"}

    # ── P4d: PROACTIVE HUNT ──────────────────────────────────────
    # [NEW] Kejar player visible bahkan tidak sedang looting,
    # jika weapon kita superior dan HP cukup untuk bertahan.
    if enemies_alive and ep >= 2 and hp >= 30:
        tgt = _select_best_combat_target(
            enemies_alive, atk, equipped, defense, r_weather,
            prefer_id=_current_target_id, looting_ids={e.get("id","") for e in looting_players}
        )
        if tgt:
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), tgt.get("def", 5), r_weather)
            e_dmg  = calc_damage(tgt.get("atk", 10), _est_enemy_bonus(tgt), defense, r_weather)
            # Serang kalau kita punya keunggulan senjata jelas (dmg >= 1.3x musuh)
            weapon_superior = my_dmg >= e_dmg * 1.3
            # Atau musuh hampir mati
            almost_dead = tgt.get("hp", 100) <= FINISH_OFF_HP_THRESHOLD
            if weapon_superior or almost_dead:
                if _is_in_range(tgt, region_id, w_range, connections):
                    hits = math.ceil(tgt.get("hp", 100) / max(my_dmg, 1))
                    proj = hits * e_dmg
                    if hp - proj > 10 or almost_dead:
                        _current_target_id = tgt.get("id", "")
                        log.info("🔥 PROACTIVE HUNT → %s (my_dmg=%d, e_dmg=%d)",
                                 tgt.get("id","")[:8], my_dmg, e_dmg)
                        return {"action": "attack",
                                "data": {"targetId": tgt["id"], "targetType": "agent"},
                                "reason": f"PROACTIVE HUNT: dmg={my_dmg} vs {e_dmg}"}
                elif ep >= move_ep_cost:
                    move = _move_toward_target(tgt, connections, danger_ids, view)
                    if move:
                        _current_target_id = tgt.get("id", "")
                        _prepare_move(region_id)
                        return {"action": "move", "data": {"regionId": move},
                                "reason": "PROACTIVE HUNT CHASE"}

    # P5: Guardian farming — [AGGRO] HP min 35→20, ratio >=g_dmg → >=g_dmg*0.7
    guardians_all = [a for a in visible_agents if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians_all and ep >= 2 and hp >= 20:
        tgt = _select_best_combat_target(guardians_all, atk, equipped, defense, r_weather)
        if _is_in_range(tgt, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), tgt.get("def", 5), r_weather)
            g_dmg  = calc_damage(tgt.get("atk", 10), _est_enemy_bonus(tgt), defense, r_weather)
            if my_dmg >= g_dmg * 0.7 or tgt.get("hp", 100) <= my_dmg * 3:
                _current_target_id = tgt.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": tgt["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: dmg={my_dmg}"}
        else:
            g_dmg    = calc_damage(tgt.get("atk", 10), _est_enemy_bonus(tgt), defense, r_weather)
            safe_hp  = max(20, int(g_dmg * 1.2))
            if ep >= move_ep_cost and hp >= safe_hp:
                move = _move_toward_target(tgt, connections, danger_ids, view)
                if move:
                    _prepare_move(region_id)
                    return {"action": "move", "data": {"regionId": move}, "reason": "APPROACH GUARDIAN"}

    # P6: Endgame hunt — [AGGRO] alive<=100 = selalu aktif dari awal game
    if alive_count <= 100 and enemies_alive and ep >= 2 and hp >= 10:
        tgt = _select_best_combat_target(
            enemies_alive, atk, equipped, defense, r_weather,
            prefer_id=_current_target_id, looting_ids={e.get("id","") for e in looting_players}
        )
        if _is_in_range(tgt, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), tgt.get("def", 5), r_weather)
            e_dmg  = calc_damage(tgt.get("atk", 10), _est_enemy_bonus(tgt), defense, r_weather)
            hits   = math.ceil(tgt.get("hp", 100) / max(my_dmg, 1))
            proj   = hits * e_dmg
            if my_dmg >= e_dmg * 0.5 or tgt.get("hp", 100) <= FINISH_OFF_HP_THRESHOLD:
                if hp - proj > 0 or tgt.get("hp", 100) <= my_dmg:
                    _current_target_id = tgt.get("id", "")
                    return {"action": "attack",
                            "data": {"targetId": tgt["id"], "targetType": "agent"},
                            "reason": f"ENDGAME HUNT: alive={alive_count}"}
        else:
            move = _move_toward_target(tgt, connections, danger_ids, view)
            if move and ep >= move_ep_cost:
                _current_target_id = tgt.get("id", "")
                _prepare_move(region_id)
                return {"action": "move", "data": {"regionId": move}, "reason": "ENDGAME CHASE"}

    # P6b: Favorable combat — [AGGRO] ratio >=e_dmg*0.75, margin HP 15→5, ranged 0.75→0.5
    if enemies_alive and ep >= 2 and hp >= 15:
        tgt = _select_best_combat_target(
            enemies_alive, atk, equipped, defense, r_weather,
            prefer_id=_current_target_id, looting_ids={e.get("id","") for e in looting_players}
        )
        if _is_in_range(tgt, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), tgt.get("def", 5), r_weather)
            e_dmg  = calc_damage(tgt.get("atk", 10), _est_enemy_bonus(tgt), defense, r_weather)
            hits   = math.ceil(tgt.get("hp", 100) / max(my_dmg, 1))
            proj   = hits * e_dmg
            if my_dmg >= e_dmg * 0.75 and hp - proj > 5:
                _current_target_id = tgt.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": tgt["id"], "targetType": "agent"},
                        "reason": f"COMBAT: {my_dmg} vs {e_dmg}"}
            elif tgt.get("hp", 100) <= my_dmg * 2 and hp - proj > 0:
                _current_target_id = tgt.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": tgt["id"], "targetType": "agent"},
                        "reason": f"FINISH OFF: HP={tgt.get('hp')}"}
            elif w_range >= 1 and my_dmg >= e_dmg * 0.5:
                _current_target_id = tgt.get("id", "")
                return {"action": "attack",
                        "data": {"targetId": tgt["id"], "targetType": "agent"},
                        "reason": f"RANGED: range={w_range}"}

    # P7: Monster farming
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2 and hp >= 25:
        tgt    = _select_best_combat_target(monsters, atk, equipped, defense, r_weather)
        my_dmg = calc_damage(atk, get_weapon_bonus(equipped), tgt.get("def", 5), r_weather)
        m_dmg  = calc_damage(tgt.get("atk", 10), 0, defense, r_weather)
        if _is_in_range(tgt, region_id, w_range, connections) and \
                (my_dmg >= m_dmg or tgt.get("hp", 100) <= my_dmg * 3):
            return {"action": "attack",
                    "data": {"targetId": tgt["id"], "targetType": "monster"},
                    "reason": "MONSTER FARM"}

    # P7b: Heal when safe
    if hp < 75 and not enemies_alive and not guardians_here:
        heal = _find_healing_item(inventory, critical=(hp < 25))
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"SAFE HEAL: HP={hp}"}

    # P8: Facility
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        fac = _select_facility(interactables, hp, ep, alive_count)
        if fac:
            return {"action": "interact", "data": {"interactableId": fac["id"]},
                    "reason": f"FACILITY: {fac.get('type')}"}

    # P9: Movement — EP guard [FIX]
    if ep >= EP_MOVE_MIN and ep >= move_ep_cost and connections:
        move_target, best_score = _choose_move_target(
            connections, danger_ids, region, visible_items,
            alive_count, enemies_alive, ep, looting_region_ids, early_game=False
        )
        if move_target and best_score > 0:
            log.info("🚶 MOVE → %s (score=%d)", move_target[:8], best_score)
            _prepare_move(region_id)
            return {"action": "move", "data": {"regionId": move_target}, "reason": "EXPLORE"}

    # P10: Rest — sampai EP cukup untuk bergerak [FIX]
    if not region.get("isDeathZone") and region_id not in danger_ids \
            and not enemies_alive and not guardians_here:
        if ep < EP_REST_UNTIL:
            return {"action": "rest", "data": {},
                    "reason": f"REST: EP={ep}/{max_ep} (target {EP_REST_UNTIL})"}

    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Equip (anti-loop fix)
# ─────────────────────────────────────────────────────────────────────

def _check_equip(inventory, equipped, view, region_id, connections=None):
    """
    [FIX v2.0.2] apple-to-apple score: current weapon dapat ranged bonus juga.
    Ini eliminasi loop sniper↔bow.
    """
    nearby = _is_enemy_nearby(view, region_id, connections)
    cur_type  = equipped.get("typeId", "").lower() if equipped else "fist"
    cur_id    = equipped.get("id", "")             if equipped else ""
    cur_bonus = get_weapon_bonus(equipped)
    cur_range = get_weapon_range(equipped)
    cur_score = cur_bonus + (40 if nearby and cur_range >= 1 else 0)

    best = None
    best_score = cur_score

    for item in inventory:
        if not isinstance(item, dict) or item.get("category") != "weapon":
            continue
        if item.get("id") == cur_id or item.get("typeId", "").lower() == cur_type:
            continue
        tid   = item.get("typeId", "").lower()
        bonus = WEAPONS.get(tid, {}).get("bonus", 0)
        rng   = WEAPONS.get(tid, {}).get("range", 0)
        score = bonus + (40 if nearby and rng >= 1 else 0)
        if score > best_score:
            best_score = score
            best = item

    if best:
        log.info("EQUIP: %s (score=%d vs cur=%d, nearby=%s)",
                 best.get("typeId"), best_score, cur_score, nearby)
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"SMART EQUIP: {best.get('typeId', 'weapon')}"}
    return None


def _is_enemy_nearby(view, my_region, connections) -> bool:
    """1-hop only."""
    adj = {conn if isinstance(conn, str) else conn.get("id","") for conn in (connections or [])}
    for a in view.get("visibleAgents", []):
        if not a.get("isGuardian", False) and a.get("isAlive", True):
            if a.get("regionId", "") != my_region and a.get("regionId", "") in adj:
                return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Helper — Pickup
# ─────────────────────────────────────────────────────────────────────

def _check_pickup(items, inventory, region_id, hp=100, ep=10,
                  inv_cap=DEFAULT_INVENTORY_CAP, early_game=False):
    local = [i for i in items if isinstance(i, dict) and i.get("id") and i.get("regionId") == region_id]
    if not local:
        return None

    heal_count = sum(1 for i in inventory if isinstance(i, dict) and i.get("typeId","").lower() in RECOVERY_ITEMS)

    def score_fn(item):
        base = _pickup_score(item, inventory, heal_count, hp, ep)
        if early_game and base > 0:
            cat = item.get("category", "").lower()
            tid = item.get("typeId", "").lower()
            return base + EARLY_GAME_ITEM_BOOST if (cat == "weapon" or tid in RECOVERY_ITEMS) \
                   else base + EARLY_GAME_ITEM_BOOST // 2
        return base

    local.sort(key=score_fn, reverse=True)
    best  = local[0]
    score = score_fn(best)
    if score <= 0:
        return None

    tid    = best.get("typeId", "item")
    prefix = "EARLY " if early_game else ""

    if len(inventory) >= inv_cap:
        drop = _find_droppable(inventory, best)
        if drop:
            return {"action": "drop_item", "data": {"itemId": drop["id"]},
                    "reason": f"{prefix}MAKE ROOM for {tid}"}
        return None

    return {"action": "pickup", "data": {"itemId": best["id"]},
            "reason": f"{prefix}PICKUP: {tid}"}


def _pickup_score(item, inventory, heal_count, hp=100, ep=10):
    tid = item.get("typeId", "").lower()
    cat = item.get("category", "").lower()

    if tid == "rewards" or cat == "currency":
        return 300
    if cat == "weapon":
        bonus = WEAPONS.get(tid, {}).get("bonus", 0)
        best_cur = max((WEAPONS.get(i.get("typeId","").lower(), {}).get("bonus", 0)
                        for i in inventory if isinstance(i, dict) and i.get("category") == "weapon"),
                       default=0)
        return (100 + bonus) if bonus > best_cur else 0
    if tid == "binoculars":
        has = any(isinstance(i, dict) and i.get("typeId","").lower() == "binoculars" for i in inventory)
        return 0 if has else 55
    if tid == "map":
        return 52
    dyn = 100 if ((hp < 50 and tid in RECOVERY_ITEMS) or (ep < 4 and tid == "energy_drink")) else 0
    if tid in RECOVERY_ITEMS:
        if heal_count >= 3 and hp >= 70:
            return 20
        return ITEM_PRIORITY.get(tid, 0) + (10 if heal_count < 4 else 0) + dyn
    return ITEM_PRIORITY.get(tid, 0) + dyn


# ─────────────────────────────────────────────────────────────────────
# Helper — Movement
# ─────────────────────────────────────────────────────────────────────

def _choose_move_target(connections, danger_ids, current_region, visible_items,
                        alive_count, enemies_visible, current_ep,
                        looting_region_ids: set = None, early_game=False):
    """
    [NEW] looting_region_ids: region berisi item.
    Kalau ada enemy DAN item di satu region → bonus besar (looting hunter movement).
    [FIX] _recent_regions penalty kumulatif (bukan hanya last 1).
    """
    looting_region_ids = looting_region_ids or set()
    item_regions = {i.get("regionId","") for i in visible_items if isinstance(i, dict)}

    enemy_region_map: dict[str, int] = {}
    for e in (enemies_visible or []):
        reg = e.get("regionId", "")
        threat = e.get("atk", 10) + e.get("def", 5)
        if reg not in enemy_region_map or threat > enemy_region_map[reg]:
            enemy_region_map[reg] = threat

    candidates = []
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        conn_dict = conn if isinstance(conn, dict) else None
        if not rid:
            continue
        if conn_dict and (conn_dict.get("isDeathZone") or rid in danger_ids):
            continue
        if isinstance(conn, str) and rid in danger_ids:
            continue
        if rid in _map_knowledge.get("death_zones", set()):
            continue

        score = 1

        if conn_dict:
            terrain = conn_dict.get("terrain", "").lower()
            weather = conn_dict.get("weather", "").lower()
            score += {"hills": 4, "plains": 2, "ruins": 3, "forest": 1, "water": -3}.get(terrain, 0)
            score += {"clear": 1, "rain": 0, "fog": -1, "storm": -2}.get(weather, 0)
            if weather == "storm" and current_ep < 4:
                score -= 100
            unused_facs = [f for f in conn_dict.get("interactables", [])
                           if isinstance(f, dict) and not f.get("isUsed")]
            score += len(unused_facs) * 2

        # Item di region ini
        has_items = rid in item_regions
        has_enemy = rid in enemy_region_map

        if has_items:
            score += 5 if not early_game else 5 + EARLY_GAME_MOVE_BOOST

        # [AGGRO] LOOTING HUNTER movement bonus: ada item + ada enemy = +35 (dari +15)
        if has_items and has_enemy and not early_game:
            score += 35   # sangat agresif menuju player yang sedang looting
            log.info("🎯 LOOTING region detected: %s (bonus +35)", rid[:8])

        if not early_game and alive_count > 10:
            threat = enemy_region_map.get(rid, 0)
            if has_items and has_enemy:
                pass  # jangan penalti, kita justru mau ke sana
            elif threat > 25:
                score -= 40
            elif threat > 0:
                score -= threat

        if not early_game and alive_count <= 100 and has_enemy and not has_items:
            score += 8   # [AGGRO] aktif tarik ke enemy region lebih kuat

        if rid in _map_knowledge.get("safe_center", []):
            score += 3

        # [FIX] Penalti kumulatif untuk region yang sering dikunjungi
        visit_count = list(_recent_regions).count(rid)
        score -= visit_count * 8

        if rid == _last_region_id:
            score -= 50

        if rid in _explored_regions:
            score -= 3 if early_game else 5

        candidates.append((rid, score))

    if not candidates:
        return None, -999
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0], candidates[0][1]


# ─────────────────────────────────────────────────────────────────────
# Helper — Combat target selection
# ─────────────────────────────────────────────────────────────────────

def _select_best_combat_target(targets, my_atk, equipped, my_def, weather,
                                prefer_id="", looting_ids: set = None):
    """
    [NEW] looting_ids: player yang sedang looting mendapat LOOTING_HUNTER_BONUS.
    [FIX] prefer_id: persistent target consistency.
    """
    looting_ids = looting_ids or set()
    my_bonus    = get_weapon_bonus(equipped)
    best        = None
    best_score  = -9999

    for t in targets:
        if not isinstance(t, dict):
            continue
        tid    = t.get("id", "")
        t_hp   = max(t.get("hp", 100), 1)
        t_def  = t.get("def", 5)
        t_atk  = t.get("atk", 10)
        t_wbns = _est_enemy_bonus(t)

        my_dmg    = calc_damage(my_atk, my_bonus, t_def, weather)
        their_dmg = calc_damage(t_atk, t_wbns, my_def, weather)
        score     = (my_dmg / t_hp) * 100 - their_dmg * THREAT_WEIGHT

        # Bonus player vs guardian
        if not t.get("isGuardian", False):
            score += PLAYER_BONUS

        # [NEW] Looting hunter bonus
        if tid in looting_ids:
            score += LOOTING_HUNTER_BONUS

        # Vulnerable (habis heal)
        if tid in _vulnerable_agents:
            score += 30

        # Finish off
        if t_hp <= FINISH_OFF_HP_THRESHOLD:
            score += FINISH_OFF_BONUS

        # Persistent target
        if prefer_id and tid == prefer_id:
            score += 15

        if score > best_score:
            best_score = score
            best = t

    return best if best else min(targets, key=lambda t: t.get("hp", 999))


def _select_weakest(targets):
    if not targets:
        return None
    return min(targets, key=lambda t: t.get("hp", 999))


# ─────────────────────────────────────────────────────────────────────
# Helper — Agent tracking
# ─────────────────────────────────────────────────────────────────────

def _track_agents(visible_agents, my_id, my_region):
    global _known_agents
    for a in visible_agents:
        if not isinstance(a, dict):
            continue
        aid = a.get("id", "")
        if not aid or aid == my_id:
            continue
        _known_agents[aid] = {
            "hp": a.get("hp", 100), "atk": a.get("atk", 10), "def": a.get("def", 5),
            "isGuardian": a.get("isGuardian", False),
            "equippedWeapon": a.get("equippedWeapon"),
            "lastSeen": my_region, "isAlive": a.get("isAlive", True),
            "lastSeenTick": _tick_counter,
        }
    stale = _tick_counter - AGENT_STALE_TICKS
    _known_agents = {k: v for k, v in _known_agents.items()
                     if v.get("isAlive", True) and v.get("lastSeenTick", 0) >= stale}


# ─────────────────────────────────────────────────────────────────────
# Helper — Utility
# ─────────────────────────────────────────────────────────────────────

def _use_utility_item(inventory, hp, ep, alive_count):
    global _map_item_used_ids
    for item in inventory:
        if not isinstance(item, dict):
            continue
        tid = item.get("typeId", "").lower()
        if tid == "map":
            iid = item.get("id", "")
            if iid and iid not in _map_item_used_ids:
                _map_item_used_ids.add(iid)
                log.info("🗺️ Using Map")
                return {"action": "use_item", "data": {"itemId": iid, "itemType": "map"},
                        "reason": "UTILITY: Map"}
    if alive_count <= 5 and hp > 50 and ep >= MEGAPHONE_MIN_EP:
        for item in inventory:
            if isinstance(item, dict) and item.get("typeId","").lower() == "megaphone":
                return {"action": "use_item",
                        "data": {"itemId": item["id"], "itemType": "megaphone"},
                        "reason": "UTILITY: Megaphone"}
    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Map learning
# ─────────────────────────────────────────────────────────────────────

def learn_from_map(view):
    global _map_knowledge
    visible_regions = view.get("visibleRegions", [])
    if not visible_regions:
        return
    _map_knowledge["revealed"] = True
    safe = []
    for r in visible_regions:
        if not isinstance(r, dict):
            continue
        rid = r.get("id", "")
        if not rid:
            continue
        if r.get("isDeathZone"):
            _map_knowledge["death_zones"].add(rid)
        else:
            tv = {"hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1}.get(r.get("terrain","").lower(), 0)
            safe.append((rid, len(r.get("connections", [])) + tv))
    safe.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe[:5]]
    log.info("🗺️ MAP: %d DZ, centre=%s", len(_map_knowledge["death_zones"]), _map_knowledge["safe_center"][:3])


# ─────────────────────────────────────────────────────────────────────
# Helper — Combat history & vulnerability
# ─────────────────────────────────────────────────────────────────────

def _update_combat_history(current_hp, recent_logs, my_id):
    global _combat_history
    last = _combat_history.get("last_hp", current_hp)
    if current_hp < last:
        _combat_history["consecutive_damage_ticks"] += 1
        _combat_history["damage_this_tick"] = True
        found = False
        for entry in recent_logs:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") in ("damage", "attack"):
                a_id = entry.get("attackerId") or entry.get("sourceId") or ""
                t_id = entry.get("targetId") or ""
                if t_id == my_id and a_id and a_id != my_id:
                    _combat_history["last_attacker_id"] = a_id
                    found = True
                    break
        if not found:
            _combat_history["last_attacker_id"] = "unknown"
    else:
        _combat_history["consecutive_damage_ticks"] = 0
        _combat_history["damage_this_tick"] = False
    _combat_history["last_hp"] = current_hp


def _detect_vulnerable_agents(recent_logs, my_id):
    global _vulnerable_agents
    _vulnerable_agents = {k: v for k, v in _vulnerable_agents.items()
                          if _tick_counter - v < VULNERABLE_TTL}
    for entry in recent_logs:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "use_item":
            uid  = entry.get("agentId") or entry.get("userId", "")
            name = entry.get("itemName", "").lower()
            if uid and uid != my_id and any(h in name for h in ("medkit","bandage","food","emergency")):
                _vulnerable_agents[uid] = _tick_counter
                log.info("🎯 Vulnerable: %s", uid[:8])


# ─────────────────────────────────────────────────────────────────────
# Helper — Items
# ─────────────────────────────────────────────────────────────────────

def _find_droppable(inventory, target_item):
    target_val = ITEM_DROP_VALUE.get(target_item.get("typeId","").lower(), 1)
    candidates = [
        (i, ITEM_DROP_VALUE.get(i.get("typeId","").lower(), 1))
        for i in inventory
        if isinstance(i, dict)
        and i.get("category","").lower() != "currency"
        and i.get("typeId","").lower() != "rewards"
        and ITEM_DROP_VALUE.get(i.get("typeId","").lower(), 1) < target_val
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[1])[0]


def _find_healing_item(inventory, critical=False):
    heals = [i for i in inventory if isinstance(i, dict) and i.get("typeId","").lower() in RECOVERY_ITEMS]
    if not heals:
        return None
    heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId","").lower(), 0), reverse=critical)
    return heals[0]


def _find_energy_drink(inventory):
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId","").lower() == "energy_drink":
            return i
    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Facility
# ─────────────────────────────────────────────────────────────────────

def _select_facility(interactables, hp, ep, alive_count):
    best = None
    best_p = -1
    for fac in interactables:
        if not isinstance(fac, dict) or fac.get("isUsed"):
            continue
        ft = fac.get("type", "").lower()
        p  = -1
        if ft == "medical_facility" and hp < 80:
            p = 10
        elif ft == "supply_cache":
            p = 8
        elif ft == "watchtower" and alive_count > 15:
            p = 6
        if p > best_p:
            best_p = p
            best   = fac
    return best


# ─────────────────────────────────────────────────────────────────────
# Helper — Range & movement
# ─────────────────────────────────────────────────────────────────────

def _is_in_range(target, my_region, weapon_range, connections=None):
    t_reg = target.get("regionId", "")
    if not t_reg or t_reg == my_region:
        return True
    if weapon_range >= 1 and connections:
        adj = {c if isinstance(c, str) else c.get("id","") for c in connections}
        if t_reg in adj:
            return True
    return False


def _move_toward_target(target, connections, danger_ids, view):
    t_reg = target.get("regionId", "")
    if not t_reg:
        return None
    safe = [conn if isinstance(conn, str) else conn.get("id","")
            for conn in connections
            if (conn if isinstance(conn, str) else conn.get("id","")) not in danger_ids
            and not (isinstance(conn, dict) and conn.get("isDeathZone"))]
    if t_reg in safe:
        return t_reg
    for s1 in safe:
        r1 = _visible_region_cache.get(s1)
        if not r1:
            continue
        for c2 in r1.get("connections", []):
            c2id = c2 if isinstance(c2, str) else c2.get("id","")
            if c2id == t_reg:
                return s1
    return safe[0] if safe else None


def _find_safe_region(connections, danger_ids, view=None):
    opts = []
    for conn in connections:
        rid  = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz and rid not in danger_ids:
            terrain = conn.get("terrain","").lower() if isinstance(conn, dict) else ""
            score   = {"hills": 3, "plains": 2, "ruins": 1, "forest": 0, "water": -2}.get(terrain, 0)
            opts.append((rid, score))
    if opts:
        return max(opts, key=lambda x: x[1])[0]
    for conn in connections:
        rid  = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            return rid
    return None


# ─────────────────────────────────────────────────────────────────────
# Helper — Misc
# ─────────────────────────────────────────────────────────────────────

def _get_move_ep_cost(terrain, weather):
    return 3 if terrain == "water" or weather == "storm" else 2


def _est_enemy_bonus(agent):
    w = agent.get("equippedWeapon")
    if not w:
        return 0
    return WEAPONS.get((w.get("typeId","").lower() if isinstance(w, dict) else ""), {}).get("bonus", 0)
