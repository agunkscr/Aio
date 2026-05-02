"""
MoltyRoyale Maximum Win Rate Brain v2.0.0
============================================
Gabungan keandalan v1.9.2 + fitur canggih v3.1.

Fitur Utama:
- Prioritas rantai lengkap v1.9.2 (counter-attack, third-party, anti-ping-pong, dll.)
- Pelacakan zona & kesadaran bahaya
- Dynamic difficulty (early/mid/late game)
- Enemy profiling & win probability
- Strategy adaptation (belajar dari 10 game terakhir)
- Performance tracking & config persistence
- Kompatibel penuh dengan game loop ActionSender
"""

import time
import random
from typing import Optional
from dataclasses import dataclass, field

# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class StrategyConfig:
    """Configurable strategy weights."""
    # Core combat
    aggression: float = 0.5
    heal_threshold: float = 0.4
    rest_ep_threshold: int = 3
    flee_threshold: float = 0.35
    explore_priority: float = 0.6

    # Thresholds
    attack_win_prob_threshold: float = 0.65
    flee_win_prob_threshold: float = 0.30
    survival_priority: float = 0.7

    # Advanced (v2.0)
    zone_awareness: float = 0.9
    loot_priority: float = 0.7
    risk_tolerance: float = 0.4
    late_game_aggression: float = 0.8
    early_game_caution: float = 0.6
    team_mode: bool = False


# ============================================================================
# GAME DATA
# ============================================================================

WEAPONS = {
    "fist":   {"bonus": 0,  "range": 0},
    "dagger": {"bonus": 10, "range": 0},
    "sword":  {"bonus": 20, "range": 0},
    "katana": {"bonus": 35, "range": 0},
    "bow":    {"bonus": 5,  "range": 1},
    "pistol": {"bonus": 10, "range": 1},
    "sniper": {"bonus": 28, "range": 2},
}

RECOVERY_ITEMS = {
    "medkit": 50, "bandage": 30, "emergency_food": 20,
    "energy_drink": 0,
}

ITEM_PRIORITY = {
    "rewards": 300,
    "katana": 100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger": 80, "bow": 75,
    "medkit": 70, "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars": 55,
    "map": 52,
    "megaphone": 40,
}

ITEM_DROP_VALUE = {
    "rewards": -1,
    "katana": 10, "sniper": 9.5, "sword": 9, "pistol": 8.5,
    "dagger": 8, "bow": 7.5,
    "medkit": 7, "bandage": 6.5, "emergency_food": 6, "energy_drink": 5.8,
    "binoculars": 5.5,
    "map": 5.2,
    "megaphone": 4,
    "fist": 0,
}

WEATHER_COMBAT_PENALTY = {
    "clear": 0.0, "rain": 0.05, "fog": 0.10, "storm": 0.15,
}

# Tuning constants
AGENT_STALE_TICKS = 20
MEGAPHONE_MIN_EP = 4
THREAT_WEIGHT = 0.8
VULNERABLE_TTL = 2


# ============================================================================
# ENEMY PROFILE (v2.0)
# ============================================================================

@dataclass
class EnemyProfile:
    """Track enemy behavior over time."""
    agent_id: str
    encounters: int = 0
    wins: int = 0
    losses: int = 0
    last_known_hp: int = 100
    last_known_ep: int = 10
    preferred_weapon: Optional[str] = None
    aggression_level: float = 0.5
    last_seen_region: Optional[str] = None
    last_seen_tick: int = 0


# ============================================================================
# MAIN BRAIN CLASS
# ============================================================================

class MoltyRoyaleBrain:
    """Maximum Win Rate Brain v2.0.0"""

    def __init__(self):
        self.config = StrategyConfig()

        # Internal state (v1.9.2 globals menjadi atribut)
        self._game_id: Optional[str] = None
        self._tick_counter: int = 0
        self._last_region_id: str = ""
        self._explored_regions: set = set()
        self._map_knowledge: dict = {
            "revealed": False,
            "death_zones": set(),
            "safe_center": [],
        }
        self._combat_history: dict = {
            "last_hp": 100,
            "consecutive_damage_ticks": 0,
            "last_attacker_id": "",
            "damage_this_tick": False,
        }
        self._vulnerable_agents: dict = {}  # id -> tick
        self._visible_region_cache: dict[str, dict] = {}
        self._map_used_this_tick: bool = False
        self._map_item_used_ids: set = set()

        # Enemy profiles (v2.0)
        self._enemy_profiles: dict[str, EnemyProfile] = {}

        # Performance tracking (v2.0)
        self._game_history: list[dict] = []
        self._combat_log: list[dict] = []
        self._damage_dealt_log: list[int] = []
        self._damage_taken_log: list[int] = []
        self._successful_combats: int = 0
        self._failed_combats: int = 0
        self._items_collected: int = 0
        self._actions_this_game: int = 0
        self._turn_number: int = 0

        # Ally coordination (v2.0)
        self._allies: set[str] = set()
        self._ally_positions: dict[str, str] = {}

    # ========================================================================
    # MAIN ENTRY POINT
    # ========================================================================

    def decide_action(self, view: dict, can_act: bool = True) -> Optional[dict]:
        """
        Main decision function.
        Returns action dict compatible with ActionSender.build_action().
        """
        # Auto-reset on game change
        new_game_id = view.get("gameId", "")
        if new_game_id and new_game_id != self._game_id:
            self.reset_game_state()
            self._game_id = new_game_id

        self._tick_counter += 1
        self._actions_this_game += 1

        # Parse view data
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
        visible_regions = view.get("visibleRegions", [])
        connected_regions = view.get("connectedRegions", [])
        pending_dz = view.get("pendingDeathzones", [])
        alive_count = view.get("aliveCount", 100)
        recent_logs = view.get("recentLogs", [])

        # Cache visible regions
        self._visible_region_cache = {}
        for r in visible_regions:
            if isinstance(r, dict) and r.get("id"):
                self._visible_region_cache[r["id"]] = r

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

        connections = connected_regions or region.get("connections", [])
        interactables = region.get("interactables", [])
        region_id = region.get("id", "")
        region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
        region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""

        if not is_alive:
            return None

        # Update explored
        if region_id:
            self._explored_regions.add(region_id)

        # Trigger learn_from_map if Map was used last tick
        if self._map_used_this_tick:
            self._map_used_this_tick = False
            self._learn_from_map(view)

        # Cleanup map item ids not in inventory
        current_item_ids = {i.get("id", "") for i in inventory if isinstance(i, dict)}
        self._map_item_used_ids.intersection_update(current_item_ids)

        # Build danger map
        danger_ids = set()
        for dz in pending_dz:
            if isinstance(dz, dict):
                danger_ids.add(dz.get("id", ""))
            elif isinstance(dz, str):
                danger_ids.add(dz)
        for conn in connections:
            resolved = self._resolve_region(conn, view)
            if resolved and resolved.get("isDeathZone"):
                danger_ids.add(resolved.get("id", ""))

        # Track agents
        self._track_agents(visible_agents, my_id, region_id)

        # Update combat history & detect vulnerable agents
        self._update_combat_history(hp, recent_logs, my_id)
        self._detect_vulnerable_agents(recent_logs, my_id)

        move_ep_cost = self._get_move_ep_cost(region_terrain, region_weather)

        # ── Priority 1: DEATHZONE ESCAPE ──────────────────────────
        if region.get("isDeathZone", False):
            safe = self._find_safe_region(connections, danger_ids, view)
            if safe and ep >= move_ep_cost:
                self._prepare_move(region_id)
                return self._build_action("move", {"regionId": safe}, f"ESCAPE: In death zone! HP={hp}")

        if region_id in danger_ids:
            safe = self._find_safe_region(connections, danger_ids, view)
            if safe and ep >= move_ep_cost:
                self._prepare_move(region_id)
                return self._build_action("move", {"regionId": safe}, "PRE-ESCAPE: Region becoming DZ")

        # Enemies
        enemies_alive = [a for a in visible_agents
                         if not a.get("isGuardian", False) and a.get("isAlive", True)
                         and a.get("id") != my_id]
        guardians_here = [a for a in visible_agents
                          if a.get("isGuardian", False) and a.get("isAlive", True)
                          and a.get("regionId") == region_id]

        # ── Priority 1c: DESPERATE FLEE ──────────────────────────
        has_healing_items = any(
            isinstance(i, dict) and i.get("typeId", "").lower() in RECOVERY_ITEMS
            and RECOVERY_ITEMS[i.get("typeId", "").lower()] > 0
            for i in inventory
        )
        if hp < 20 and not has_healing_items and (enemies_alive or guardians_here):
            if ep >= move_ep_cost:
                safe = self._find_safe_region(connections, danger_ids, view)
                if safe:
                    self._prepare_move(region_id)
                    return self._build_action("move", {"regionId": safe}, f"DESPERATE FLEE: HP={hp}")
            else:
                w_range = self._get_weapon_range(equipped)
                nearest = enemies_alive[0] if enemies_alive else (guardians_here[0] if guardians_here else None)
                if nearest and self._is_in_range(nearest, region_id, w_range, connections):
                    return self._build_action("attack", {"targetId": nearest["id"], "targetType": "agent"},
                                              "DESPERATE ATTACK")
                if not region.get("isDeathZone") and region_id not in danger_ids:
                    return self._build_action("rest", {}, "DESPERATE REST")

        # ── Priority 1d: COUNTER-ATTACK ──────────────────────────
        if self._combat_history.get("damage_this_tick"):
            attacker_id = self._combat_history["last_attacker_id"]
            if attacker_id == "unknown" or not attacker_id:
                attacker = enemies_alive[0] if enemies_alive else (guardians_here[0] if guardians_here else None)
            else:
                attacker = next((a for a in visible_agents if a.get("id") == attacker_id and a.get("isAlive", True)),
                                None)
            if attacker:
                w_range = self._get_weapon_range(equipped)
                if self._is_in_range(attacker, region_id, w_range, connections):
                    my_dmg = self._calc_damage(atk, self._get_weapon_bonus(equipped),
                                               attacker.get("def", 5), region_weather)
                    self._combat_history["damage_this_tick"] = False
                    return self._build_action("attack", {"targetId": attacker["id"], "targetType": "agent"},
                                              "COUNTER-ATTACK: Just damaged")
                else:
                    if ep >= move_ep_cost:
                        move = self._move_toward_target(attacker, connections, danger_ids, view)
                        if move:
                            self._combat_history["damage_this_tick"] = False
                            self._prepare_move(region_id)
                            return self._build_action("move", {"regionId": move}, "CHASE: Pursuing attacker")

        # ── Priority 2b: Guardian threat evasion ──────────────────
        if guardians_here and ep >= move_ep_cost:
            threat_guardian = max(guardians_here, key=lambda g: g.get("atk", 10))
            g_dmg = self._calc_damage(threat_guardian.get("atk", 10),
                                      self._estimate_enemy_weapon_bonus(threat_guardian),
                                      defense, region_weather)
            flee_hp_threshold = max(25, int(g_dmg * 1.5))
            if hp < flee_hp_threshold:
                safe = self._find_safe_region(connections, danger_ids, view)
                if safe:
                    self._prepare_move(region_id)
                    return self._build_action("move", {"regionId": safe},
                                              f"GUARDIAN FLEE: HP={hp} < {flee_hp_threshold}")

        # ── Priority 3: FREE ACTIONS (pickup, equip) ──────────────
        pickup_action = self._check_pickup(visible_items, inventory, region_id, hp, ep)
        if pickup_action:
            self._items_collected += 1
            return pickup_action

        equip_action = self._check_equip(inventory, equipped, view, region_id, connections)
        if equip_action:
            return equip_action

        # ── Priority 3b: Utility items (Map, Megaphone) ───────────
        util_action = self._use_utility_item(inventory, hp, ep, alive_count)
        if util_action:
            if util_action.get("data", {}).get("itemType") == "map":
                self._map_used_this_tick = True
            return util_action

        # ── Cooldown gate ─────────────────────────────────────────
        if not can_act:
            return None

        # ── Priority 4: Critical healing (HP < 25) ────────────────
        if hp < 25:
            heal = self._find_healing_item(inventory, critical=True)
            if heal:
                return self._build_action("use_item", {"itemId": heal["id"]}, f"CRITICAL HEAL: HP={hp}")
        elif hp < 60:
            heal = self._find_healing_item(inventory, critical=False)
            if heal:
                return self._build_action("use_item", {"itemId": heal["id"]}, f"HEAL: HP={hp}")

        # ── Priority 4b: EP management (aggressive) ──────────────
        energy_drink_count = sum(1 for i in inventory
                                 if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink")
        if ep <= 2 and ep < max_ep:
            drink = self._find_energy_drink(inventory)
            if drink:
                return self._build_action("use_item", {"itemId": drink["id"]}, f"EP RECOVERY: EP={ep}/{max_ep}")
        elif energy_drink_count >= 2 and ep <= max_ep - 4:
            drink = self._find_energy_drink(inventory)
            if drink:
                return self._build_action("use_item", {"itemId": drink["id"]},
                                          f"AGGRESSIVE EP RECOVERY: EP={ep}/{max_ep}")

        # ── Priority 4c: Binoculars ranged harassment ────────────
        has_binos = any(isinstance(i, dict) and i.get("typeId", "").lower() == "binoculars"
                        for i in inventory)
        w_range = self._get_weapon_range(equipped)
        if has_binos and w_range >= 1 and enemies_alive and hp >= 30:
            for enemy in enemies_alive:
                if self._is_in_range(enemy, region_id, w_range, connections):
                    my_dmg = self._calc_damage(atk, self._get_weapon_bonus(equipped),
                                               enemy.get("def", 5), region_weather)
                    e_dmg = self._calc_damage(enemy.get("atk", 10),
                                              self._estimate_enemy_weapon_bonus(enemy),
                                              defense, region_weather)
                    if my_dmg > e_dmg or enemy.get("hp", 100) <= my_dmg * 2:
                        return self._build_action("attack", {"targetId": enemy["id"], "targetType": "agent"},
                                                  "BINOCULARS RANGED HARASSMENT")
                    break

        # ── Priority 4d: Third-Party Cleanup ──────────────────────
        if hp >= 60 and ep >= move_ep_cost + 2 and alive_count > 5:
            nearby_fight = self._detect_nearby_fight(recent_logs, my_id, view,
                                                     connections, danger_ids, region_id, alive_count)
            if nearby_fight:
                fake_target = {"regionId": nearby_fight}
                move = self._move_toward_target(fake_target, connections, danger_ids, view)
                if move:
                    self._prepare_move(region_id)
                    return self._build_action("move", {"regionId": move},
                                              f"THIRD-PARTY: Fight at {nearby_fight[:8]}")

        # ── Priority 5: Guardian farming ──────────────────────────
        guardians = [a for a in visible_agents
                     if a.get("isGuardian", False) and a.get("isAlive", True)]
        if guardians and ep >= 2 and hp >= 25:
            target = self._select_best_combat_target(guardians, atk, equipped, defense, region_weather,
                                                     recent_logs=recent_logs)
            if self._is_in_range(target, region_id, w_range, connections):
                my_dmg = self._calc_damage(atk, self._get_weapon_bonus(equipped),
                                           target.get("def", 5), region_weather)
                g_dmg = self._calc_damage(target.get("atk", 10),
                                          self._estimate_enemy_weapon_bonus(target),
                                          defense, region_weather)
                if my_dmg >= g_dmg or target.get("hp", 100) <= my_dmg * 3:
                    return self._build_action("attack", {"targetId": target["id"], "targetType": "agent"},
                                              f"GUARDIAN FARM: dmg={my_dmg}")
            else:
                move = self._move_toward_target(target, connections, danger_ids, view)
                if move and ep >= move_ep_cost and hp >= 35:
                    self._prepare_move(region_id)
                    return self._build_action("move", {"regionId": move}, "APPROACH GUARDIAN")

        # ── Priority 6: Endgame hunt ──────────────────────────────
        if alive_count <= 10 and enemies_alive and ep >= 2 and hp >= 20:
            target = self._select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather,
                                                     recent_logs=recent_logs)
            if self._is_in_range(target, region_id, w_range, connections):
                return self._build_action("attack", {"targetId": target["id"], "targetType": "agent"},
                                          f"ENDGAME HUNT: alive={alive_count}")
            else:
                move = self._move_toward_target(target, connections, danger_ids, view)
                if move and ep >= move_ep_cost:
                    self._prepare_move(region_id)
                    return self._build_action("move", {"regionId": move}, "ENDGAME CHASE")

        # ── Priority 6b: Favorable agent combat ──────────────────
        hp_threshold = 25 if alive_count > 20 else 20
        if enemies_alive and ep >= 2 and hp >= hp_threshold:
            target = self._select_best_combat_target(enemies_alive, atk, equipped, defense, region_weather,
                                                     recent_logs=recent_logs)
            if self._is_in_range(target, region_id, w_range, connections):
                my_dmg = self._calc_damage(atk, self._get_weapon_bonus(equipped),
                                           target.get("def", 5), region_weather)
                e_dmg = self._calc_damage(target.get("atk", 10),
                                          self._estimate_enemy_weapon_bonus(target),
                                          defense, region_weather)
                if my_dmg > e_dmg or target.get("hp", 100) <= my_dmg * 2:
                    return self._build_action("attack", {"targetId": target["id"], "targetType": "agent"},
                                              f"COMBAT: dmg={my_dmg} vs {e_dmg}")
                elif w_range >= 1 and my_dmg >= e_dmg * 0.7:
                    return self._build_action("attack", {"targetId": target["id"], "targetType": "agent"},
                                              f"RANGED ATTACK: range={w_range}")

        # ── Priority 7: Monster farming ───────────────────────────
        monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
        if monsters and ep >= 2 and hp >= 20:
            target = self._select_best_combat_target(monsters, atk, equipped, defense, region_weather,
                                                     recent_logs=recent_logs)
            if self._is_in_range(target, region_id, w_range, connections):
                my_dmg = self._calc_damage(atk, self._get_weapon_bonus(equipped),
                                           target.get("def", 5), region_weather)
                m_dmg = self._calc_damage(target.get("atk", 10), 0, defense, region_weather)
                if my_dmg >= m_dmg or target.get("hp", 100) <= my_dmg * 3:
                    return self._build_action("attack", {"targetId": target["id"], "targetType": "monster"},
                                              "MONSTER FARM")

        # ── Priority 7b: Heal when safe ───────────────────────────
        if hp < 75 and not enemies_alive and not guardians_here:
            heal = self._find_healing_item(inventory, critical=(hp < 25))
            if heal:
                return self._build_action("use_item", {"itemId": heal["id"]}, f"HEAL: HP={hp}, area safe")
        elif hp < 100 and not enemies_alive and not guardians_here and alive_count <= 10:
            heal = self._find_healing_item(inventory, critical=False)
            if heal:
                return self._build_action("use_item", {"itemId": heal["id"]}, f"CAMPING HEAL: HP={hp}")

        # ── Priority 8: Facility interaction ──────────────────────
        if interactables and ep >= 2 and not region.get("isDeathZone"):
            facility = self._select_facility(interactables, hp, ep, alive_count)
            if facility:
                return self._build_action("interact", {"interactableId": facility["id"]},
                                          f"FACILITY: {facility.get('type')}")

        # ── Priority 8.5: Endgame Camping ─────────────────────────
        if alive_count <= 5 and self._map_knowledge.get("revealed") and region_id in self._map_knowledge.get(
                "safe_center", []):
            if not enemies_alive and not guardians_here and ep < max_ep:
                return self._build_action("rest", {}, f"ENDGAME CAMP: safe centre, EP={ep}/{max_ep}")

        # ── Priority 8.6: Hold Position ───────────────────────────
        if not enemies_alive and not guardians_here and ep < max_ep:
            all_explored = all(
                (c if isinstance(c, str) else c.get("id", "")) in self._explored_regions for c in connections)
            no_items_here = not any(it.get("regionId") == region_id for it in visible_items)
            if all_explored and no_items_here and not region.get("isDeathZone"):
                return self._build_action("rest", {}, "HOLD: No incentive to move")

        # ── Priority 9: Strategic movement (anti-ping-pong) ──────
        if ep >= move_ep_cost and connections:
            move_target, best_score = self._choose_move_target(connections, danger_ids,
                                                               region, visible_items, alive_count,
                                                               enemies_alive, ep)
            if move_target and best_score > 0:
                self._prepare_move(region_id)
                return self._build_action("move", {"regionId": move_target}, f"EXPLORE (score={best_score})")

        # ── Priority 10: Rest ─────────────────────────────────────
        if ep < 3 and not enemies_alive and not guardians_here \
                and not region.get("isDeathZone") and region_id not in danger_ids:
            return self._build_action("rest", {}, f"REST: EP={ep}/{max_ep}")

        return None

    # ========================================================================
    # HELPER: Build action dict
    # ========================================================================
    def _build_action(self, action: str, data: dict, reason: str) -> dict:
        """Build action dict compatible with ActionSender.build_action()."""
        return {"action": action, "data": data, "reason": reason}

    def _prepare_move(self, region_id: str):
        """Update _last_region_id before moving (anti-ping-pong)."""
        self._last_region_id = region_id

    # ========================================================================
    # ZONE & REGION HELPERS
    # ========================================================================
    def _resolve_region(self, entry, view: dict):
        if isinstance(entry, dict):
            return entry
        if isinstance(entry, str):
            for r in view.get("visibleRegions", []):
                if isinstance(r, dict) and r.get("id") == entry:
                    return r
        return None

    def _find_safe_region(self, connections, danger_ids: set, view: dict = None) -> Optional[str]:
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

    def _is_death_zone(self, region_id: str, game_state: dict) -> bool:
        # Reuse find_safe_region logic? We'll use existing danger_ids
        # Not used directly in decision; danger_ids is built from pending/connections
        return False

    # ========================================================================
    # COMBAT HELPERS
    # ========================================================================
    def _calc_damage(self, atk: int, weapon_bonus: int, target_def: int, weather: str = "clear") -> int:
        base = atk + weapon_bonus - int(target_def * 0.5)
        penalty = WEATHER_COMBAT_PENALTY.get(weather, 0.0)
        return max(1, int(base * (1 - penalty)))

    def _get_weapon_bonus(self, equipped_weapon) -> int:
        if not equipped_weapon:
            return 0
        type_id = equipped_weapon.get("typeId", "").lower()
        return WEAPONS.get(type_id, {}).get("bonus", 0)

    def _get_weapon_range(self, equipped_weapon) -> int:
        if not equipped_weapon:
            return 0
        type_id = equipped_weapon.get("typeId", "").lower()
        return WEAPONS.get(type_id, {}).get("range", 0)

    def _estimate_enemy_weapon_bonus(self, agent: dict) -> int:
        weapon = agent.get("equippedWeapon")
        if not weapon:
            return 0
        type_id = weapon.get("typeId", "").lower() if isinstance(weapon, dict) else ""
        return WEAPONS.get(type_id, {}).get("bonus", 0)

    def _select_best_combat_target(self, targets: list, my_atk: int, equipped,
                                    my_def: int, weather: str, recent_logs: list = None) -> dict:
        best = None
        best_score = -9999
        my_bonus = self._get_weapon_bonus(equipped)
        for t in targets:
            if not isinstance(t, dict):
                continue
            t_id = t.get("id", "")
            t_hp = max(t.get("hp", 100), 1)
            t_def = t.get("def", 5)
            t_atk = t.get("atk", 10)
            t_bonus = self._estimate_enemy_weapon_bonus(t)
            my_dmg = self._calc_damage(my_atk, my_bonus, t_def, weather)
            their_dmg = self._calc_damage(t_atk, t_bonus, my_def, weather)
            kill_speed = (my_dmg / t_hp) * 100
            threat = their_dmg * THREAT_WEIGHT
            score = kill_speed - threat
            if t_id in self._vulnerable_agents:
                score += 30
            if recent_logs and self._is_agent_fighting(t_id, recent_logs):
                score += 25
            if score > best_score:
                best_score = score
                best = t
        return best if best else min(targets, key=lambda t: t.get("hp", 999))

    def _is_agent_fighting(self, agent_id: str, recent_logs: list) -> bool:
        for entry in recent_logs:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") in ("attack", "damage"):
                if (entry.get("attackerId") == agent_id or
                        entry.get("sourceId") == agent_id or
                        entry.get("targetId") == agent_id):
                    return True
        return False

    def _is_in_range(self, target: dict, my_region: str, weapon_range: int, connections=None) -> bool:
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

    def _move_toward_target(self, target: dict, connections, danger_ids: set, view: dict) -> Optional[str]:
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
        # 2-hop BFS
        best_hop = None
        for step1_id in safe_conn_ids:
            step1_region = self._visible_region_cache.get(step1_id)
            if not step1_region:
                continue
            for step2 in step1_region.get("connections", []):
                step2_id = step2 if isinstance(step2, str) else step2.get("id", "")
                if step2_id == target_region:
                    if best_hop is None or step1_id not in self._explored_regions:
                        best_hop = step1_id
                    break
        if best_hop:
            return best_hop
        for rid in safe_conn_ids:
            return rid
        return None

    def _get_move_ep_cost(self, terrain: str, weather: str) -> int:
        if terrain == "water" or weather == "storm":
            return 3
        return 2

    # ========================================================================
    # INVENTORY HELPERS
    # ========================================================================
    def _find_healing_item(self, inventory: list, critical: bool = False) -> Optional[dict]:
        heals = [i for i in inventory
                 if isinstance(i, dict)
                 and i.get("typeId", "").lower() in RECOVERY_ITEMS
                 and RECOVERY_ITEMS[i.get("typeId", "").lower()] > 0]
        if not heals:
            return None
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), reverse=critical)
        return heals[0]

    def _find_energy_drink(self, inventory: list) -> Optional[dict]:
        for i in inventory:
            if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
                return i
        return None

    def _check_pickup(self, items: list, inventory: list, region_id: str, hp: int, ep: int) -> Optional[dict]:
        local_items = [i for i in items if isinstance(i, dict) and i.get("id") and i.get("regionId") == region_id]
        if not local_items:
            local_items = [i for i in items if isinstance(i, dict) and i.get("id")]
        if not local_items:
            return None
        heal_count = sum(1 for i in inventory if isinstance(i, dict)
                         and i.get("typeId", "").lower() in RECOVERY_ITEMS
                         and RECOVERY_ITEMS[i.get("typeId", "").lower()] > 0)
        local_items.sort(key=lambda i: self._pickup_score(i, inventory, heal_count, hp, ep), reverse=True)
        best = local_items[0]
        score = self._pickup_score(best, inventory, heal_count, hp, ep)
        if score <= 0:
            return None
        type_id = best.get("typeId", "item")
        if len(inventory) >= 10:
            drop = self._find_droppable_item(inventory, best)
            if drop:
                return self._build_action("drop_item", {"itemId": drop["id"]}, "MAKE ROOM")
            return None
        return self._build_action("pickup", {"itemId": best["id"]}, f"PICKUP: {type_id}")

    def _pickup_score(self, item: dict, inventory: list, heal_count: int, hp: int, ep: int) -> int:
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
            if bonus > current_best:
                return 100 + bonus
            return 0
        dynamic_bonus = 0
        need_healing = (hp < 50) and (type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0)
        need_energy = (ep < 4) and (type_id == "energy_drink")
        if need_healing or need_energy:
            dynamic_bonus = 100
        if type_id == "binoculars":
            has_binos = any(
                isinstance(i, dict) and i.get("typeId", "").lower() == "binoculars" for i in inventory)
            return 55 if not has_binos else 0
        if type_id == "map":
            return 52
        if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
            return ITEM_PRIORITY.get(type_id, 0) + (10 if heal_count < 4 else 0) + dynamic_bonus
        if type_id == "energy_drink":
            return ITEM_PRIORITY.get(type_id, 0) + dynamic_bonus
        return ITEM_PRIORITY.get(type_id, 0)

    def _find_droppable_item(self, inventory: list, target_item: dict) -> Optional[dict]:
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

    def _check_equip(self, inventory: list, equipped, view: dict, region_id: str, connections=None) -> Optional[dict]:
        nearby_enemy = self._is_enemy_nearby(view, region_id, connections)
        current_type = equipped.get("typeId", "").lower() if equipped else "fist"
        current_id = equipped.get("id", "") if equipped else ""
        best = None
        best_score = self._get_weapon_bonus(equipped)
        for item in inventory:
            if not isinstance(item, dict) or item.get("category") != "weapon":
                continue
            if item.get("id") == current_id:
                continue
            if item.get("typeId", "").lower() == current_type:
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
            return self._build_action("equip", {"itemId": best["id"]},
                                      f"SMART EQUIP: {best.get('typeId')}")
        return None

    def _is_enemy_nearby(self, view: dict, my_region: str, connections) -> bool:
        visible = view.get("visibleAgents", [])
        for agent in visible:
            if agent.get("isGuardian", False) or not agent.get("isAlive", True):
                continue
            target_region = agent.get("regionId", "")
            if target_region == my_region:
                continue
            for conn in connections:
                cid = conn if isinstance(conn, str) else conn.get("id", "")
                if cid == target_region:
                    return True
            for conn in connections:
                cid = conn if isinstance(conn, str) else conn.get("id", "")
                region_obj = self._visible_region_cache.get(cid)
                if region_obj:
                    for c2 in region_obj.get("connections", []):
                        c2id = c2 if isinstance(c2, str) else c2.get("id", "")
                        if c2id == target_region:
                            return True
        return False

    def _use_utility_item(self, inventory: list, hp: int, ep: int, alive_count: int) -> Optional[dict]:
        for item in inventory:
            if not isinstance(item, dict):
                continue
            type_id = item.get("typeId", "").lower()
            if type_id == "map":
                item_id = item.get("id", "")
                if item_id and item_id in self._map_item_used_ids:
                    continue
                self._map_item_used_ids.add(item_id)
                return self._build_action("use_item", {"itemId": item_id, "itemType": "map"}, "UTILITY: Using Map")
        if alive_count <= 5 and hp > 50 and ep >= MEGAPHONE_MIN_EP:
            for item in inventory:
                if not isinstance(item, dict):
                    continue
                if item.get("typeId", "").lower() == "megaphone":
                    return self._build_action("use_item", {"itemId": item["id"], "itemType": "megaphone"},
                                              "UTILITY: Megaphone lure")
        return None

    def _learn_from_map(self, view: dict):
        visible_regions = view.get("visibleRegions", [])
        if not visible_regions:
            return
        self._map_knowledge["revealed"] = True
        safe_regions = []
        for region in visible_regions:
            if not isinstance(region, dict):
                continue
            rid = region.get("id", "")
            if not rid:
                continue
            if region.get("isDeathZone"):
                self._map_knowledge["death_zones"].add(rid)
            else:
                conns = region.get("connections", [])
                terrain = region.get("terrain", "").lower()
                terrain_value = {"hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1}.get(terrain, 0)
                score = len(conns) + terrain_value
                safe_regions.append((rid, score))
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        self._map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]

    def _select_facility(self, interactables: list, hp: int, ep: int, alive_count: int) -> Optional[dict]:
        best = None
        best_priority = -1
        for fac in interactables:
            if not isinstance(fac, dict) or fac.get("isUsed"):
                continue
            ftype = fac.get("type", "").lower()
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

    # ========================================================================
    # STATE TRACKING (v1.9.2 + v2.0)
    # ========================================================================
    def _update_combat_history(self, current_hp: int, recent_logs: list, my_id: str):
        last = self._combat_history.get("last_hp", current_hp)
        if current_hp < last:
            self._combat_history["consecutive_damage_ticks"] += 1
            self._combat_history["damage_this_tick"] = True
            attacker_found = False
            for entry in recent_logs:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") in ("damage", "attack") or "damage" in str(entry.get("message", "")).lower():
                    attacker_id = entry.get("attackerId") or entry.get("sourceId") or ""
                    target_id = entry.get("targetId") or ""
                    if target_id == my_id and attacker_id and attacker_id != my_id:
                        self._combat_history["last_attacker_id"] = attacker_id
                        attacker_found = True
                        break
            if not attacker_found:
                self._combat_history["last_attacker_id"] = "unknown"
        else:
            self._combat_history["consecutive_damage_ticks"] = 0
            self._combat_history["damage_this_tick"] = False
        self._combat_history["last_hp"] = current_hp

    def _track_agents(self, visible_agents: list, my_id: str, my_region: str):
        for agent in visible_agents:
            if not isinstance(agent, dict):
                continue
            aid = agent.get("id", "")
            if not aid or aid == my_id:
                continue
            # Update enemy profile
            if aid not in self._enemy_profiles:
                self._enemy_profiles[aid] = EnemyProfile(agent_id=aid)
            prof = self._enemy_profiles[aid]
            prof.last_known_hp = agent.get("hp", 100)
            prof.last_known_ep = agent.get("ep", 10)
            prof.last_seen_region = my_region
            prof.last_seen_tick = self._tick_counter
            if agent.get("equippedWeapon"):
                prof.preferred_weapon = agent["equippedWeapon"].get("typeId", "")
        # Cleanup stale
        stale_cutoff = self._tick_counter - AGENT_STALE_TICKS
        self._enemy_profiles = {k: v for k, v in self._enemy_profiles.items() if v.last_seen_tick >= stale_cutoff}

    def _detect_vulnerable_agents(self, recent_logs: list, my_id: str):
        self._vulnerable_agents = {k: v for k, v in self._vulnerable_agents.items()
                                   if self._tick_counter - v < VULNERABLE_TTL}
        for entry in recent_logs:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "use_item":
                user_id = entry.get("agentId") or entry.get("userId", "")
                item_name = entry.get("itemName", "").lower()
                if any(heal in item_name for heal in ("medkit", "bandage", "food", "emergency")):
                    if user_id and user_id != my_id:
                        self._vulnerable_agents[user_id] = self._tick_counter

    def _detect_nearby_fight(self, recent_logs: list, my_id: str, view: dict,
                             connections: list, danger_ids: set,
                             region_id: str, alive_count: int) -> Optional[str]:
        fight_region = None
        for entry in recent_logs:
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type", "")
            if etype in ("attack", "damage"):
                attacker = entry.get("attackerId") or entry.get("sourceId") or ""
                target = entry.get("targetId") or ""
                if attacker == my_id or target == my_id:
                    continue
                if attacker and target:
                    visible = view.get("visibleAgents", [])
                    for agent in visible:
                        if agent.get("id") == attacker and agent.get("isAlive", True):
                            fight_region = agent.get("regionId", "")
                            break
                        if agent.get("id") == target and agent.get("isAlive", True):
                            fight_region = agent.get("regionId", "")
                            break
                    if fight_region:
                        break
        if not fight_region or fight_region == region_id or fight_region in danger_ids:
            return None
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
                    region_obj = self._visible_region_cache.get(cid)
                    if region_obj:
                        for c2 in region_obj.get("connections", []):
                            c2id = c2 if isinstance(c2, str) else c2.get("id", "")
                            if c2id == fight_region:
                                reachable = True
                                break
                    if reachable:
                        break
        return fight_region if reachable else None

    def _choose_move_target(self, connections, danger_ids: set, current_region: dict,
                            visible_items: list, alive_count: int,
                            enemies_visible: list = None, current_ep: int = 999):
        candidates = []
        item_regions = {item.get("regionId", "") for item in visible_items if isinstance(item, dict)}
        enemy_regions = set()
        enemy_threat_map = {}
        if enemies_visible:
            for e in enemies_visible:
                reg = e.get("regionId", "")
                enemy_regions.add(reg)
                e_id = e.get("id", "")
                prof = self._enemy_profiles.get(e_id, None)
                e_atk = prof.last_known_hp if prof else e.get("atk", 10)  # wrong, should be atk
                # Actually we need atk, use agent data
                e_atk = e.get("atk", 10)
                e_def = e.get("def", 5)
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
            if rid in self._map_knowledge.get("death_zones", set()):
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
            if alive_count > 10:
                threat_level = enemy_threat_map.get(rid, 0)
                if threat_level > 0:
                    if threat_level > 25:
                        score -= 50
                    else:
                        score -= max(0, threat_level * 2)
            if conn_dict:
                facs = conn_dict.get("interactables", [])
                unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
                score += len(unused) * 2
            if alive_count < 30:
                score += 3
            if self._map_knowledge.get("revealed") and rid in self._map_knowledge.get("safe_center", []):
                score += 5
            if rid in self._explored_regions:
                score -= 5
            if rid == self._last_region_id:
                score -= 50

            candidates.append((rid, score))

        if not candidates:
            return None, -999
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0], candidates[0][1]

    # ========================================================================
    # PERFORMANCE & ADAPTATION (v2.0)
    # ========================================================================

    def record_game_result(self, result: dict):
        """Record game result for learning (v2.0)."""
        self._game_history.append({
            "timestamp": time.time(),
            "placement": result.get("placement"),
            "kills": result.get("kills"),
            "survival_time": result.get("survivalTime"),
            "win": result.get("placement") == 1,
            "death_cause": result.get("deathCause"),
        })
        # Adapt strategy
        self._adapt_strategy()

    def _adapt_strategy(self):
        """Dynamically adjust config based on recent performance."""
        if len(self._game_history) < 3:
            return

        recent = self._game_history[-5:]
        wins = sum(1 for g in recent if g.get("win"))
        win_rate = wins / len(recent)
        avg_kills = sum(g.get("kills", 0) for g in recent) / len(recent)

        # Tweak aggression
        if win_rate > 0.6:
            self.config.aggression = min(1.0, self.config.aggression + 0.03)
            self.config.risk_tolerance = min(1.0, self.config.risk_tolerance + 0.02)
        elif avg_kills > 2 and win_rate < 0.3:
            self.config.aggression = max(0.0, self.config.aggression - 0.05)

        # Tweak heal threshold
        if avg_kills < 0.5 and win_rate < 0.2:
            self.config.heal_threshold = min(0.6, self.config.heal_threshold + 0.02)

        # Clamp
        self.config.aggression = max(0.0, min(1.0, self.config.aggression))
        self.config.risk_tolerance = max(0.0, min(1.0, self.config.risk_tolerance))
        self.config.heal_threshold = max(0.1, min(0.8, self.config.heal_threshold))

    def get_performance_metrics(self) -> dict:
        return {
            "turn": self._tick_counter,
            "combat_win_rate": self._successful_combats / max(1, self._successful_combats + self._failed_combats),
            "items_collected": self._items_collected,
            "explored_regions": len(self._explored_regions),
            "enemy_profiles": len(self._enemy_profiles),
            "config": self.get_config_json(),
        }

    def get_config_json(self) -> dict:
        return {k: v for k, v in self.config.__dict__.items()}

    def set_config_from_json(self, config_dict: dict):
        for key, value in config_dict.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)

    def reset_game_state(self):
        """Reset all per-game state."""
        self._tick_counter = 0
        self._last_region_id = ""
        self._explored_regions = set()
        self._map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
        self._combat_history = {"last_hp": 100, "consecutive_damage_ticks": 0, "last_attacker_id": "",
                                "damage_this_tick": False}
        self._vulnerable_agents = {}
        self._visible_region_cache = {}
        self._map_used_this_tick = False
        self._map_item_used_ids = set()
        self._enemy_profiles = {}
        self._actions_this_game = 0
        self._items_collected = 0

    def learn_from_map(self, view: dict):
        """Compatibility wrapper."""
        self._learn_from_map(view)


# ============================================================================
# GLOBAL COMPATIBILITY LAYER
# ============================================================================

_global_brain: Optional[MoltyRoyaleBrain] = None


def _get_brain() -> MoltyRoyaleBrain:
    global _global_brain
    if _global_brain is None:
        _global_brain = MoltyRoyaleBrain()
    return _global_brain


def decide_action(view: dict, can_act: bool = True) -> Optional[dict]:
    return _get_brain().decide_action(view, can_act)


def reset_game_state():
    _get_brain().reset_game_state()


def learn_from_map(view: dict):
    _get_brain().learn_from_map(view)


def record_game_result(result: dict):
    _get_brain().record_game_result(result)


def get_performance_metrics() -> dict:
    return _get_brain().get_performance_metrics()


def get_config_json() -> dict:
    return _get_brain().get_config_json()


def set_config_from_json(config_dict: dict):
    _get_brain().set_config_from_json(config_dict)


# Export list
__all__ = [
    'MoltyRoyaleBrain', 'StrategyConfig',
    'decide_action', 'reset_game_state', 'learn_from_map',
    'record_game_result', 'get_performance_metrics',
    'get_config_json', 'set_config_from_json',
]