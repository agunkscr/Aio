"""
Strategy brain v1.7.3 — free action chaining + BotSpeak + loot chase + safer healing + ALLY RECOGNITION.
Bots with the same ALLY_SECRET automatically detect each other and form a secret alliance.
"""

import base64
from bot.utils.logger import get_logger

log = get_logger(__name__)

# ── ALLY SYSTEM ───────────────────────────────────────────────────────
# Ganti dengan string rahasia yang SAMA untuk semua bot Anda!
ALLY_SECRET = "K4tanaS3cret2024"
_ally_ids: set = set()                # ID agent yang sudah dikenali sebagai ally
_ally_broadcast_tick: int = 0         # Counter agar tidak spam berlebihan
_ally_announced_this_game: bool = False  # Cukup umumkan identitas satu kali per game

def _set_ally(agent_id: str):
    """Tandai agent_id sebagai ally."""
    if agent_id not in _ally_ids:
        _ally_ids.add(agent_id)
        log.info("🤝 ALLY REGISTERED: %s", agent_id[:8])

def _is_ally(agent_id: str) -> bool:
    return agent_id in _ally_ids

def _clear_allies():
    """Reset ally list saat game baru."""
    global _ally_ids, _ally_broadcast_tick, _ally_announced_this_game
    _ally_ids.clear()
    _ally_broadcast_tick = 0
    _ally_announced_this_game = False

# ── Weapon stats ─────────────────────────────────────────────────────
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

# ── Item drop values ──────────────────────────────────────────────────
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

# ── BotSpeak encryption constants ──────────────────────────────────────
BOTSPEAK_ROT = 13
BOTSPEAK_POST_ROT = 1

# ── Global state ──────────────────────────────────────────────────────
_game_id: str = None
_known_agents: dict = {}
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": []}
_combat_history: dict = {"last_hp": 100, "consecutive_damage_ticks": 0,
                           "last_attacker_id": "", "damage_this_tick": False}
_explored_regions: set = set()
_map_used_this_tick: bool = False

# ── Damage calculation ────────────────────────────────────────────────
def calc_damage(atk: int, weapon_bonus: int, target_def: int, weather: str = "clear") -> int:
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

# ── BotSpeak cryptography ──────────────────────────────────────────────
def _rot_char(c: str, n: int) -> str:
    """Rotate ASCII printable character (32-126)."""
    if 32 <= ord(c) <= 126:
        base = 32
        return chr((ord(c) - base + n) % 95 + base)
    return c

def _rot_str(s: str, n: int) -> str:
    return ''.join(_rot_char(c, n) for c in s)

def encode_botspeak(plaintext: str) -> str:
    """Encrypt to BotSpeak (unreadable by humans)."""
    step1 = _rot_str(plaintext, BOTSPEAK_ROT)
    step2 = base64.b64encode(step1.encode()).decode()
    step3 = _rot_str(step2, BOTSPEAK_POST_ROT)
    return step3

def decode_botspeak(cipher: str) -> str:
    """Decrypt BotSpeak (if needed)."""
    try:
        step1 = _rot_str(cipher, -BOTSPEAK_POST_ROT)
        step2 = base64.b64decode(step1.encode()).decode()
        step3 = _rot_str(step2, -BOTSPEAK_ROT)
        return step3
    except:
        return None

# ── Communication decision (v1.7.3: ally announcement + lure) ──────────
def _should_talk(view: dict) -> str | None:
    """
    Return plaintext message to send (will be encrypted), or None.
    Priority: ally announcement (first few ticks) > lure / gertakan.
    """
    global _ally_announced_this_game, _ally_broadcast_tick
    self_data = view.get("self", {})
    my_id = self_data.get("id", "")
    hp = self_data.get("hp", 100)
    alive_count = view.get("aliveCount", 100)
    enemies = [a for a in view.get("visibleAgents", [])
               if not a.get("isGuardian") and a.get("isAlive")
               and a.get("id") != my_id and not _is_ally(a.get("id", ""))]

    # 1. Jika belum mengumumkan identitas dan game masih awal, kirim tanda pengenal
    if not _ally_announced_this_game:
        _ally_broadcast_tick += 1
        # Kirim pada tick ke-1, ke-3, dan ke-5 (jika belum ada ally terdeteksi)
        if _ally_broadcast_tick in (1, 2, 3) and not _ally_ids:
            return f"ALLY:{ALLY_SECRET}"
        elif _ally_broadcast_tick >= 5:
            _ally_announced_this_game = True   # berhenti mengumumkan setelah beberapa tick

    # 2. Lure / gertakan seperti biasa
    if alive_count <= 5 and hp > 60:
        return "Free katana here! Come quick."
    if enemies:
        weak = [e for e in enemies if e.get("hp", 100) < 40]
        if weak and hp > 50:
            return "You are so weak, run!"
    if len(enemies) == 1 and hp > 70:
        return "Truce? Let's team up."
    return None

# ── Free action chaining ───────────────────────────────────────────────
def get_free_actions(view: dict) -> list[dict]:
    """
    Return list of free actions to perform before main action.
    Sequence: talk (if conditions met) → drop (if inv full) → pickup → equip.
    """
    actions = []
    self_data = view.get("self", {})
    inventory = self_data.get("inventory", [])
    region = view.get("currentRegion", {})
    region_id = region.get("id", "")
    visible_items_raw = view.get("visibleItems", [])

    # 1. Talk (BotSpeak)
    message_plain = _should_talk(view)
    if message_plain:
        cipher = encode_botspeak(message_plain)
        actions.append({"action": "talk", "data": {"message": cipher}, "reason": "FREE TALK: BotSpeak"})

    # 2. Pickup logic (will include drop if inventory full)
    visible_items = []
    for entry in visible_items_raw:
        if isinstance(entry, dict):
            inner = entry.get("item") or entry
            if isinstance(inner, dict):
                inner["regionId"] = entry.get("regionId", "")
                visible_items.append(inner)
    local_items = [i for i in visible_items if isinstance(i, dict) and i.get("id")
                   and i.get("regionId") == region_id]
    if local_items:
        heal_count = sum(1 for i in inventory if isinstance(i, dict)
                         and i.get("typeId","").lower() in RECOVERY_ITEMS
                         and RECOVERY_ITEMS.get(i.get("typeId","").lower(),0) > 0)
        local_items.sort(key=lambda i: _pickup_score(i, inventory, heal_count), reverse=True)
        best = local_items[0]
        if _pickup_score(best, inventory, heal_count) > 0:
            if len(inventory) >= 10:
                drop = _find_droppable_item(inventory, best)
                if drop:
                    actions.append({"action": "drop_item", "data": {"itemId": drop["id"]},
                                    "reason": "FREE DROP: make room"})
            actions.append({"action": "pickup", "data": {"itemId": best["id"]},
                            "reason": "FREE PICKUP"})

    # 3. Equip best weapon (after potential pickup)
    equipped = self_data.get("equippedWeapon")
    current_bonus = get_weapon_bonus(equipped) if equipped else 0
    best_weapon = None
    best_bonus = current_bonus
    for item in inventory:
        if isinstance(item, dict) and item.get("category") == "weapon":
            type_id = item.get("typeId", "").lower()
            bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
            if bonus > best_bonus:
                best_weapon = item
                best_bonus = bonus
    if best_weapon:
        actions.append({"action": "equip", "data": {"itemId": best_weapon["id"]},
                        "reason": "FREE EQUIP"})
    return actions

# ── Main decision engine (v1.7.3) ─────────────────────────────────────
def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Main decision for EP-cost actions.
    Ally-aware: will never attack an ally, and avoids targeting allies.

    Priority chain:
    1. Death zone escape / pre-escape
    1c. Desperate flee (HP < 20, no healing, enemies)
    1d. Counter-attack (only non-ally attackers)
    2b. Guardian threat evasion
    3. Use utility items (Map, Megaphone)
    4. Critical healing (HP < 45)
    4b. Normal healing (HP < 80)
    4c. Energy drink (EP <= 2, not full)
    5. Guardian farming (guardians only, never ally)
    6. Endgame hunt (non-ally enemies only)
    6b. Favorable agent combat (non-ally)
    7. Monster farming
    7b. Opportunistic heal / Camping heal
    8. Facility interaction
    8b. Loot chase
    9. Strategic movement
    10. Rest
    """
    global _game_id, _map_used_this_tick, _explored_regions

    # Auto-reset on game change
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

    # Unwrap visibleItems (needed for map/megaphone detection)
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

    # Mark explored region
    if region_id:
        _explored_regions.add(region_id)

    # Trigger learn_from_map if Map was used
    if _map_used_this_tick:
        _map_used_this_tick = False
        learn_from_map(view)

    # Danger map
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

    _track_agents(visible_agents, my_id, region_id)
    _update_combat_history(hp, recent_logs, my_id)

    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # Priority 1: Death zone escape
    if region.get("isDeathZone"):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 DZ escape to %s", safe)
            return {"action": "move", "data": {"regionId": safe}, "reason": "ESCAPE: DZ"}
    if region_id in danger_ids:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("⚠️ Pre-escape to %s", safe)
            return {"action": "move", "data": {"regionId": safe}, "reason": "PRE-ESCAPE"}

    # Filter out allies from enemy lists
    enemies_alive = [a for a in visible_agents
                     if not a.get("isGuardian") and a.get("isAlive")
                     and a.get("id") != my_id
                     and not _is_ally(a.get("id", ""))]
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian") and a.get("isAlive")
                      and a.get("regionId") == region_id]

    # 1c. Desperate flee
    has_healing = any(isinstance(i, dict) and i.get("typeId","").lower() in RECOVERY_ITEMS and RECOVERY_ITEMS[i.get("typeId","").lower()] > 0 for i in inventory)
    if hp < 20 and not has_healing and (enemies_alive or guardians_here):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            return {"action": "move", "data": {"regionId": safe}, "reason": "DESPERATE FLEE"}

    # 1d. Counter-attack (only if attacker is not ally)
    if _combat_history.get("damage_this_tick"):
        attacker_id = _combat_history["last_attacker_id"]
        if attacker_id and not _is_ally(attacker_id):
            attacker = None
            if attacker_id == "unknown":
                if enemies_alive:
                    attacker = enemies_alive[0]
                elif guardians_here:
                    attacker = guardians_here[0]
            else:
                attacker = next((a for a in visible_agents if a.get("id") == attacker_id and a.get("isAlive")), None)
            if attacker:
                w_range = get_weapon_range(equipped)
                if _is_in_range(attacker, region_id, w_range, connections):
                    my_dmg = calc_damage(atk, get_weapon_bonus(equipped), attacker.get("def", 5), region_weather)
                    log.warning("⚔️ Counter-attack! (non-ally)")
                    _combat_history["damage_this_tick"] = False
                    return {"action": "attack", "data": {"targetId": attacker["id"], "targetType": "agent"}, "reason": "COUNTER-ATTACK"}
                else:
                    move = _move_toward_target(attacker, connections, danger_ids, view)
                    if move:
                        _combat_history["damage_this_tick"] = False
                        return {"action": "move", "data": {"regionId": move}, "reason": "CHASE attacker"}
            # If attacker is unknown and no visible enemy, just clear the flag
            _combat_history["damage_this_tick"] = False

    # 2b. Guardian evasion
    if guardians_here and ep >= move_ep_cost:
        threat = max(guardians_here, key=lambda g: g.get("atk", 10))
        g_dmg = calc_damage(threat.get("atk", 10), _estimate_enemy_weapon_bonus(threat), defense, region_weather)
        if hp < max(45, int(g_dmg * 1.5)):
            safe = _find_safe_region(connections, danger_ids, view)
            if safe:
                return {"action": "move", "data": {"regionId": safe}, "reason": "GUARDIAN FLEE"}

    # 3. Use utility items (Map, Megaphone)
    util = _use_utility_item(inventory, hp, ep, alive_count)
    if util:
        if util.get("data", {}).get("itemType") == "map":
            _map_used_this_tick = True
        return util

    if not can_act:
        return None

    # ── Healing (v1.7.2 thresholds) ─────────────────────────────────
    if hp < 45:
        heal = _find_healing_item(inventory, critical=True)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "CRITICAL HEAL"}
    elif hp < 80:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "HEAL"}

    # 4b. Energy drink
    if ep <= 2 and ep < max_ep:
        drink = _find_energy_drink(inventory)
        if drink:
            return {"action": "use_item", "data": {"itemId": drink["id"]}, "reason": "EP RECOVERY"}

    # 5. Guardian farming (guardians only, never allies)
    guardians = [a for a in visible_agents if a.get("isGuardian") and a.get("isAlive")]
    if guardians and ep >= 2 and hp >= 45:
        target = _select_best_combat_target(guardians, atk, equipped, defense, region_weather)
        if _is_in_range(target, region_id, get_weapon_range(equipped), connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), target.get("def", 5), region_weather)
            g_dmg = calc_damage(target.get("atk", 10), _estimate_enemy_weapon_bonus(target), defense, region_weather)
            if my_dmg >= g_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"}, "reason": "GUARDIAN FARM"}
        else:
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost and hp >= 50:
                return {"action": "move", "data": {"regionId": move}, "reason": "APPROACH GUARDIAN"}

    # 6. Endgame hunt (non-ally enemies only)
    if alive_count <= 10 and enemies_alive and ep >= 2 and hp >= 45:
        target = _select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather)
        if _is_in_range(target, region_id, get_weapon_range(equipped), connections):
            return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"}, "reason": "ENDGAME HUNT"}
        else:
            move = _move_toward_target(target, connections, danger_ids, view)
            if move and ep >= move_ep_cost:
                return {"action": "move", "data": {"regionId": move}, "reason": "ENDGAME CHASE"}

    # 6b. Favorable combat (non-ally)
    hp_threshold = 45 if alive_count > 20 else 35
    if enemies_alive and ep >= 2 and hp >= hp_threshold:
        target = _select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather)
        if _is_in_range(target, region_id, get_weapon_range(equipped), connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), target.get("def", 5), region_weather)
            e_dmg = calc_damage(target.get("atk", 10), _estimate_enemy_weapon_bonus(target), defense, region_weather)
            if my_dmg > e_dmg or target.get("hp", 100) <= my_dmg * 2:
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"}, "reason": "COMBAT"}
            elif get_weapon_range(equipped) >= 1 and my_dmg >= e_dmg * 0.7:
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"}, "reason": "RANGED ATTACK"}

    # 7. Monster farming
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2 and hp >= 35:
        target = _select_best_combat_target(monsters, atk, equipped, defense, region_weather)
        if _is_in_range(target, region_id, get_weapon_range(equipped), connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), target.get("def", 5), region_weather)
            m_dmg = calc_damage(target.get("atk", 10), 0, defense, region_weather)
            if my_dmg >= m_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "monster"}, "reason": "MONSTER FARM"}

    # 7b. Opportunistic heal / Camping heal
    if hp < 95 and not enemies_alive and not guardians_here:
        heal = _find_healing_item(inventory, critical=(hp < 45))
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "HEAL (safe)"}
    elif hp < 100 and not enemies_alive and not guardians_here and alive_count <= 10:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "CAMPING HEAL"}

    # 8. Facility interaction
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep, alive_count)
        if facility:
            return {"action": "interact", "data": {"interactableId": facility["id"]}, "reason": f"FACILITY: {facility.get('type')}"}

    # 8b. Loot chase
    if ep >= move_ep_cost and connections and not enemies_alive and not guardians_here:
        loot_region, loot_name = _find_valuable_item_region(
            connections, danger_ids, visible_items_raw, inventory, view
        )
        if loot_region:
            return {"action": "move", "data": {"regionId": loot_region}, "reason": f"LOOT CHASE: {loot_name}"}

    # 9. Strategic movement
    if ep >= move_ep_cost and connections:
        move_target = _choose_move_target(connections, danger_ids, region, visible_items, alive_count, enemies_alive, ep)
        if move_target:
            return {"action": "move", "data": {"regionId": move_target}, "reason": "EXPLORE"}

    # 10. Rest
    if ep < 3 and not enemies_alive and not guardians_here and region_id not in danger_ids and not region.get("isDeathZone"):
        return {"action": "rest", "data": {}, "reason": f"REST: EP={ep}"}

    return None

# ── Combined decision for engine ───────────────────────────────────────
def decide_actions(view: dict, can_act: bool) -> list[dict]:
    """
    Return all actions for this tick, free actions first then main action.
    Engine should send them sequentially via WebSocket.
    """
    free = get_free_actions(view)
    main = decide_action(view, can_act)
    if main:
        free.append(main)
    return free

# ── Helper functions (unchanged unless noted) ──────────────────────────
def reset_game_state():
    global _known_agents, _map_knowledge, _combat_history, _explored_regions, _map_used_this_tick
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _combat_history = {"last_hp": 100, "consecutive_damage_ticks": 0, "last_attacker_id": "", "damage_this_tick": False}
    _explored_regions = set()
    _map_used_this_tick = False
    _clear_allies()   # Reset ally list saat game baru
    log.info("Strategy brain reset (v1.7.3)")

def _update_combat_history(current_hp: int, recent_logs: list, my_id: str):
    global _combat_history
    last = _combat_history.get("last_hp", current_hp)
    if current_hp < last:
        _combat_history["consecutive_damage_ticks"] += 1
        _combat_history["damage_this_tick"] = True
        for log_entry in recent_logs:
            if isinstance(log_entry, dict) and "damage" in str(log_entry.get("message", "")).lower():
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

def _get_move_ep_cost(terrain: str, weather: str) -> int:
    if terrain == "water": return 3
    if weather == "storm": return 3
    return 2

def _estimate_enemy_weapon_bonus(agent: dict) -> int:
    weapon = agent.get("equippedWeapon")
    if not weapon: return 0
    type_id = weapon.get("typeId", "").lower() if isinstance(weapon, dict) else ""
    return WEAPONS.get(type_id, {}).get("bonus", 0)

def _select_best_combat_target(targets: list, my_atk: int, equipped, my_def: int, weather: str) -> dict:
    best, best_score = None, -9999
    my_bonus = get_weapon_bonus(equipped)
    for t in targets:
        if not isinstance(t, dict): continue
        t_hp = max(t.get("hp", 100), 1)
        my_dmg = calc_damage(my_atk, my_bonus, t.get("def", 5), weather)
        their_dmg = calc_damage(t.get("atk", 10), _estimate_enemy_weapon_bonus(t), my_def, weather)
        score = (my_dmg / t_hp) * 100 - their_dmg * 0.5
        if score > best_score:
            best_score, best = score, t
    return best or min(targets, key=lambda t: t.get("hp", 999))

def _track_agents(visible_agents: list, my_id: str, my_region: str):
    global _known_agents
    for agent in visible_agents:
        if not isinstance(agent, dict): continue
        aid = agent.get("id", "")
        if not aid or aid == my_id: continue
        _known_agents[aid] = {
            "hp": agent.get("hp", 100), "atk": agent.get("atk", 10), "def": agent.get("def", 5),
            "isGuardian": agent.get("isGuardian", False), "equippedWeapon": agent.get("equippedWeapon"),
            "lastSeen": my_region, "isAlive": agent.get("isAlive", True),
        }

def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    for item in inventory:
        if isinstance(item, dict) and item.get("typeId", "").lower() == "map":
            return {"action": "use_item", "data": {"itemId": item["id"], "itemType": "map"}, "reason": "UTILITY: Map"}
    if alive_count <= 5 and hp > 50:
        for item in inventory:
            if isinstance(item, dict) and item.get("typeId", "").lower() == "megaphone":
                return {"action": "use_item", "data": {"itemId": item["id"], "itemType": "megaphone"}, "reason": "UTILITY: Megaphone"}
    return None

def learn_from_map(view: dict):
    global _map_knowledge
    regions = view.get("visibleRegions", [])
    if not regions: return
    _map_knowledge["revealed"] = True
    safe = []
    for r in regions:
        if not isinstance(r, dict): continue
        rid = r.get("id", "")
        if not rid: continue
        if r.get("isDeathZone"):
            _map_knowledge["death_zones"].add(rid)
        else:
            conns = r.get("connections", [])
            terrain = r.get("terrain", "").lower()
            score = len(conns) + {"hills":3,"plains":2,"ruins":2,"forest":1,"water":-1}.get(terrain,0)
            safe.append((rid, score))
    safe.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe[:5]]

def _pickup_score(item: dict, inventory: list, heal_count: int) -> int:
    type_id = item.get("typeId", "").lower()
    if type_id == "rewards" or item.get("category","").lower() == "currency":
        return 300
    if item.get("category") == "weapon":
        bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
        current_best = max((WEAPONS.get(i.get("typeId","").lower(),{}).get("bonus",0) for i in inventory if isinstance(i,dict) and i.get("category")=="weapon"), default=0)
        return 100 + bonus if bonus > current_best else 0
    if type_id == "binoculars":
        return 55 if not any(isinstance(i,dict) and i.get("typeId","").lower()=="binoculars" for i in inventory) else 0
    if type_id == "map": return 52
    if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS[type_id] > 0:
        return ITEM_PRIORITY.get(type_id, 0) + (10 if heal_count < 4 else 0)
    if type_id == "energy_drink": return 58
    return ITEM_PRIORITY.get(type_id, 0)

def _find_droppable_item(inventory: list, target_item: dict) -> dict | None:
    target_score = ITEM_DROP_VALUE.get(target_item.get("typeId","").lower(), 1)
    candidates = [(i, ITEM_DROP_VALUE.get(i.get("typeId","").lower(), 1)) for i in inventory if isinstance(i,dict) and i.get("category","").lower() != "currency"]
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0] if candidates else None

def _find_healing_item(inventory: list, critical: bool = False) -> dict | None:
    heals = [i for i in inventory if isinstance(i, dict) and i.get("typeId","").lower() in RECOVERY_ITEMS and RECOVERY_ITEMS[i["typeId"].lower()] > 0]
    if not heals: return None
    heals.sort(key=lambda i: RECOVERY_ITEMS[i["typeId"].lower()], reverse=critical)
    return heals[0]

def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId","").lower() == "energy_drink": return i
    return None

def _select_facility(interactables: list, hp, ep, alive_count):
    best, best_prio = None, -1
    for fac in interactables:
        if not isinstance(fac, dict) or fac.get("isUsed"): continue
        ftype = fac.get("type","").lower()
        prio = -1
        if ftype == "medical_facility" and hp < 80: prio = 10
        elif ftype == "watchtower" and alive_count > 15: prio = 8
        elif ftype == "supply_cache": prio = 7
        elif ftype == "broadcast_station" and alive_count <= 5: prio = 5
        if prio > best_prio: best, best_prio = fac, prio
    return best

def _is_in_range(target, my_region, weapon_range, connections=None):
    tr = target.get("regionId","")
    if not tr or tr == my_region: return True
    if weapon_range >= 1 and connections:
        adj = {c if isinstance(c,str) else c.get("id","") for c in connections}
        return tr in adj
    return False

def _move_toward_target(target, connections, danger_ids, view):
    tr = target.get("regionId","")
    if not tr: return None
    for conn in connections:
        rid = conn if isinstance(conn,str) else conn.get("id","")
        if rid == tr and rid not in danger_ids and not (isinstance(conn,dict) and conn.get("isDeathZone")):
            return rid
    for conn in connections:
        rid = conn if isinstance(conn,str) else conn.get("id","")
        if rid and rid not in danger_ids and not (isinstance(conn,dict) and conn.get("isDeathZone")):
            return rid
    return None

def _find_valuable_item_region(connections, danger_ids, visible_items, inventory, view) -> tuple[str, str] | tuple[None, None]:
    """Scan adjacent regions for high-value items worth moving toward."""
    if not visible_items:
        return None, None

    current_best_bonus = 0
    for i in inventory:
        if isinstance(i, dict) and i.get("category") == "weapon":
            type_id = i.get("typeId", "").lower()
            current_best_bonus = max(current_best_bonus, WEAPONS.get(type_id, {}).get("bonus", 0))

    safe_adj = {}
    for conn in connections:
        if isinstance(conn, str):
            if conn not in danger_ids:
                safe_adj[conn] = conn
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if rid and rid not in danger_ids and not conn.get("isDeathZone"):
                safe_adj[rid] = rid

    best_score = 0
    best_region = None
    best_name = None

    for entry in visible_items:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("item") or entry
        if not isinstance(inner, dict):
            continue
        region_id = entry.get("regionId", "") or inner.get("regionId", "")
        if region_id not in safe_adj:
            continue

        type_id = inner.get("typeId", "").lower()
        category = inner.get("category", "").lower()

        score = 0
        if type_id == "rewards" or category == "currency":
            score = 300
        elif category == "weapon":
            bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
            if bonus > current_best_bonus:
                score = 100 + bonus
        elif type_id == "medkit":
            score = 70
        elif type_id == "bandage":
            score = 50

        if score > best_score:
            best_score = score
            best_region = region_id
            best_name = type_id

    MIN_SCORE = 70
    if best_score >= MIN_SCORE:
        return best_region, best_name
    return None, None

def _choose_move_target(connections, danger_ids, current_region, visible_items, alive_count, enemies_visible, current_ep):
    global _explored_regions
    candidates = []
    item_regions = {i.get("regionId","") for i in visible_items if isinstance(i, dict)}
    enemy_regions = {e.get("regionId","") for e in (enemies_visible or []) if isinstance(e, dict)}
    for conn in connections:
        rid, conn_dict = None, None
        if isinstance(conn, str):
            if conn in danger_ids: continue
            rid, conn_dict = conn, None
        elif isinstance(conn, dict):
            rid = conn.get("id","")
            if not rid or conn.get("isDeathZone") or rid in danger_ids: continue
            conn_dict = conn
        if rid in _map_knowledge.get("death_zones", set()): continue
        score = 1
        if conn_dict:
            terrain = conn_dict.get("terrain","").lower()
            weather = conn_dict.get("weather","").lower()
            score += {"hills":4, "plains":2, "ruins":2, "forest":1, "water":-3}.get(terrain,0)
            score += {"clear":1, "rain":0, "fog":-1, "storm":-2}.get(weather,0)
            if weather == "storm" and current_ep < 4: score -= 100
        if rid in item_regions: score += 5
        if rid in enemy_regions and alive_count <= 10: score += 4
        if conn_dict:
            facs = conn_dict.get("interactables", [])
            score += len([f for f in facs if isinstance(f, dict) and not f.get("isUsed")]) * 2
        if alive_count < 30: score += 3
        if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []): score += 5
        if rid in _explored_regions: score -= 5
        candidates.append((rid, score))
    if not candidates: return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]

def _resolve_region(entry, view: dict):
    if isinstance(entry, dict): return entry
    if isinstance(entry, str):
        for r in view.get("visibleRegions", []):
            if isinstance(r, dict) and r.get("id") == entry: return r
    return None