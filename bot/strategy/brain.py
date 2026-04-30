"""
 Simple adaptive strategy brain.
 Focus: survival-first, avoid bad fights, then secure value.
 """
 
 from bot.utils.logger import get_logger
 
 log = get_logger(__name__)
 
  WEAPONS = {
    "fist": {"bonus": 0, "range": 0},
     "dagger": {"bonus": 10, "range": 0},
     "knife": {"bonus": 10, "range": 0},
     "sword": {"bonus": 20, "range": 0},
     "katana": {"bonus": 35, "range": 0},
    "bow": {"bonus": 5, "range": 1},
     "pistol": {"bonus": 10, "range": 1},
     "sniper": {"bonus": 28, "range": 2},
 }
 
  ITEM_PRIORITY = {
     "rewards": 300,
     "katana": 100,
     "sniper": 95,
     "sword": 90,
     "pistol": 85,
     "dagger": 80,
     "knife": 80,
     "bow": 75,
     "medkit": 70,
     "bandage": 65,
     "emergency_food": 60,
     "energy_drink": 55,
 }
 
 RECOVERY_ITEMS = {"medkit": 50, "bandage": 30, "emergency_food": 20}
 
 _map_knowledge: dict = {"death_zones": set(), "safe_center": []}
 _game_state: dict = {"turn": 0, "last_heal_turn": -5, "last_talk_turn": -10}
 
 
 def reset_game_state():
     global _map_knowledge, _game_state
     _map_knowledge = {"death_zones": set(), "safe_center": []}
     _game_state = {"turn": 0, "last_heal_turn": -5, "last_talk_turn": -10}
 
 
 def learn_from_map(view: dict):
     """Lightweight map learning hook used after map item usage."""
     if not isinstance(view, dict):
         return
     for region in view.get("visibleRegions", []):
         if isinstance(region, dict) and region.get("isDeathZone"):
             rid = region.get("id")
             if rid:
                 _map_knowledge["death_zones"].add(rid)
 
 
 def calc_damage(atk: int, weapon_bonus: int, target_def: int) -> int:
     return max(1, atk + weapon_bonus - int(target_def * 0.5))
 
 
 def _weapon_bonus(equipped: dict | None) -> int:
     if not equipped:
         return 0
     return WEAPONS.get(str(equipped.get("typeId", "")).lower(), {}).get("bonus", 0)
 
 
 def _weapon_range(equipped: dict | None) -> int:
     if not equipped:
         return 0
     return WEAPONS.get(str(equipped.get("typeId", "")).lower(), {}).get("range", 0)
 
 
 def _distance(target_region: str, current_region: str, connections: list) -> int:
     if not target_region or target_region == current_region:
         return 0
     for c in connections:
         if isinstance(c, str) and c == target_region:
             return 1
         if isinstance(c, dict) and c.get("id") == target_region:
             return 1
     return 99
 
 
 def _best_local_item(local_items: list[dict]) -> dict | None:
     best = None
     score = -1
     for item in local_items:
         t = str(item.get("typeId", "")).lower()
         s = ITEM_PRIORITY.get(t, 0)
         if s > score:
             score = s
             best = item
     return best
 
 
 def _find_heal(inventory: list[dict]) -> dict | None:
     heals = [i for i in inventory if str(i.get("typeId", "")).lower() in RECOVERY_ITEMS]
     if not heals:
         return None
     heals.sort(key=lambda i: RECOVERY_ITEMS.get(str(i.get("typeId", "")).lower(), 0), reverse=True)
     return heals[0]
 
 
 def _risk_score(hp: int, ep: int, enemies: list[dict], guardians: list[dict], my_def: int, my_bonus: int) -> float:
     score = 0.0
     if hp < 60:
         score += (60 - hp) * 0.8
     if ep < 3:
         score += (3 - ep) * 8
     score += len(enemies) * 14
     score += len(guardians) * 10
 
     enemy_power = 0
     for e in enemies:
         e_atk = int(e.get("atk", 10))
         e_w = WEAPONS.get(str(e.get("equippedWeapon", {}).get("typeId", "")).lower(), {}).get("bonus", 0)
         enemy_power += calc_damage(e_atk, e_w, my_def)
 
     my_power = calc_damage(10, my_bonus, 5)
     score += max(0, enemy_power - my_power * max(1, len(enemies))) * 0.5
     return round(score, 2)
 
 
 def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
     self_data = view.get("self", {}) if isinstance(view, dict) else {}
     if not isinstance(self_data, dict) or not self_data.get("isAlive", True):
         return None
 
     _game_state["turn"] += 1
 
     hp = int(self_data.get("hp", 100))
     ep = int(self_data.get("ep", 10))
     atk = int(self_data.get("atk", 10))
     defense = int(self_data.get("def", 5))
     my_id = self_data.get("id", "")
     inventory = self_data.get("inventory", []) or []
     equipped = self_data.get("equippedWeapon")
 
     region = view.get("currentRegion", {}) if isinstance(view, dict) else {}
     region_id = region.get("id", "") if isinstance(region, dict) else ""
     connections = view.get("connectedRegions", []) or region.get("connections", [])
     pending_dz = view.get("pendingDeathzones", [])
 
     vis_agents = view.get("visibleAgents", [])
     enemies = [a for a in vis_agents if a.get("isAlive") and not a.get("isGuardian") and a.get("id") != my_id]
     guardians = [a for a in vis_agents if a.get("isAlive") and a.get("isGuardian")]
 
     # free actions first: pickup / equip
     visible_items = []
     for e in view.get("visibleItems", []):
         if not isinstance(e, dict):
             continue
         i = e.get("item") if isinstance(e.get("item"), dict) else e
         rid = e.get("regionId") or i.get("regionId")
         if rid == region_id:
             merged = dict(i)
             merged["regionId"] = rid
             visible_items.append(merged)
 
     pick = _best_local_item(visible_items)
     if pick:
         return {"action": "pickup", "data": {"itemId": pick.get("id")}, "reason": "High-priority local item"}
 
     my_bonus = _weapon_bonus(equipped)
     inv_weapons = [i for i in inventory if str(i.get("typeId", "")).lower() in WEAPONS]
     best = max(inv_weapons, key=lambda i: WEAPONS.get(str(i.get("typeId", "")).lower(), {}).get("bonus", 0), default=None)
     if best and WEAPONS.get(str(best.get("typeId", "")).lower(), {}).get("bonus", 0) > my_bonus:
         return {"action": "equip", "data": {"itemId": best.get("id")}, "reason": "Upgrade weapon"}
 
     if not can_act:
     return None
     
     # zone escape
     danger_ids = {z.get("id") if isinstance(z, dict) else z for z in pending_dz}
     danger_ids |= _map_knowledge.get("death_zones", set())
     if region.get("isDeathZone") or region_id in danger_ids:
         for conn in connections:
             cid = conn.get("id") if isinstance(conn, dict) else conn
             if cid and cid not in danger_ids:
                 return {"action": "move", "data": {"regionId": cid}, "reason": "Escape death zone"}
 
     # adaptive retreat policy
     risk = _risk_score(hp, ep, enemies, guardians, defense, my_bonus)
     if risk >= 40 and ep >= 2:
         heal = _find_heal(inventory)
         if heal and hp < 85:
             return {"action": "use_item", "data": {"itemId": heal.get("id")}, "reason": f"High risk ({risk}) - heal"}
         for conn in connections:
             cid = conn.get("id") if isinstance(conn, dict) else conn
             if cid and cid not in danger_ids:
                 return {"action": "move", "data": {"regionId": cid}, "reason": f"High risk ({risk}) - reposition"}
 
     # choose fights only if favorable
     if enemies and ep >= 2:
         wrange = _weapon_range(equipped)
         target = min(enemies, key=lambda e: e.get("hp", 100))
         t_region = target.get("regionId", region_id)
         in_range = _distance(t_region, region_id, connections) <= wrange
         if in_range:
             my_dmg = calc_damage(atk, my_bonus, int(target.get("def", 5)))
             e_bonus = WEAPONS.get(str(target.get("equippedWeapon", {}).get("typeId", "")).lower(), {}).get("bonus", 0)
             enemy_dmg = calc_damage(int(target.get("atk", 10)), e_bonus, defense)
             if my_dmg >= enemy_dmg or target.get("hp", 100) <= my_dmg * 2:
                 return {"action": "attack", "data": {"targetId": target.get("id"), "targetType": "agent"}, "reason": "Favorable duel"}
 
     # resource recovery fallback
     if hp < 70:
         heal = _find_heal(inventory)
         if heal:
             return {"action": "use_item", "data": {"itemId": heal.get("id")}, "reason": "Recover HP"}
 
     if ep < 4:
         return {"action": "rest", "data": {}, "reason": "Recover EP"}

     # movement fallback
     for conn in connections:
         cid = conn.get("id") if isinstance(conn, dict) else conn
         if cid and cid not in danger_ids:
             return {"action": "move", "data": {"regionId": cid}, "reason": "Rotate to safe region"}
 
     return None