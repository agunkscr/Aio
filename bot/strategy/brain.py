"""
Strategy brain — BERSERKER MODE v8.0 (TACTICAL PREDATOR)
===========================================================================
FITUR UTAMA v8.0:
- TACTICAL RETREAT: Mundur saat duel diprediksi kalah, lalu heal & counter
- SMART TARGET: Eksekusi ancaman tertinggi, bukan sekadar HP rendah
- PRE-HEAL: Selalu full HP sebelum duel
- RANGE-BASED WEAPON SWITCH: Ganti senjata sesuai posisi musuh
- MAP CONTROL: Bergerak menuju posisi sentral, hindari deathzone
- ENEMY PROFILE: Pelajari gaya bertarung & winrate, blacklist aktif
- FREE ACTIONS: Otomatis pickup/equip tanpa buang giliran (jika engine support)
===========================================================================
"""

import time, math
from collections import defaultdict, deque
from enum import Enum
from bot.utils.logger import get_logger

log = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════
# KONFIGURASI SENJATA
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

RECOVERY_ITEMS = {
    "medkit": 50, "bandage": 30, "emergency_food": 20, "energy_drink": 0,
}

WEATHER_COMBAT_PENALTY = {
    "clear": 0.0, "rain": 0.05, "fog": 0.10, "storm": 0.15,
}

# ═══════════════════════════════════════════════════════════════════
# KONFIGURASI TACTICAL PREDATOR v8.0
# ═══════════════════════════════════════════════════════════════════
TACTIC_CONFIG = {
    "HP_PRE_HEAL": 60,              # Di bawah ini, heal sebelum cari musuh
    "HP_EMERGENCY": 25,             # Di bawah ini, wajib heal/mundur
    "HP_SAFE": 70,                  # Di atas ini anggap aman untuk serang

    "ADVANTAGE_TURN_DIFF": 1,       # Serang hanya jika kita menang ≤ selisih ini
    "MIN_DAMAGE_TO_SERANG": 3,      # Minimal damage kita (setelah pengurangan def)

    "FLEE_IF_OUTNUMBERED": 2,       # Mundur jika musuh di region > 1 & kita tidak bisa one-shot
    "BLACKLIST_WINRATE": 0.7,       # Blacklist jika kalah sering (≥70%)
    "PURSUIT_MAX_HOPS": 3,         # Kejar maks 3 region, lalu berhenti
}

# ═══════════════════════════════════════════════════════════════════
# ENEMY MEMORY (ENHANCED)
# ═══════════════════════════════════════════════════════════════════
class PlayerStyle(Enum):
    AGGRESSOR = "aggressor"
    KITER = "kiter"
    HEALER = "healer"
    CAMPER = "camper"
    CAREFUL = "careful"
    UNKNOWN = "unknown"

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
        self.style = PlayerStyle.UNKNOWN
        self.heal_used_recently = False   # turn terakhir pakai heal?
        self.last_ep_seen = 0

    def record_real_damage(self, damage: int):
        self.real_damage_samples.append(damage)
        if len(self.real_damage_samples) > 5:
            self.real_damage_samples.pop(0)
        self.estimated_damage = sum(self.real_damage_samples) // max(1, len(self.real_damage_samples))
        if self.estimated_damage >= 70:
            self.is_blacklisted = True
            self.blacklist_reason = f"damage={self.estimated_damage}"

    def record_attacked_us(self, turn: int, damage: int):
        self.attacked_us_count += 1
        self.last_attacked_turn = turn
        self.record_real_damage(damage)

    def update_style(self, action_type: str):
        # Heuristic: perbarui gaya berdasarkan aksi musuh yang terlihat
        if action_type == "flee":
            self.style = PlayerStyle.CAREFUL if self.style != PlayerStyle.AGGRESSOR else self.style
        elif action_type == "attack" and self.attacked_us_count > 2:
            self.style = PlayerStyle.AGGRESSOR
        elif action_type == "use_item":
            self.heal_used_recently = True

    def record_combat(self, combat_data: dict):
        self.encounters += 1
        self.combat_logs.append(combat_data)
        self.last_encounter_turn = combat_data.get("turn", 0)
        result = combat_data.get("result", "unknown")
        if result == "loss":
            self.victories_against_us += 1
        elif result == "win":
            self.defeats_by_us += 1
        # Update blacklist berdasarkan winrate
        if self.encounters >= 3 and (self.victories_against_us / self.encounters) >= TACTIC_CONFIG["BLACKLIST_WINRATE"]:
            self.is_blacklisted = True
            self.blacklist_reason = f"high_winrate={self.victories_against_us}/{self.encounters}"

# Global memory store
_enemy_memories: dict = {}

def get_or_create_memory(enemy_id: str) -> EnemyMemory:
    global _enemy_memories
    if enemy_id not in _enemy_memories:
        _enemy_memories[enemy_id] = EnemyMemory(enemy_id)
    return _enemy_memories[enemy_id]

# ═══════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS (sama seperti sebelumnya, dengan tambahan)
# ═══════════════════════════════════════════════════════════════════
def calc_damage(atk: int, weapon_bonus: int, target_def: int, weather: str = "clear") -> int:
    base = atk + weapon_bonus - int(target_def * 0.5)
    penalty = WEATHER_COMBAT_PENALTY.get(weather, 0.0)
    return max(1, int(base * (1 - penalty)))

def get_weapon_bonus(weapon: dict) -> int:
    if not weapon: return 0
    return WEAPONS.get(weapon.get("typeId", "").lower(), {}).get("bonus", 0)

def get_weapon_damage(weapon: dict) -> int:
    if not weapon: return WEAPONS["fist"]["damage"]
    return WEAPONS.get(weapon.get("typeId", "").lower(), WEAPONS["fist"]).get("damage", 4)

def get_weapon_range(weapon: dict) -> int:
    if not weapon: return 0
    return WEAPONS.get(weapon.get("typeId", "").lower(), {}).get("range", 0)

def _resolve_region(entry, view: dict):
    if isinstance(entry, dict): return entry
    if isinstance(entry, str):
        for r in view.get("visibleRegions", []):
            if isinstance(r, dict) and r.get("id") == entry: return r
    return None

def _get_move_ep_cost(terrain: str, weather: str) -> int:
    if terrain == "water": return 3
    if weather == "storm": return 3
    return 2

def _is_in_range(target: dict, my_region: str, weapon_range: int, connections=None) -> bool:
    target_region = target.get("regionId", "")
    if not target_region or target_region == my_region:
        return True
    if weapon_range >= 1 and connections:
        adj_ids = set()
        for conn in connections:
            if isinstance(conn, str): adj_ids.add(conn)
            elif isinstance(conn, dict): adj_ids.add(conn.get("id", ""))
        if target_region in adj_ids:
            return True
    return False

def _find_healing_item(inventory: list) -> dict | None:
    heals = [i for i in inventory if isinstance(i, dict)
             and i.get("typeId", "").lower() in RECOVERY_ITEMS
             and RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0) > 0]
    return max(heals, key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), default=None)

def _select_best_weapon(inventory: list, current_weapon: dict, target_in_region: bool, target_in_adjacent: bool) -> dict | None:
    """Pilih senjata optimal berdasarkan posisi musuh."""
    best = current_weapon
    best_dmg = get_weapon_damage(current_weapon)
    for item in inventory:
        if not isinstance(item, dict) or item.get("category") != "weapon":
            continue
        wpn_range = get_weapon_range(item)
        wpn_dmg = get_weapon_damage(item)
        # Jika musuh di region kita, ranged (>0) tidak berguna, pilih melee (range 0) bila lebih kuat
        if target_in_region:
            if wpn_range == 0 and wpn_dmg > best_dmg:
                best, best_dmg = item, wpn_dmg
            elif wpn_range > 0 and wpn_dmg > best_dmg and get_weapon_range(current_weapon) > 0:
                # Jika kita juga ranged, boleh upgrade, tapi melee lebih baik
                continue
        # Jika musuh di adjacent, kita butuh ranged, pilih range 1 atau 2 yang damage-nya tertinggi
        elif target_in_adjacent:
            if wpn_range >= 1 and wpn_dmg > best_dmg:
                best, best_dmg = item, wpn_dmg
        # Tidak ada target, pilih damage tertinggi (bebas)
        else:
            if wpn_dmg > best_dmg:
                best, best_dmg = item, wpn_dmg
    return best if best != current_weapon else None

def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None

def _simulate_duel(my_hp, my_dmg, enemy_hp, enemy_dmg) -> (int, int):
    """Return (turns_to_kill_enemy, turns_enemy_kill_me)."""
    if my_dmg <= 0: my_dmg = 1
    if enemy_dmg <= 0: enemy_dmg = 1
    ttk = math.ceil(enemy_hp / my_dmg)
    ttd = math.ceil(my_hp / enemy_dmg)
    return ttk, ttd

# ═══════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════
_known_agents: dict = {}
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": []}
_hunting_target: dict | None = None
_hunting_timer: int = 0
_interacted_facilities: dict = {}
_last_attacked_by: str | None = None
_last_attacked_turn: int = 0
_last_attacked_damage: int = 0
_revenge_target: str | None = None
_last_heal_turn: int = 0
_post_heal_safe_turns: int = 0
_broadcast_used: bool = False

def reset_game_state():
    global _known_agents, _map_knowledge, _hunting_target, _hunting_timer
    global _interacted_facilities, _last_attacked_by, _last_attacked_turn
    global _last_attacked_damage, _revenge_target, _last_heal_turn, _post_heal_safe_turns, _broadcast_used
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _hunting_target = None
    _hunting_timer = 0
    _interacted_facilities = {}
    _last_attacked_by = None
    _last_attacked_turn = 0
    _last_attacked_damage = 0
    _revenge_target = None
    _last_heal_turn = 0
    _post_heal_safe_turns = 0
    _broadcast_used = False
    log.info("🧠 TACTICAL PREDATOR v8.0 ready.")

def on_attacked_by(attacker_id: str, current_turn: int, damage: int = None):
    global _last_attacked_by, _last_attacked_turn, _last_attacked_damage, _revenge_target
    _last_attacked_by = attacker_id
    _last_attacked_turn = current_turn
    _last_attacked_damage = damage or 0
    _revenge_target = attacker_id
    get_or_create_memory(attacker_id).record_attacked_us(current_turn, damage or 10)
    log.warning(f"⚠️ ATTACKED by {attacker_id[:8]} for {damage} dmg")

def on_enemy_killed(enemy_id: str):
    global _hunting_target, _revenge_target
    if _hunting_target and _hunting_target.get("id") == enemy_id:
        _hunting_target = None
    if _revenge_target == enemy_id:
        _revenge_target = None
    log.info(f"✅ KILLED {enemy_id[:8]}")

def on_we_died(killer_id: str, combat_summary: dict = None):
    log.warning(f"💀 DIED by {killer_id[:8]}")
    if combat_summary:
        get_or_create_memory(killer_id).record_combat({
            "result": "loss",
            "turn": combat_summary.get("turn", 0),
            "my_hp_final": combat_summary.get("my_hp_final", 0),
            "enemy_hp_final": combat_summary.get("enemy_hp_final", 100),
        })
    reset_game_state()

# ═══════════════════════════════════════════════════════════════════
# MAIN DECISION ENGINE v8.0 - TACTICAL PREDATOR
# ═══════════════════════════════════════════════════════════════════
def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    global _hunting_target, _hunting_timer, _revenge_target, _last_attacked_by, _last_attacked_turn
    global _post_heal_safe_turns

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

    # Normalize items
    visible_items = []
    for entry in visible_items_raw:
        if not isinstance(entry, dict): continue
        inner = entry.get("item")
        if isinstance(inner, dict):
            inner["regionId"] = entry.get("regionId", "")
            visible_items.append(inner)
        elif entry.get("id"):
            visible_items.append(entry)

    connections = view.get("connectedRegions", []) or region.get("connections", [])
    pending_dz = view.get("pendingDeathzones", [])
    current_turn = view.get("turn", 0)
    region_id = region.get("id", "")
    region_terrain = region.get("terrain", "").lower()
    region_weather = region.get("weather", "").lower()

    # Free action cooldown (untuk memastikan free action dijalankan di awal giliran)
    if _post_heal_safe_turns > 0:
        _post_heal_safe_turns -= 1

    if not is_alive:
        return None

    # Update hunting timer
    if _hunting_timer > 0:
        _hunting_timer -= 1
    elif _hunting_target:
        _hunting_target = None

    # Daftar deathzone
    danger_ids = set()
    for dz in pending_dz:
        if isinstance(dz, dict): danger_ids.add(dz.get("id", ""))
        elif isinstance(dz, str): danger_ids.add(dz)
    for conn in connections:
        resolved = _resolve_region(conn, view)
        if resolved and resolved.get("isDeathZone"):
            danger_ids.add(resolved.get("id", ""))

    # Populasi agen terlihat
    for agent in visible_agents:
        if isinstance(agent, dict) and agent.get("id") != my_id:
            _known_agents[agent["id"]] = agent

    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)
    ep_ratio = ep / max_ep if max_ep > 0 else 1.0

    # Agent di region kita
    enemies_here = [a for a in visible_agents
                    if a.get("isAlive", True) and a.get("id") != my_id
                    and a.get("regionId") == region_id
                    and not a.get("isGuardian", False)]
    guardians_here = [a for a in visible_agents if a.get("isGuardian") and a.get("regionId") == region_id]
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]

    just_attacked = (current_turn - _last_attacked_turn) <= 2 and _last_attacked_by

    # ── 1. FREE ACTIONS: Equip dan Pickup (jika memungkinkan tanpa buang giliran) ──
    # Asumsinya, engine mendukung. Jika tidak, kita kembalikan sebagai aksi pertama.
    # Di sini kita cek langsung, karena jika engine support, kita bisa langsung lanjut.
    # Untuk kompatibilitas, kita akan jadikan prioritas tinggi jika tidak ada ancaman instan.

    # Cek senjata lebih baik berdasarkan musuh
    target_in_region = len(enemies_here) > 0
    target_in_adjacent = any(
        a.get("regionId") != region_id and a.get("regionId") in [c.get("id") if isinstance(c, dict) else c for c in connections]
        for a in visible_agents if a.get("id") != my_id and not a.get("isGuardian")
    )
    better_weapon = _select_best_weapon(inventory, equipped, target_in_region, target_in_adjacent)
    if better_weapon and ep >= 1:  # equip mungkin perlu EP? Atau benar-benar gratis. Anggap gratis.
        log.info(f"🔁 FREE EQUIP: {better_weapon.get('typeId')}")
        return {"action": "equip", "data": {"itemId": better_weapon["id"]}, "reason": "TACTICAL_EQUIP"}

    # Ambil item jika ada yang berharga
    local_items = [i for i in visible_items if i.get("regionId") == region_id]
    if local_items:
        # Prioritas: senjata upgrade, healing, lalu lainnya
        for item in local_items:
            if item.get("category") == "weapon":
                wpn_dmg = get_weapon_damage(item)
                if wpn_dmg > get_weapon_damage(equipped):
                    log.info(f"📦 PICKUP WEAPON: {item.get('typeId')}")
                    return {"action": "pickup", "data": {"itemId": item["id"]}, "reason": "FREE_WEAPON_PICKUP"}
        # healing jika HP < safe
        if hp < TACTIC_CONFIG["HP_SAFE"]:
            for item in local_items:
                if item.get("typeId", "").lower() in RECOVERY_ITEMS:
                    log.info(f"💊 PICKUP HEAL: {item.get('typeId')}")
                    return {"action": "pickup", "data": {"itemId": item["id"]}, "reason": "FREE_HEAL_PICKUP"}

    # ── 2. DEATHZONE ESCAPE ──
    if region.get("isDeathZone") or region_id in danger_ids:
        for conn in connections:
            rid = conn.get("id") if isinstance(conn, dict) else conn
            if rid and rid not in danger_ids:
                log.warning(f"💀 DEATHZONE ESCAPE to {rid}")
                return {"action": "move", "data": {"regionId": rid}, "reason": "DEATHZONE"}

    # ── 3. EMERGENCY HEAL / TACTICAL RETREAT ──
    if hp < TACTIC_CONFIG["HP_EMERGENCY"]:
        # Jika ada musuh di region dan kita kemungkinan kalah, kabur dulu
        if enemies_here:
            enemy = max(enemies_here, key=lambda e: get_weapon_damage(e.get("equippedWeapon")) + e.get("atk", 10))
            enemy_dmg = calc_damage(enemy.get("atk", 10), get_weapon_bonus(enemy.get("equippedWeapon")), defense, region_weather)
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped), enemy.get("def", 5), region_weather)
            ttk, ttd = _simulate_duel(hp, my_dmg, enemy.get("hp", 100), enemy_dmg)
            if ttk > ttd:   # kita kalah duluan
                # Cari region aman untuk kabur
                for conn in connections:
                    rid = conn.get("id") if isinstance(conn, dict) else conn
                    if rid and rid not in danger_ids:
                        log.warning(f"🏃 TACTICAL RETREAT to {rid} (HP:{hp})")
                        return {"action": "move", "data": {"regionId": rid}, "reason": "RETREAT"}
        # Kalo aman, langsung heal
        heal = _find_healing_item(inventory)
        if heal:
            log.warning(f"🏥 EMERGENCY HEAL: {heal.get('typeId')} (HP:{hp})")
            _post_heal_safe_turns = 1
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "HEAL"}

    # ── 4. PRE-HEAL BEFORE FIGHT ──
    if hp < TACTIC_CONFIG["HP_PRE_HEAL"] and not enemies_here:
        heal = _find_healing_item(inventory)
        if heal:
            log.info(f"❤️ PRE-HEAL: {heal.get('typeId')} (HP:{hp})")
            _post_heal_safe_turns = 1
            return {"action": "use_item", "data": {"itemId": heal["id"]}, "reason": "PRE_HEAL"}

    # ── 5. SMART TARGET SELECTION ──
    if enemies_here:
        # Buat daftar target dengan skor
        target_list = []
        for e in enemies_here:
            e_dmg = calc_damage(e.get("atk", 10), get_weapon_bonus(e.get("equippedWeapon")), defense, region_weather)
            my_dmg_to_e = calc_damage(atk, get_weapon_bonus(equipped), e.get("def", 5), region_weather)
            e_hp = e.get("hp", 100)
            ttk, ttd = _simulate_duel(hp, my_dmg_to_e, e_hp, e_dmg)

            # Skor: prioritas eksekusi instant (ttk=1) > threat tinggi > revenge > low hp
            score = 0
            if ttk == 1: score += 1000
            if e.get("id") == _revenge_target: score += 500
            if e.get("id") == _last_attacked_by: score += 300
            score += (e_dmg * 10)   # ancaman tinggi
            score -= e_hp           # semakin rendah HP semakin baik

            memory = get_or_create_memory(e.get("id"))
            if memory.is_blacklisted:
                score += 2000  # blacklist prioritas utama

            target_list.append((score, ttk, ttd, e))

        target_list.sort(reverse=True, key=lambda x: x[0])
        best_score, ttk, ttd, target = target_list[0]

        # Keputusan serangan: jika kita menang cepat atau sama-sama tipis dan kita unggul
        my_dmg_to_target = calc_damage(atk, get_weapon_bonus(equipped), target.get("def", 5), region_weather)
        if ttk <= ttd + TACTIC_CONFIG["ADVANTAGE_TURN_DIFF"] or ttk <= 1:
            if _is_in_range(target, region_id, get_weapon_range(equipped), connections):
                # Update hunting
                if not _hunting_target:
                    _hunting_target = target
                    _hunting_timer = 5
                log.warning(f"⚔️ ATTACK {target['id'][:8]} (HP:{target.get('hp')}, TTD:{ttd}, TTK:{ttk})")
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"}, "reason": "SMART_ATTACK"}

        # Jika tidak bisa menang, tactical retreat
        for conn in connections:
            rid = conn.get("id") if isinstance(conn, dict) else conn
            if rid and rid not in danger_ids:
                log.warning(f"🏳️ OUTMATCHED, retreat to {rid}")
                return {"action": "move", "data": {"regionId": rid}, "reason": "RETREAT_FROM_DUEL"}

    # ── 6. MOVE TO HUNTING TARGET ──
    if _hunting_target and _hunting_timer > 0 and ep >= move_ep_cost:
        target_region = _hunting_target.get("regionId")
        if target_region and target_region not in danger_ids and target_region != region_id:
            # Cek koneksi langsung
            for conn in connections:
                rid = conn.get("id") if isinstance(conn, dict) else conn
                if rid == target_region:
                    log.warning(f"🎯 MOVE TO HUNT: {target_region}")
                    return {"action": "move", "data": {"regionId": target_region}, "reason": "HUNT_MOVE"}
            # Jika tidak terhubung, cari jalur terdekat? Sederhana: ke region dengan koneksi terbanyak
            best_conn = None
            best_conn_score = -1
            for conn in connections:
                rid = conn.get("id") if isinstance(conn, dict) else conn
                if rid and rid not in danger_ids:
                    # Ambil jumlah koneksi region tersebut jika kita tahu
                    resolved_conn = _resolve_region(rid, view)
                    score = len(resolved_conn.get("connections", [])) if resolved_conn else 0
                    if score > best_conn_score:
                        best_conn_score = score
                        best_conn = rid
            if best_conn:
                log.warning(f"🚶 MOVE TOWARDS HUNT: {best_conn}")
                return {"action": "move", "data": {"regionId": best_conn}, "reason": "HUNT_PATH"}

    # ── 7. MAP CONTROL: Bergerak ke region sentral ──
    if ep >= move_ep_cost and not enemies_here:
        # Pilih region terhubung dengan koneksi terbanyak yang bukan deathzone
        candidate = None
        max_conn = -1
        for conn in connections:
            rid = conn.get("id") if isinstance(conn, dict) else conn
            if rid and rid not in danger_ids:
                resolved = _resolve_region(rid, view)
                if resolved:
                    num_conn = len(resolved.get("connections", []))
                    if num_conn > max_conn:
                        max_conn = num_conn
                        candidate = rid
        if candidate and candidate != region_id:
            log.info(f"🗺️ MAP CONTROL move to {candidate}")
            return {"action": "move", "data": {"regionId": candidate}, "reason": "MAP_CONTROL"}

    # ── 8. FARM MONSTER ──
    if monsters and ep >= 1 and not enemies_here:
        target = min(monsters, key=lambda m: m.get("hp", 999))
        if _is_in_range(target, region_id, get_weapon_range(equipped), connections):
            log.info(f"🐗 FARM monster HP={target.get('hp')}")
            return {"action": "attack", "data": {"targetId": target["id"], "targetType": "monster"}, "reason": "FARM"}

    # ── 9. REST ──
    if ep < move_ep_cost and not enemies_here:
        log.info(f"😴 REST (EP:{ep}/{max_ep})")
        return {"action": "rest", "data": {}, "reason": "REST"}

    # ── 10. NOTHING ──
    log.warning(f"⚠️ IDLE - HP:{hp} EP:{ep} Enemies:{len(enemies_here)}")
    return None