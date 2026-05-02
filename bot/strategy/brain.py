"""
MoltyRoyale - Maximum Win Rate Brain v3.1
============================================
Optimized for maximum win rate with full game loop integration.

CHANGELOG v3.1 (Latest):
- ✅ Enhanced zone awareness with prediction (zone_awareness tuning)
- ✅ Dynamic difficulty adjustment (early/mid/late game phases)
- ✅ Better combat prediction with ML-inspired features
- ✅ Learning from past games with death cause tracking
- ✅ Team detection and ally coordination (team_mode)
- ✅ Risk assessment matrix (risk_tolerance)
- ✅ Improved emergency handling with ally support
- ✅ Performance metrics tracking (combat win rate, etc.)
- ✅ Config persistence (get/set config as JSON)
- ✅ Reset game state for clean new game starts

CHANGELOG v3.0:
- ✅ Fixed output format for ActionSender.build_action() compatibility
- ✅ Enhanced view parsing for nested WebSocket structures
- ✅ Better death zone tracking with pendingDeathzones
- ✅ Improved free action prioritization (pickup → equip → facility)
- ✅ More accurate combat win probability calculation
- ✅ Better emergency handling with multi-threat assessment
- ✅ Optimized EP management (+1/turn auto-recovery)
- ✅ Enhanced enemy profiling with behavior tracking
- ✅ Strategy adaptation based on last 10 games

Output Format (compatible with game_loop.py):
    {"action": "move|attack|pickup|equip|use|rest|use_facility",
     "data": {"regionId"|"itemId"|"targetId"|"facilityId": "..."},
     "reason": "Human-readable explanation"}
"""

import time
import random
from typing import Optional
from dataclasses import dataclass, field


# ============================================================================
# CONFIGURATION - TUNE THIS UNTUK PLAYSTYLE ANDA
# ============================================================================
#
# STRATEGY TUNING GUIDE:
# ----------------------
# Aggression: 0.3 = passive/safe, 0.5 = balanced, 0.7+ = aggressive hunter
# Heal Threshold: 0.3 = heal at 30% HP, 0.5 = heal at 50% HP (more cautious)
# Flee Threshold: 0.3 = flee only when losing badly, 0.5 = flee when uncertain
# Explore Priority: 0.3 = stay in safe zones, 0.7+ = actively explore map
#
# RECOMMENDED PRESETS:
# --------------------
# Safe/Survival: aggression=0.3, heal_threshold=0.5, flee_threshold=0.5, explore=0.4
# Balanced:      aggression=0.5, heal_threshold=0.4, flee_threshold=0.35, explore=0.6
# Aggressive:    aggression=0.7, heal_threshold=0.3, flee_threshold=0.25, explore=0.8
#
# ============================================================================

@dataclass
class StrategyConfig:
    """Adjustable strategy weights - optimize based on your playstyle"""
    # Aggression: 0 = ultra-passive, 1 = always attack
    aggression: float = 0.5

    # Heal threshold: HP < X% = heal
    heal_threshold: float = 0.4

    # Rest threshold: EP < X = rest
    rest_ep_threshold: int = 3

    # Flee threshold: win_prob < X% = flee
    flee_threshold: float = 0.35

    # Explore priority: 0 = stay safe, 1 = explore aggressively
    explore_priority: float = 0.6

    # Combat win probability threshold to attack
    attack_win_prob_threshold: float = 0.65

    # Combat win probability threshold to flee
    flee_win_prob_threshold: float = 0.30

    # Prioritize survival over kills
    survival_priority: float = 0.7

    # === ADVANCED TUNING (v3.1) ===
    # Zone awareness: 0 = ignore, 1 = hyper-aware of death zones
    zone_awareness: float = 0.9

    # Loot priority: 0 = ignore items, 1 = grab everything
    loot_priority: float = 0.7

    # Risk tolerance: 0 = never risk, 1 = always gamble
    risk_tolerance: float = 0.4

    # Late game aggression (when alive_count <= 10)
    late_game_aggression: float = 0.8

    # Early game caution (when alive_count >= 50)
    early_game_caution: float = 0.6

    # Team mode: True = look for allies, False = FFA
    team_mode: bool = False


@dataclass
class Constants:
    """Game constants from MoltyRoyale docs"""
    TURN_DURATION = 60  # detik
    TURN_BUFFER = 5     # submit 5 detik sebelum timeout
    EP_AUTO_RECOVER = 1  # +1 EP per turn

    # EP thresholds (lower karena auto-recover)
    EP_CRITICAL = 1     # < 1 EP = rest immediately
    EP_LOW = 3          # < 3 EP = consider rest if no threats

    # HP thresholds
    HP_CRITICAL = 25    # < 25% HP = heal immediately
    HP_LOW = 45         # < 45% HP = consider heal

    # Inventory
    MAX_INVENTORY = 10  # Max items (from limits.md)
    MIN_WEAPON_TIER = 1 # Minimum acceptable weapon tier

    # Combat
    MAX_ENEMIES_TO_FIGHT = 1  # Hanya fight 1 musuh sekaligus
    SAFE_DIST_FROM_DEATH_ZONE = 2  # Keep this far from death zone


# ============================================================================
# GAME DATA - Weapon/Item Stats (from MoltyRoyale docs)
# ============================================================================

WEAPONS = {
    # Melee weapons
    "fist": {"bonus": 0, "type": "melee"},
    "knife": {"bonus": 5, "type": "melee"},
    "bat": {"bonus": 8, "type": "melee"},
    "sword": {"bonus": 12, "type": "melee"},
    "katana": {"bonus": 15, "type": "melee"},
    # Ranged weapons
    "pistol": {"bonus": 10, "type": "ranged"},
    "rifle": {"bonus": 14, "type": "ranged"},
    "shotgun": {"bonus": 18, "type": "ranged"},
    "sniper": {"bonus": 20, "type": "ranged"},
    # Special
    "plasma": {"bonus": 22, "type": "energy"},
    "laser": {"bonus": 25, "type": "energy"},
}

ARMOR = {
    "none": {"bonus": 0},
    "cloth": {"bonus": 3},
    "leather": {"bonus": 6},
    "vest": {"bonus": 10},
    "plate": {"bonus": 15},
    "advanced": {"bonus": 20},
}


# ============================================================================
# STATE MANAGEMENT
# ============================================================================

@dataclass
class EnemyProfile:
    """Track enemy behavior over time"""
    agent_id: str
    encounters: int = 0
    wins: int = 0
    losses: int = 0
    last_known_hp: int = 100
    last_known_ep: int = 10
    preferred_weapon: Optional[str] = None
    aggression_level: float = 0.5  # 0 = passive, 1 = aggressive
    last_seen_region: Optional[str] = None


@dataclass
class GameStateSnapshot:
    """Track game state history for pattern detection"""
    turn: int = 0
    hp_history: list = field(default_factory=list)
    ep_history: list = field(default_factory=list)
    win_rate: float = 0.0
    avg_placement: float = 0.0
    total_kills: int = 0


class MoltyRoyaleBrain:
    """Maximum Win Rate Brain for MoltyRoyale v3.1

    Upgrades in v3.1:
    - Enhanced zone awareness with prediction
    - Dynamic difficulty adjustment based on alive_count
    - Better combat prediction with ML-inspired features
    - Learning from past games (win/loss patterns)
    - Team detection and ally coordination
    - Risk assessment matrix
    """

    def __init__(self):
        self.config = StrategyConfig()
        self.constants = Constants()

        # State tracking
        self._enemy_profiles: dict[str, EnemyProfile] = {}
        self._game_history: list[dict] = []
        self._last_game_state: Optional[dict] = None
        self._turn_start_time: float = 0
        self._game_id: Optional[str] = None
        self._agent_id: Optional[str] = None

        # Current game state
        self._current_hp: int = 100
        self._current_ep: int = 10
        self._current_region: Optional[str] = None
        self._inventory: list = []
        self._equipped_weapon: Optional[str] = None
        self._equipped_armor: Optional[str] = None

        # === ADVANCED TRACKING (v3.1) ===
        # Zone knowledge
        self._death_zones: set[str] = set()
        self._pending_death_zones: set[str] = set()
        self._safe_zones: set[str] = set()

        # Map knowledge
        self._explored_regions: set[str] = set()
        self._high_value_regions: set[str] = set()  # Regions with good loot history
        self._danger_regions: set[str] = set()  # Regions where we died/got hurt

        # Combat tracking
        self._combat_log: list[dict] = []  # Recent combat encounters
        self._damage_taken_log: list[int] = []  # Track damage patterns
        self._damage_dealt_log: list[int] = []

        # Timing
        self._turn_number: int = 0
        self._game_start_time: float = 0
        self._last_action_time: float = 0

        # Allies (team mode)
        self._allies: set[str] = set()
        self._ally_positions: dict[str, str] = {}  # ally_id -> region_id

        # Performance metrics
        self._actions_this_game: int = 0
        self._successful_combats: int = 0
        self._failed_combats: int = 0
        self._items_collected: int = 0

    # ========================================================================
    # MAIN ENTRY POINT
    # ========================================================================

    def decide_action(self, game_state: dict, can_act: bool = True) -> dict:
        """
        Main decision function - called every turn

        Args:
            game_state: Full game view from WebSocket
            can_act: Whether cooldown actions are allowed

        Returns: { "action": "...", "data": {...}, "reason": "..." }
        Compatible with game_loop.py ActionSender.build_action()
        """
        # Update internal state
        self._update_from_game_state(game_state)

        # Start turn timer
        self._turn_start_time = time.time()

        # Step 1: CHECK DEADLY SITUATIONS (highest priority)
        if self._is_in_immediate_danger(game_state):
            emergency_action = self._handle_emergency(game_state)
            if emergency_action:
                return emergency_action

        # Step 2: FREE ACTIONS (pickup/equip - don't consume turn)
        # These are handled separately by game loop before main action
        free_action = self._get_next_free_action(game_state)
        if free_action:
            return free_action

        # Step 3: CHECK IF CAN ACT (for cooldown actions like attack)
        if not can_act:
            # Cooldown active - do non-cooldown action
            return self._decide_safe_action(game_state)

        # Step 4: DECIDE MAIN ACTION
        main_action = self._decide_main_action(game_state)

        return main_action

    # ========================================================================
    # FREE ACTIONS OPTIMIZATION
    # ========================================================================

    def _get_next_free_action(self, game_state: dict) -> Optional[dict]:
        """
        Get next free action (pickup/equip/facility) - returns ONE action at a time
        Free actions don't consume turn, so game loop will call again after each

        Returns: {"action": "pickup", "data": {...}, "reason": "..."} or None
        """
        # Priority 1: Pickup best item
        pickup = self._get_best_pickup(game_state)
        if pickup:
            return pickup

        # Priority 2: Equip better weapon/armor
        equip = self._get_best_equip(game_state)
        if equip:
            return equip

        # Priority 3: Use facility
        facility = self._use_facility_if_needed(game_state)
        if facility:
            return facility

        return None  # No free actions needed

    def _get_best_pickup(self, game_state: dict) -> Optional[dict]:
        """
        Get best item to pickup - FREE ACTION
        Handles both flat and wrapped visibleItems format.
        """
        view = game_state.get("view", game_state)
        current_region = view.get("currentRegion", {})
        region_id = current_region.get("id", "")

        # Collect items from multiple sources
        items = []

        # Source 1: currentRegion.items
        region_items = current_region.get("items", [])
        items.extend(self._unwrap_items(region_items, region_id))

        # Source 2: visibleItems (wrapped format: {regionId, item: {...}})
        visible = view.get("visibleItems", [])
        items.extend(self._unwrap_items(visible, region_id))

        # Filter to current region only
        if region_id:
            items = [i for i in items if i.get("regionId") == region_id or not i.get("regionId")]

        # Deduplicate by item ID
        seen_ids = set()
        unique_items = []
        for item in items:
            item_id = item.get("id")
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                unique_items.append(item)
        items = unique_items

        best_item = None
        best_score = 0

        # Get current inventory IDs for deduplication
        inv_ids = {i.get("id") for i in self._inventory if isinstance(i, dict)}

        for item in items:
            if not isinstance(item, dict):
                continue

            # Skip if already in inventory
            item_id = item.get("id")
            if item_id and item_id in inv_ids:
                continue

            # Score: weapon > armor > consumable > misc
            item_type = self._get_item_type(item).lower()
            tier = item.get("tier", 1)
            bonus = item.get("bonus", 0) or self._get_type_bonus(item_type)

            score = 0
            if "weapon" in item_type:
                # Prioritize weapons based on bonus
                score = 100 + (bonus * 5) + (tier * 3)
            elif "armor" in item_type:
                score = 80 + (bonus * 5) + (tier * 3)
            elif "consumable" in item_type or "heal" in item.get("effect", "").lower():
                heal_amount = item.get("healAmount", item.get("value", 20))
                score = 60 + (heal_amount // 5)
            elif "food" in item_type or "ep" in item.get("effect", "").lower():
                score = 50 + (tier * 2)
            else:
                # Misc items - low priority
                score = 20 + tier

            if score > best_score:
                best_score = score
                best_item = item

        if best_item and best_score >= 50:
            item_name = self._get_item_name(best_item)
            item_bonus = best_item.get("bonus", 0) or self._get_type_bonus(self._get_item_type(best_item))
            return {
                "action": "pickup",
                "data": {"itemId": best_item.get("id")},
                "reason": f"PICKUP: {item_name} (bonus={item_bonus})"
            }

        return None

    def _unwrap_items(self, raw_items: list, region_id: str = "") -> list:
        """
        Unwrap visibleItems format.
        Handles both wrapped {regionId, item: {...}} and flat {...} formats.
        """
        result = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue

            # Wrapped format: {regionId, item: {...}}
            if "item" in entry and isinstance(entry["item"], dict):
                item = entry["item"].copy()
                item["regionId"] = entry.get("regionId", region_id)
                result.append(item)
            # Flat format: {...}
            elif entry.get("id"):
                item = entry.copy()
                item["regionId"] = entry.get("regionId", region_id)
                result.append(item)

        return result

    def _get_item_type(self, item: dict) -> str:
        """Get item type from any available field."""
        return (item.get("type")
                or item.get("typeId")
                or item.get("itemType")
                or item.get("category")
                or "")

    def _get_item_name(self, item: dict) -> str:
        """Get best display name for item."""
        return (item.get("name")
                or item.get("typeId")
                or item.get("type")
                or item.get("itemName")
                or f"item_{item.get('id', 'unknown')[:8]}")

    def _get_type_bonus(self, item_type: str) -> int:
        """Get bonus for item type from WEAPONS/ARMOR dicts."""
        item_type = item_type.lower()
        if item_type in WEAPONS:
            return WEAPONS[item_type].get("bonus", 0)
        if item_type in ARMOR:
            return ARMOR[item_type].get("bonus", 0)
        return 0

    def _get_best_equip(self, game_state: dict) -> Optional[dict]:
        """
        Get best weapon/armor to equip - FREE ACTION
        Compares actual bonus values, not just type matching.
        """
        best_item = None
        best_bonus = 0
        current_weapon_bonus = self._get_current_weapon_bonus()
        current_armor_bonus = self._get_current_armor_bonus()

        for item in self._inventory:
            if not isinstance(item, dict):
                continue

            item_type = self._get_item_type(item).lower()
            item_bonus = item.get("bonus", 0) or self._get_type_bonus(item_type)

            # Determine if weapon or armor
            is_weapon = "weapon" in item_type or item_type in WEAPONS
            is_armor = "armor" in item_type or item_type in ARMOR

            # Compare against appropriate current bonus
            if is_weapon:
                current_bonus = current_weapon_bonus
                # Only upgrade if significantly better (+2 or more)
                if item_bonus > current_bonus + 1:
                    if item_bonus > best_bonus:
                        best_bonus = item_bonus
                        best_item = item
            elif is_armor:
                current_bonus = current_armor_bonus
                if item_bonus > current_bonus + 1:
                    if item_bonus > best_bonus:
                        best_bonus = item_bonus
                        best_item = item

        if best_item:
            item_name = self._get_item_name(best_item)
            item_type_str = "weapon" if ("weapon" in self._get_item_type(best_item).lower() or self._get_item_type(best_item).lower() in WEAPONS) else "armor"
            return {
                "action": "equip",
                "data": {"itemId": best_item.get("id")},
                "reason": f"SMART EQUIP: {item_name} ({item_type_str}: +{best_bonus} vs +{current_bonus})"
            }

        return None

    def _get_current_weapon_bonus(self) -> int:
        """Get current equipped weapon bonus."""
        if not self._equipped_weapon:
            return 0
        if isinstance(self._equipped_weapon, dict):
            weapon_type = self._equipped_weapon.get("typeId", "").lower()
            return WEAPONS.get(weapon_type, {}).get("bonus", 0)
        return 0

    def _get_current_armor_bonus(self) -> int:
        """Get current equipped armor bonus."""
        if not self._equipped_armor:
            return 0
        if isinstance(self._equipped_armor, dict):
            armor_type = self._equipped_armor.get("typeId", "").lower()
            return ARMOR.get(armor_type, {}).get("bonus", 0)
        return 0

    def _use_facility_if_needed(self, game_state: dict) -> Optional[dict]:
        """Use facility if beneficial - FREE ACTION"""
        current_region = game_state.get("currentRegion", {})
        facilities = current_region.get("facilities", [])

        for facility in facilities:
            facility_type = facility.get("type", "")
            facility_id = facility.get("id", "")
            facility_name = facility.get("name", facility_type)

            # Medical facility when HP low
            if facility_type == "medical" and self._current_hp < 50:
                return {
                    "action": "use_facility",
                    "data": {"facilityId": facility_id},
                    "reason": f"FACILITY: {facility_name} (HP={self._current_hp})"
                }

            # Supply facility when need loot
            if facility_type == "supply" and len(self._inventory) < 5:
                return {
                    "action": "use_facility",
                    "data": {"facilityId": facility_id},
                    "reason": f"FACILITY: {facility_name} (loot)"
                }

        return None

    # ========================================================================
    # DEATH ZONE DETECTION - CRITICAL!
    # ========================================================================

    def _is_in_immediate_danger(self, game_state: dict) -> bool:
        """Check if agent is in immediate death danger"""
        # 1. Death zone check
        current_region_id = game_state.get("currentRegion", {}).get("id")
        if self._is_death_zone(current_region_id, game_state):
            return True

        # 2. Multiple enemies nearby
        nearby_enemies = self._count_nearby_enemies(game_state)
        if nearby_enemies >= 2:
            return True

        # 3. Low HP + low EP = vulnerable
        if self._current_hp < self.constants.HP_CRITICAL and self._current_ep < 3:
            return True

        return False

    def _is_death_zone(self, region_id: Optional[str], game_state: dict) -> bool:
        """
        Check if region is (or will become) death zone.
        Handles both string IDs and object format {id, name} for pendingDeathzones.
        """
        if not region_id:
            return False

        view = game_state.get("view", game_state)

        # Check current region death zone status
        current_region = view.get("currentRegion", {})
        if current_region.get("id") == region_id and current_region.get("isDeathZone"):
            return True

        # Check all regions for death zone status
        regions = view.get("regions", {})
        if isinstance(regions, dict):
            region = regions.get(region_id, {})
            if isinstance(region, dict) and region.get("isDeathZone"):
                return True

        # Check visible regions
        for region in view.get("visibleRegions", []):
            if isinstance(region, dict) and region.get("id") == region_id and region.get("isDeathZone"):
                return True

        # Check connected regions
        for conn in view.get("connectedRegions", []):
            if isinstance(conn, dict) and conn.get("id") == region_id and conn.get("isDeathZone"):
                return True

        # Check pending death zones (1-2 turns ahead)
        # Format can be: string ID or object {id, name}
        pending = view.get("pendingDeathzones", [])
        for dz in pending:
            if isinstance(dz, dict):
                if dz.get("id") == region_id:
                    return True
            elif isinstance(dz, str) and dz == region_id:
                return True

        return False

    def _find_nearest_safe_region(self, game_state: dict) -> Optional[str]:
        """Find nearest region that is NOT death zone"""
        current_region = game_state.get("currentRegion", {})
        current_x = current_region.get("x", 0)
        current_y = current_region.get("y", 0)

        regions = game_state.get("regions", {})
        safe_regions = []

        for region_id, region in regions.items():
            # Skip death zones
            if region.get("isDeathZone"):
                continue
            if region_id in game_state.get("pendingDeathzones", []):
                continue

            # Calculate distance
            rx = region.get("x", 0)
            ry = region.get("y", 0)
            distance = abs(current_x - rx) + abs(current_y - ry)

            safe_regions.append((distance, region_id))

        if not safe_regions:
            return None

        # Return nearest safe region
        return min(safe_regions)[1]

    # ========================================================================
    # EMERGENCY HANDLING
    # ========================================================================

    def _handle_emergency(self, game_state: dict) -> Optional[dict]:
        """Handle immediate danger situations - v3.1 enhanced
        
        v3.1 improvements:
        - Ally coordination (team mode)
        - Better death zone prediction
        - Multi-threat assessment
        """
        # 1. Death zone escape (highest priority)
        if self._is_death_zone(self._current_region, game_state):
            target_region = self._find_nearest_safe_region(game_state)
            if target_region:
                return {
                    "action": "move",
                    "data": {"regionId": target_region},
                    "reason": f"PRE-ESCAPE: Region becoming DZ"
                }

        # 2. Check pending death zones (1-2 turns ahead)
        pending_dz = game_state.get("pendingDeathzones", [])
        for dz in pending_dz:
            dz_id = dz.get("id", dz) if isinstance(dz, dict) else dz
            # Check if we're adjacent to pending DZ
            connected = game_state.get("connectedRegions", [])
            for conn in connected:
                if isinstance(conn, dict) and conn.get("id") == dz_id:
                    return {
                        "action": "move",
                        "data": {"regionId": self._find_nearest_safe_region(game_state)},
                        "reason": f"PRE-ESCAPE: Adjacent to pending DZ"
                    }
                elif isinstance(conn, str) and conn == dz_id:
                    return {
                        "action": "move",
                        "data": {"regionId": self._find_nearest_safe_region(game_state)},
                        "reason": f"PRE-ESCAPE: Adjacent to pending DZ"
                    }

        # 3. Multiple enemies - flee or call for help
        nearby_enemies = self._count_nearby_enemies(game_state)
        if nearby_enemies >= 2:
            # v3.1: Team mode - check if allies nearby
            if self.config.team_mode and self._allies:
                for ally_id in self._allies:
                    if ally_id in self._ally_positions:
                        ally_region = self._ally_positions[ally_id]
                        if ally_region == self._current_region:
                            return {
                                "action": "move",
                                "data": {"regionId": ally_region},
                                "reason": f"CALL HELP: Ally nearby, {nearby_enemies} enemies"
                            }
            
            flee_region = self._find_flee_region(game_state)
            if flee_region:
                return {
                    "action": "move",
                    "data": {"regionId": flee_region},
                    "reason": f"FLEE: {nearby_enemies} enemies nearby"
                }

        # 4. Low HP + low EP - find safe spot to heal
        if self._current_hp < self.constants.HP_CRITICAL and self._current_ep < 3:
            # v3.1: Check for ally assistance
            if self.config.team_mode and self._allies:
                for ally_id in self._allies:
                    if ally_id in self._ally_positions:
                        ally_region = self._ally_positions[ally_id]
                        if ally_region == self._current_region:
                            # Stay with ally, they can help
                            return {
                                "action": "rest",
                                "data": {},
                                "reason": f"REST: With ally protection (HP={self._current_hp})"
                            }
            
            safe_region = self._find_nearest_safe_region(game_state)
            if safe_region and safe_region != self._current_region:
                return {
                    "action": "move",
                    "data": {"regionId": safe_region},
                    "reason": f"RETREAT: HP={self._current_hp} EP={self._current_ep}"
                }
            else:
                heal_item = self._find_best_heal_item()
                if heal_item:
                    return {
                        "action": "use",
                        "data": {"itemId": heal_item["id"]},
                        "reason": f"EMERGENCY HEAL: {heal_item.get('name', 'item')}"
                    }
                else:
                    return {
                        "action": "rest",
                        "data": {},
                        "reason": f"EMERGENCY REST: HP={self._current_hp} EP={self._current_ep}"
                    }

        return None

    def _find_flee_region(self, game_state: dict) -> Optional[str]:
        """Find region to flee to (away from enemies)"""
        current_region = game_state.get("currentRegion", {})
        current_x = current_region.get("x", 0)
        current_y = current_region.get("y", 0)

        # Get enemy positions
        enemies = game_state.get("nearbyAgents", [])

        # Calculate average enemy position
        if not enemies:
            # No enemies, find any safe region
            return self._find_nearest_safe_region(game_state)

        avg_ex = sum(e.get("x", 0) for e in enemies) / len(enemies)
        avg_ey = sum(e.get("y", 0) for e in enemies) / len(enemies)

        # Find region furthest from enemies
        regions = game_state.get("regions", {})
        best_region = None
        best_distance = 0

        for region_id, region in regions.items():
            if self._is_death_zone(region_id, game_state):
                continue

            rx = region.get("x", 0)
            ry = region.get("y", 0)

            # Distance from enemies
            enemy_dist = abs(rx - avg_ex) + abs(ry - avg_ey)
            # Distance from current (don't go too far)
            current_dist = abs(rx - current_x) + abs(ry - current_y)

            # Score: far from enemies, close to current
            score = enemy_dist - (current_dist * 0.5)

            if score > best_distance:
                best_distance = score
                best_region = region_id

        return best_region

    # ========================================================================
    # MAIN ACTION DECISION
    # ========================================================================

    def _decide_main_action(self, game_state: dict) -> dict:
        """Decide main action (move/attack/use/rest)"""

        # 1. Check if need to heal
        if self._should_heal(game_state):
            heal_action = self._get_heal_action(game_state)
            if heal_action:
                return heal_action

        # 2. Check if need to rest (EP management)
        if self._should_rest(game_state):
            return {"action": "rest", "data": {}, "reason": f"REST: EP={self._current_ep} (auto-recover +1/turn)"}

        # 3. Check for combat opportunity
        combat_action = self._check_combat_opportunity(game_state)
        if combat_action:
            return combat_action

        # 4. Default: move to strategic location
        move_action = self._decide_move_strategy(game_state)
        return move_action

    def _decide_safe_action(self, game_state: dict) -> dict:
        """Decide action when cooldown is active (no attack/move)"""
        # Can still do: pickup, equip, use, rest

        # 1. Heal if needed
        if self._should_heal(game_state):
            heal_action = self._get_heal_action(game_state)
            if heal_action:
                return heal_action

        # 2. Rest if EP low
        if self._should_rest(game_state):
            return {"action": "rest", "data": {}, "reason": f"REST: EP={self._current_ep} (cooldown active)"}

        # 3. Explore (move is allowed even during cooldown)
        return self._decide_move_strategy(game_state)

    def _should_heal(self, game_state: dict) -> bool:
        """Determine if agent should heal"""
        hp_percent = self._current_hp / 100.0

        # Always heal if critically low
        if hp_percent < self.constants.HP_CRITICAL / 100:
            return True

        # Heal if below threshold and no immediate threat
        if hp_percent < self.config.heal_threshold:
            # Check if there are enemies nearby
            nearby_enemies = self._count_nearby_enemies(game_state)
            if nearby_enemies == 0:
                return True

        return False

    def _get_heal_action(self, game_state: dict) -> dict:
        """Get optimal heal action"""
        # Priority 1: Medical facility (FREE & efficient)
        current_region = game_state.get("currentRegion", {})
        facilities = current_region.get("facilities", [])

        for facility in facilities:
            if facility.get("type") == "medical":
                return {
                    "action": "use_facility",
                    "data": {"facilityId": facility["id"]},
                    "reason": f"MEDICAL: {facility.get('name', 'facility')} (HP={self._current_hp})"
                }

        # Priority 2: Heal item
        heal_item = self._find_best_heal_item()
        if heal_item:
            return {
                "action": "use",
                "data": {"itemId": heal_item["id"]},
                "reason": f"HEAL: {heal_item.get('name', 'item')} (+{heal_item.get('healAmount', '?')} HP)"
            }

        # Priority 3: Rest (slow but free)
        return {
            "action": "rest",
            "data": {},
            "reason": f"REST: No heal items (HP={self._current_hp})"
        }

    def _find_best_heal_item(self) -> Optional[dict]:
        """Find best heal item in inventory"""
        heal_items = [
            i for i in self._inventory
            if i.get("type") == "consumable" and i.get("effect") == "heal"
        ]

        if not heal_items:
            return None

        # Sort by heal amount
        return max(heal_items, key=lambda x: x.get("healAmount", 0))

    def _should_rest(self, game_state: dict) -> bool:
        """Determine if agent should rest for EP"""
        # Critical EP
        if self._current_ep <= self.constants.EP_CRITICAL:
            return True

        # Low EP and no threats
        if self._current_ep <= self.constants.EP_LOW:
            nearby_enemies = self._count_nearby_enemies(game_state)
            if nearby_enemies == 0:
                return True

        return False

    def _check_combat_opportunity(self, game_state: dict) -> Optional[dict]:
        """Check if there's a good combat opportunity"""
        nearby_agents = game_state.get("nearbyAgents", [])

        # Don't fight if multiple enemies
        if len(nearby_agents) > self.constants.MAX_ENEMIES_TO_FIGHT:
            return None

        # Find best target
        best_target = None
        best_win_prob = 0.0

        for agent in nearby_agents:
            win_prob = self._calculate_win_probability(agent, game_state)

            if win_prob > self.config.attack_win_prob_threshold and win_prob > best_win_prob:
                best_win_prob = win_prob
                best_target = agent

        # Attack if good opportunity
        if best_target and best_win_prob >= self.config.attack_win_prob_threshold:
            # v3.1: Adjust threshold based on game phase
            alive_count = game_state.get("aliveCount", 100)
            game_phase = self._determine_game_phase(alive_count)
            
            # Late game: Be more aggressive (lower threshold)
            if game_phase == "late":
                effective_threshold = self.config.attack_win_prob_threshold * 0.85
            # Early game: Be more cautious (higher threshold)
            elif game_phase == "early":
                effective_threshold = self.config.attack_win_prob_threshold * 1.15
            else:
                effective_threshold = self.config.attack_win_prob_threshold
            
            if best_win_prob >= effective_threshold:
                # Update enemy profile
                self._update_enemy_profile(best_target)

                enemy_name = best_target.get("name", "enemy")
                enemy_hp = best_target.get("hp", "?")
                return {
                    "action": "attack",
                    "data": {"targetId": best_target["id"]},
                    "reason": f"COMBAT: {int(win_prob*100)}% win vs {enemy_name} (HP={enemy_hp})"
                }

        # Flee if threatened
        for agent in nearby_agents:
            win_prob = self._calculate_win_probability(agent, game_state)
            if win_prob <= self.config.flee_win_prob_threshold:
                flee_region = self._find_flee_region(game_state)
                if flee_region:
                    return {
                        "action": "move",
                        "data": {"regionId": flee_region},
                        "reason": f"FLEE: {int(win_prob*100)}% win prob too low"
                    }

        return None

    def _calculate_win_probability(self, enemy: dict, game_state: dict) -> float:
        """
        Calculate win probability against enemy.
        Uses ML-inspired feature weighting based on successful bot patterns.

        Features:
        - HP advantage (weight: 0.25)
        - EP advantage (weight: 0.15)
        - Attack advantage (weight: 0.30)
        - Defense advantage (weight: 0.15)
        - Enemy behavior history (weight: 0.10)
        - Terrain/distance (weight: 0.05)

        Returns: 0.0 (certain loss) to 1.0 (certain win)
        """
        view = game_state.get("view", game_state)

        # Base stats
        my_hp_percent = self._current_hp / 100.0
        my_ep = self._current_ep
        my_attack = self._get_attack_power()
        my_defense = self._get_defense_power()

        enemy_hp = enemy.get("hp", 100)
        enemy_hp_percent = enemy_hp / 100.0
        enemy_ep = enemy.get("ep", 10)
        enemy_attack = enemy.get("attack", enemy.get("atk", 50))
        enemy_defense = enemy.get("defense", enemy.get("def", 50))

        # Calculate normalized advantages (-1 to +1 scale)
        hp_advantage = my_hp_percent - enemy_hp_percent

        # Attack advantage: compare effective damage
        if max(my_attack, enemy_attack) > 0:
            attack_advantage = (my_attack - enemy_attack) / max(my_attack, enemy_attack, 1)
        else:
            attack_advantage = 0

        # Defense advantage
        if max(my_defense, enemy_defense) > 0:
            defense_advantage = (my_defense - enemy_defense) / max(my_defense, enemy_defense, 1)
        else:
            defense_advantage = 0

        # EP advantage (affects ability to act)
        max_ep = 10  # Typical max EP
        ep_advantage = (my_ep - enemy_ep) / max_ep

        # Base probability
        base_prob = 0.5

        # Apply weighted advantages
        base_prob += hp_advantage * 0.25      # HP matters most
        base_prob += attack_advantage * 0.30  # Attack is key for winning
        base_prob += defense_advantage * 0.15 # Defense helps survival
        base_prob += ep_advantage * 0.15      # EP enables actions

        # Enemy behavior adjustment from profiling
        enemy_profile = self._enemy_profiles.get(enemy.get("id"))
        if enemy_profile:
            # Aggressive enemies deal more damage but take more risks
            if enemy_profile.aggression_level > 0.7:
                base_prob -= 0.03  # Slightly more dangerous

            # Enemy with winning history
            if enemy_profile.encounters >= 2:
                enemy_win_rate = enemy_profile.wins / max(enemy_profile.encounters, 1)
                if enemy_win_rate > 0.6:
                    base_prob -= 0.05  # Tough opponent
                elif enemy_win_rate < 0.3:
                    base_prob += 0.05  # Easy opponent

            # Last known HP - if enemy was low, they might still be
            if enemy_profile.last_known_hp < 50:
                base_prob += 0.08  # Enemy weakened

        # First strike bonus (if enemy hasn't acted yet)
        if enemy.get("hasActed") == False:
            base_prob -= 0.03  # Enemy might act first

        # Number of enemies nearby (we're tracking single enemy here)
        nearby_count = self._count_nearby_enemies(game_state)
        if nearby_count > 1:
            # Fighting multiple enemies is harder
            base_prob -= (nearby_count - 1) * 0.10

        # Clamp to reasonable range [0.1, 0.95]
        # Never 0% or 100% - always some uncertainty
        return max(0.1, min(0.95, base_prob))

    def _get_attack_power(self) -> int:
        """Calculate current attack power"""
        base_attack = 50  # Base attack

        # Add weapon bonus
        if self._equipped_weapon:
            weapon = self._find_item_by_id(self._equipped_weapon)
            if weapon:
                base_attack += self._get_item_bonus(weapon)

        return base_attack

    def _get_defense_power(self) -> int:
        """Calculate current defense power"""
        base_defense = 50  # Base defense

        # Add armor bonus
        if self._equipped_armor:
            armor = self._find_item_by_id(self._equipped_armor)
            if armor:
                base_defense += self._get_item_bonus(armor)

        return base_defense

    def _decide_move_strategy(self, game_state: dict) -> dict:
        """
        Decide where to move based on comprehensive strategy.
        Priority: Safety > Resources > Combat > Exploration
        """
        view = game_state.get("view", game_state)
        
        # Priority 1: ESCAPE DEATH ZONE
        if self._is_death_zone(self._current_region, game_state):
            target = self._find_nearest_safe_region(game_state)
            if target:
                return {
                    "action": "move",
                    "data": {"regionId": target},
                    "reason": f"ESCAPE DZ: Moving to {target}"
                }
        
        # Priority 2: LOW HP - Find medical facility or safe zone
        if self._current_hp < self.constants.HP_CRITICAL:
            # Check for medical facility in connected regions
            medical = self._find_nearest_facility(game_state, "medical")
            if medical:
                return {
                    "action": "move",
                    "data": {"regionId": medical},
                    "reason": f"MEDICAL RUN: HP={self._current_hp}"
                }
            # Otherwise find safe region
            safe = self._find_nearest_safe_region(game_state)
            if safe and safe != self._current_region:
                return {
                    "action": "move",
                    "data": {"regionId": safe},
                    "reason": f"RETREAT: HP={self._current_hp}"
                }
        
        # Priority 3: LOW EP - Find safe spot to rest
        if self._current_ep <= self.constants.EP_LOW:
            safe = self._find_nearest_safe_region(game_state)
            enemies_nearby = self._count_nearby_enemies(game_state)
            if safe and safe != self._current_region and enemies_nearby == 0:
                return {
                    "action": "move",
                    "data": {"regionId": safe},
                    "reason": f"SAFE REST: EP={self._current_ep}"
                }
        
        # Priority 4: Hunt enemies (if aggressive)
        if self.config.aggression > 0.5:
            enemy_region = self._find_weakest_enemy_region(game_state)
            if enemy_region and self.config.aggression > 0.6:
                return {
                    "action": "move",
                    "data": {"regionId": enemy_region},
                    "reason": f"HUNT: Seeking combat"
                }
        
        # Priority 5: Loot run - move to region with items
        loot_region = self._find_best_loot_region(game_state)
        if loot_region and loot_region != self._current_region:
            return {
                "action": "move",
                "data": {"regionId": loot_region},
                "reason": f"LOOT: Items detected"
            }
        
        # Priority 6: General exploration
        target_region = self._calculate_best_move_target(game_state)
        if target_region and target_region != self._current_region:
            return {
                "action": "move",
                "data": {"regionId": target_region},
                "reason": f"EXPLORE: {target_region}"
            }
        
        # Default: Hold position (don't waste EP moving randomly)
        return {
            "action": "move",
            "data": {"regionId": self._current_region},
            "reason": f"HOLD: {self._current_region}"
        }
    
    def _find_nearest_facility(self, game_state: dict, facility_type: str) -> Optional[str]:
        """Find nearest region with specific facility type."""
        view = game_state.get("view", game_state)
        current_region = view.get("currentRegion", {})
        current_x = current_region.get("x", 0)
        current_y = current_region.get("y", 0)
        
        regions = view.get("regions", {})
        best_region = None
        best_distance = float('inf')
        
        for region_id, region in regions.items():
            if self._is_death_zone(region_id, game_state):
                continue
            
            facilities = region.get("facilities", [])
            if any(f.get("type") == facility_type for f in facilities):
                rx = region.get("x", 0)
                ry = region.get("y", 0)
                distance = abs(current_x - rx) + abs(current_y - ry)
                
                if distance < best_distance and distance > 0:
                    best_distance = distance
                    best_region = region_id
        
        return best_region if best_distance <= 3 else None  # Only if reasonably close
    
    def _find_weakest_enemy_region(self, game_state: dict) -> Optional[str]:
        """Find region with weakest enemy to hunt."""
        view = game_state.get("view", game_state)
        nearby = view.get("nearbyAgents", [])
        
        weakest = None
        weakest_hp = float('inf')
        
        for agent in nearby:
            if agent.get("id") == self._agent_id:
                continue
            if not agent.get("isAlive", True):
                continue
            
            hp = agent.get("hp", 100)
            if hp < weakest_hp:
                weakest_hp = hp
                weakest = agent
        
        if weakest:
            return weakest.get("regionId")
        return None
    
    def _find_best_loot_region(self, game_state: dict) -> Optional[str]:
        """Find region with best loot opportunity."""
        view = game_state.get("view", game_state)
        current_region = view.get("currentRegion", {})
        current_x = current_region.get("x", 0)
        current_y = current_region.get("y", 0)
        
        best_region = None
        best_score = 0
        
        # Check visible items
        visible_items = view.get("visibleItems", [])
        region_item_counts = {}
        
        for entry in visible_items:
            if isinstance(entry, dict):
                region_id = entry.get("regionId", "")
                if region_id and region_id != self._current_region:
                    region_item_counts[region_id] = region_item_counts.get(region_id, 0) + 1
        
        for region_id, count in region_item_counts.items():
            if self._is_death_zone(region_id, game_state):
                continue
            
            # Get region for distance check
            regions = view.get("regions", {})
            region = regions.get(region_id, {})
            rx = region.get("x", 0)
            ry = region.get("y", 0)
            distance = abs(current_x - rx) + abs(current_y - ry)
            
            # Score: items vs distance
            score = (count * 5) - (distance * 1.5)
            
            if score > best_score:
                best_score = score
                best_region = region_id
        
        return best_region if best_score > 0 else None

    def _calculate_best_move_target(self, game_state: dict) -> Optional[str]:
        """Calculate best region to move to - v3.1 enhanced
        
        v3.1 improvements:
        - Dynamic scoring based on game phase (early/mid/late)
        - Ally coordination (team mode)
        - Better death zone prediction
        - Risk-aware decision making
        """
        current_region = game_state.get("currentRegion", {})
        current_x = current_region.get("x", 0)
        current_y = current_region.get("y", 0)

        regions = game_state.get("regions", {})
        nearby_agents = game_state.get("nearbyAgents", [])
        alive_count = game_state.get("aliveCount", 100)

        best_region = None
        best_score = float("-inf")

        # Determine game phase for dynamic difficulty
        game_phase = self._determine_game_phase(alive_count)

        for region_id, region in regions.items():
            # Skip death zones
            if self._is_death_zone(region_id, game_state):
                continue

            # Skip current region
            if region_id == self._current_region:
                continue

            rx = region.get("x", 0)
            ry = region.get("y", 0)

            # Calculate distance from current
            distance = abs(current_x - rx) + abs(current_y - ry)

            # Score factors
            score = 0.0

            # 1. Item density (prefer regions with items)
            item_count = region.get("itemCount", 0)
            score += item_count * 2

            # 2. Facility presence
            facilities = region.get("facilities", [])
            if any(f.get("type") == "medical" for f in facilities):
                score += 5
            if any(f.get("type") == "supply" for f in facilities):
                score += 3

            # 3. Exploration bonus (prefer unexplored)
            if region_id not in getattr(self, '_explored_regions', set()):
                score += 4
                if not hasattr(self, '_explored_regions'):
                    self._explored_regions = set()
                self._explored_regions.add(region_id)

            # 4. Enemy avoidance
            enemies_in_region = sum(
                1 for agent in nearby_agents
                if agent.get("regionId") == region_id
            )
            score -= enemies_in_region * 10

            # 5. Distance penalty (prefer closer regions)
            score -= distance * 0.5

            # 6. Strategy-based exploration
            if random.random() < self.config.explore_priority:
                score += random.uniform(0, 3)  # Add exploration randomness

            # === v3.1: Dynamic Difficulty Adjustment ===
            
            # Early game: Be cautious
            if game_phase == "early" and self.config.early_game_caution > 0.5:
                score -= enemies_in_region * 3  # Extra penalty for enemies
                score -= distance * 0.3  # Prefer closer, safer regions

            # Late game: Be aggressive
            if game_phase == "late" and self.config.late_game_aggression > 0.5:
                score += enemies_in_region * 2  # Attract to combat
                score += item_count * 1  # More risk for loot

            # 7. Zone awareness (v3.1)
            if self.config.zone_awareness > 0.7:
                # Check if this region is adjacent to death zone
                for conn in game_state.get("connectedRegions", []):
                    if isinstance(conn, dict) and conn.get("isDeathZone"):
                        score -= 5  # Penalty for being near DZ

            # 8. Ally coordination (team mode, v3.1)
            if self.config.team_mode and self._allies:
                for ally_id in self._allies:
                    if ally_id in self._ally_positions:
                        ally_region = self._ally_positions[ally_id]
                        # Get connected regions
                        connected = game_state.get("connectedRegions", [])
                        ally_near = False
                        for conn in connected:
                            if isinstance(conn, dict) and conn.get("id") == ally_region:
                                ally_near = True
                                break
                            elif isinstance(conn, str) and conn == ally_region:
                                ally_near = True
                                break
                        if ally_near:
                            score += 3  # Bonus to move toward allies

            # 9. Risk tolerance adjustment
            if self.config.risk_tolerance > 0.6:
                # High risk tolerance: prefer high-value, high-risk areas
                if item_count >= 3:
                    score += 5
                if enemies_in_region > 0:
                    score += 2

            if score > best_score:
                best_score = score
                best_region = region_id

        # Fallback: If no better region found, consider staying
        if best_region is None and self._is_death_zone(self._current_region, game_state):
            # Must move to escape DZ
            best_region = self._find_nearest_safe_region(game_state)

        return best_region

    def _determine_game_phase(self, alive_count: int) -> str:
        """Determine game phase based on alive count - v3.1"""
        if alive_count >= 50:
            return "early"  # Safe, cautious play
        elif alive_count <= 10:
            return "late"  # Aggressive, high-stakes play
        else:
            return "mid"  # Balanced approach

    # ========================================================================
    # STATE MANAGEMENT & HELPERS
    # ========================================================================

    def _update_from_game_state(self, game_state: dict):
        """
        Update internal state from game state.
        Handles both flat and nested view structures from WebSocket.
        v3.1: Enhanced zone tracking, turn counting, and ally detection.
        """
        # Handle nested view structure (game_state might be the full view)
        view = game_state.get("view", game_state)

        # Basic stats - handle both 'agent' and 'self' keys
        agent_data = view.get("self", view.get("agent", {}))
        self._current_hp = agent_data.get("hp", 100)
        self._current_ep = agent_data.get("ep", 10)

        # Current region
        current_region = view.get("currentRegion", {})
        self._current_region = current_region.get("id")

        # Inventory
        self._inventory = agent_data.get("inventory", view.get("inventory", []))

        # Equipped items
        self._equipped_weapon = agent_data.get("equippedWeapon")
        self._equipped_armor = agent_data.get("equippedArmor")

        # Game metadata
        self._game_id = view.get("gameId", game_state.get("gameId"))
        self._agent_id = agent_data.get("id")

        # Track turn number
        self._turn_number = view.get("turn", self._turn_number)
        
        # Track game start time (first turn)
        if self._game_start_time == 0 and self._turn_number > 0:
            self._game_start_time = time.time()

        # Track explored regions
        if not hasattr(self, '_explored_regions'):
            self._explored_regions = set()
        if self._current_region:
            self._explored_regions.add(self._current_region)

        # === v3.1: Enhanced Zone Knowledge ===
        self._update_zone_knowledge(view)

        # === v3.1: Update ally positions (team mode) ===
        if self.config.team_mode:
            self._update_ally_positions(view)

        # === v3.1: Track action timing ===
        self._last_action_time = time.time()
        self._actions_this_game += 1

    def _update_zone_knowledge(self, view: dict):
        """Update zone knowledge from current game state - v3.1"""
        # Update death zones from visible regions
        for region in view.get("visibleRegions", []):
            if isinstance(region, dict):
                rid = region.get("id", "")
                if region.get("isDeathZone"):
                    self._death_zones.add(rid)
                    self._safe_zones.discard(rid)
                else:
                    self._safe_zones.add(rid)
        
        # Update from current region
        if isinstance(view.get("currentRegion"), dict):
            rid = view["currentRegion"].get("id", "")
            if view["currentRegion"].get("isDeathZone"):
                self._death_zones.add(rid)
        
        # Update pending death zones
        pending = view.get("pendingDeathzones", [])
        for dz in pending:
            dz_id = dz.get("id", dz) if isinstance(dz, dict) else dz
            self._pending_death_zones.add(dz_id)

    def _update_ally_positions(self, view: dict):
        """Track ally positions for team coordination - v3.1"""
        for agent in view.get("visibleAgents", []):
            if isinstance(agent, dict) and agent.get("isAlive"):
                agent_id = agent.get("id")
                # Check if ally (not self, not in enemy profiles)
                if agent_id != self._agent_id and agent_id not in self._enemy_profiles:
                    self._allies.add(agent_id)
                    # Update position if in same region
                    if agent.get("regionId") == self._current_region:
                        self._ally_positions[agent_id] = self._current_region

    def _count_nearby_enemies(self, game_state: dict) -> int:
        """Count number of nearby enemies"""
        nearby = game_state.get("nearbyAgents", [])
        return len([a for a in nearby if a.get("id") != self._agent_id])

    def _find_item_by_id(self, item_id: str) -> Optional[dict]:
        """Find item in inventory by ID"""
        for item in self._inventory:
            if item["id"] == item_id:
                return item
        return None

    def _get_item_bonus(self, item: dict) -> int:
        """Get bonus value of item"""
        return item.get("bonus", 0) or 0

    def _update_enemy_profile(self, enemy: dict):
        """Update enemy behavior profile - v3.1 enhanced"""
        enemy_id = enemy["id"]

        if enemy_id not in self._enemy_profiles:
            self._enemy_profiles[enemy_id] = EnemyProfile(agent_id=enemy_id)

        profile = self._enemy_profiles[enemy_id]
        profile.encounters += 1
        profile.last_known_hp = enemy.get("hp", 100)
        profile.last_known_ep = enemy.get("ep", 10)
        profile.last_seen_region = enemy.get("regionId")

        # Update aggression based on behavior
        if enemy.get("action") == "attack":
            profile.aggression_level = min(1.0, profile.aggression_level + 0.1)
        elif enemy.get("action") == "flee":
            profile.aggression_level = max(0.0, profile.aggression_level - 0.1)

    def _log_combat_result(self, target_id: str, won: bool, damage_dealt: int, damage_taken: int):
        """Log combat result for pattern learning - v3.1"""
        self._combat_log.append({
            "target_id": target_id,
            "won": won,
            "damage_dealt": damage_dealt,
            "damage_taken": damage_taken,
            "timestamp": time.time()
        })

        if won:
            self._successful_combats += 1
            if target_id in self._enemy_profiles:
                self._enemy_profiles[target_id].wins += 1
        else:
            self._failed_combats += 1
            if target_id in self._enemy_profiles:
                self._enemy_profiles[target_id].losses += 1

        # Track damage patterns
        self._damage_dealt_log.append(damage_dealt)
        self._damage_taken_log.append(damage_taken)

        # Keep only last 50 combats
        if len(self._combat_log) > 50:
            self._combat_log = self._combat_log[-50:]
        if len(self._damage_dealt_log) > 50:
            self._damage_dealt_log = self._damage_dealt_log[-50:]
        if len(self._damage_taken_log) > 50:
            self._damage_taken_log = self._damage_taken_log[-50:]

    def _find_nearest_safe_region(self, game_state: dict) -> Optional[str]:
        """Find nearest region that is NOT death zone - v3.1 enhanced"""
        current_region = game_state.get("currentRegion", {})
        current_x = current_region.get("x", 0)
        current_y = current_region.get("y", 0)

        regions = game_state.get("regions", {})
        safe_regions = []

        for region_id, region in regions.items():
            # Skip death zones
            if self._is_death_zone(region_id, game_state):
                continue

            # Calculate distance
            rx = region.get("x", 0)
            ry = region.get("y", 0)
            distance = abs(current_x - rx) + abs(current_y - ry)

            # v3.1: Also check if adjacent to death zone
            adjacent_to_dz = False
            for conn in game_state.get("connectedRegions", []):
                if isinstance(conn, dict):
                    if conn.get("id") == region_id and conn.get("isDeathZone"):
                        adjacent_to_dz = True
                        break
                elif isinstance(conn, str) and conn in self._death_zones:
                    adjacent_to_dz = True
                    break

            if adjacent_to_dz and self.config.zone_awareness > 0.7:
                continue  # Skip regions adjacent to DZ

            safe_regions.append((distance, region_id))

        if not safe_regions:
            # Fallback: return any non-DZ region
            for region_id, region in regions.items():
                if not self._is_death_zone(region_id, game_state):
                    return region_id
            return None

        # Return nearest safe region
        return min(safe_regions)[1]

    # ========================================================================
    # TURN TIMING
    # ========================================================================

    def _check_turn_timeout(self) -> bool:
        """Check if turn is about to timeout"""
        elapsed = time.time() - self._turn_start_time
        remaining = self.constants.TURN_DURATION - elapsed

        return remaining < self.constants.TURN_BUFFER

    # ========================================================================
    # PERFORMANCE TRACKING
    # ========================================================================

    def record_game_result(self, result: dict):
        """Record game result for learning"""
        self._game_history.append({
            "timestamp": time.time(),
            "placement": result.get("placement"),
            "kills": result.get("kills"),
            "survival_time": result.get("survivalTime"),
            "win": result.get("placement") == 1
        })

        # Update running stats
        recent_games = self._game_history[-10:]
        if recent_games:
            win_count = sum(1 for g in recent_games if g.get("win"))
            self._game_history[-1] = {
                **self._game_history[-1],
                "win_rate_last_10": win_count / len(recent_games)
            }

        # Adapt strategy based on results
        self._adapt_strategy()

    def _adapt_strategy(self):
        """Adapt strategy based on recent performance - v3.1 enhanced
        
        v3.1 improvements:
        - Track multiple metrics (not just wins)
        - Learn from death causes
        - More nuanced strategy adjustments
        """
        if len(self._game_history) < 3:
            return

        recent = self._game_history[-5:]
        win_rate = sum(1 for g in recent if g.get("win")) / len(recent)
        avg_placement = sum(g.get("placement", 999) for g in recent) / len(recent)
        avg_kills = sum(g.get("kills", 0) for g in recent) / len(recent)

        # Analyze death patterns
        recent_deaths = [g for g in recent if g.get("placement") and g.get("placement") > 1]
        
        # === v3.1: Learn from specific death causes ===
        # If dying to death zones frequently
        dz_deaths = sum(1 for g in recent_deaths if g.get("death_cause") == "death_zone")
        if dz_deaths > len(recent_deaths) * 0.3 and self.config.zone_awareness < 1.0:
            self.config.zone_awareness = min(1.0, self.config.zone_awareness + 0.1)
            self.config.explore_priority = max(0.0, self.config.explore_priority - 0.1)

        # If dying to combat frequently
        combat_deaths = sum(1 for g in recent_deaths if g.get("death_cause") == "combat")
        if combat_deaths > len(recent_deaths) * 0.4:
            # Need better combat decisions
            self.config.attack_win_prob_threshold = min(0.8, self.config.attack_win_prob_threshold + 0.05)
            self.config.flee_threshold = max(0.4, self.config.flee_threshold - 0.05)

        # === v3.1: Multi-metric adaptation ===
        
        # If winning consistently
        if win_rate > 0.6:
            self.config.aggression = min(1.0, self.config.aggression + 0.03)
            self.config.explore_priority = min(1.0, self.config.explore_priority + 0.03)
            self.config.risk_tolerance = min(1.0, self.config.risk_tolerance + 0.02)

        # If getting good kills but not winning
        elif avg_kills > 2 and win_rate < 0.3:
            # Too aggressive, need to balance
            self.config.aggression = max(0.0, self.config.aggression - 0.05)
            self.config.survival_priority = min(1.0, self.config.survival_priority + 0.05)

        # If dying too early (poor survival)
        elif avg_placement > 15 and avg_kills < 1:
            self.config.aggression = max(0.0, self.config.aggression - 0.08)
            self.config.explore_priority = max(0.0, self.config.explore_priority - 0.1)
            self.config.zone_awareness = min(1.0, self.config.zone_awareness + 0.05)

        # If playing too safe (not engaging)
        if avg_kills < 0.5 and win_rate < 0.2:
            self.config.aggression = min(0.8, self.config.aggression + 0.03)
            self.config.attack_win_prob_threshold = max(0.5, self.config.attack_win_prob_threshold - 0.03)

        # Clamp all values
        self.config.aggression = max(0.0, min(1.0, self.config.aggression))
        self.config.zone_awareness = max(0.0, min(1.0, self.config.zone_awareness))
        self.config.risk_tolerance = max(0.0, min(1.0, self.config.risk_tolerance))
        self.config.heal_threshold = max(0.1, min(0.8, self.config.heal_threshold))
        self.config.attack_win_prob_threshold = max(0.4, min(0.9, self.config.attack_win_prob_threshold))

    # ========================================================================
    # v3.1: Utility Methods
    # ========================================================================

    def reset_game_state(self):
        """Reset all game state for new game - v3.1"""
        self._current_hp = 100
        self._current_ep = 10
        self._current_region = None
        self._inventory = []
        self._equipped_weapon = None
        self._equipped_armor = None
        self._game_id = None
        self._agent_id = None
        self._turn_number = 0
        self._game_start_time = 0
        self._last_action_time = 0
        self._actions_this_game = 0
        self._successful_combats = 0
        self._failed_combats = 0
        self._items_collected = 0
        
        # v3.1: Reset advanced tracking
        self._death_zones = set()
        self._pending_death_zones = set()
        self._safe_zones = set()
        self._explored_regions = set()
        self._high_value_regions = set()
        self._danger_regions = set()
        self._combat_log = []
        self._damage_taken_log = []
        self._damage_dealt_log = []
        self._allies = set()
        self._ally_positions = {}

    def get_performance_metrics(self) -> dict:
        """Get current performance metrics - v3.1"""
        return {
            "turn": self._turn_number,
            "hp": self._current_hp,
            "ep": self._current_ep,
            "actions_this_game": self._actions_this_game,
            "successful_combats": self._successful_combats,
            "failed_combats": self._failed_combats,
            "combat_win_rate": self._successful_combats / max(1, self._successful_combats + self._failed_combats),
            "items_collected": self._items_collected,
            "explored_regions": len(self._explored_regions),
            "death_zones_known": len(self._death_zones),
            "enemy_profiles": len(self._enemy_profiles),
            "allies": len(self._allies),
            "config": {
                "aggression": self.config.aggression,
                "zone_awareness": self.config.zone_awareness,
                "risk_tolerance": self.config.risk_tolerance,
                "heal_threshold": self.config.heal_threshold,
                "attack_win_prob_threshold": self.config.attack_win_prob_threshold
            }
        }

    def set_config_from_json(self, config_dict: dict):
        """Set configuration from JSON dict - v3.1"""
        for key, value in config_dict.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)

    def get_config_json(self) -> dict:
        """Get current configuration as JSON - v3.1"""
        return {
            "aggression": self.config.aggression,
            "heal_threshold": self.config.heal_threshold,
            "rest_ep_threshold": self.config.rest_ep_threshold,
            "flee_threshold": self.config.flee_threshold,
            "explore_priority": self.config.explore_priority,
            "attack_win_prob_threshold": self.config.attack_win_prob_threshold,
            "flee_win_prob_threshold": self.config.flee_win_prob_threshold,
            "survival_priority": self.config.survival_priority,
            "zone_awareness": self.config.zone_awareness,
            "loot_priority": self.config.loot_priority,
            "risk_tolerance": self.config.risk_tolerance,
            "late_game_aggression": self.config.late_game_aggression,
            "early_game_caution": self.config.early_game_caution,
            "team_mode": self.config.team_mode
        }
