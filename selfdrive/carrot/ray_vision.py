RAY_CURVE_SOURCES = {"atc", "atc2", "vturn", "model", "route", "mapd", "mapd_curve"}
RAY_VTURN_SHARP_KPH = 35.0
RAY_CURVE_DROP_KPH = 12.0
RAY_LEAD_BASE_T_FOLLOW = 1.0
RAY_LEAD_T_FOLLOW_ADD_FACTOR = 0.5
RAY_LEAD_MIN_DISTANCE_M = 8.0


def ray_desired_speed_allowed(source, desired_kph, base_cruise_kph, vturn_kph=0.0, *,
                              enabled=True, disabled_result=True, allow_non_curve=True):
  if not enabled:
    return disabled_result

  source = str(source or "")
  if source == "road":
    return False
  if source not in RAY_CURVE_SOURCES:
    return allow_non_curve

  desired_kph = float(desired_kph or 0.0)
  if not (0.0 < desired_kph < 200.0):
    return False

  drop_kph = float(base_cruise_kph or 0.0) - desired_kph
  if drop_kph < 7.0:
    return False

  raw_vturn_kph = abs(float(vturn_kph or 0.0))
  if source == "vturn" and raw_vturn_kph > 0.0:
    return raw_vturn_kph <= RAY_VTURN_SHARP_KPH

  return drop_kph >= RAY_CURVE_DROP_KPH


def ray_lead_target_speed_kph(ego_speed_ms, lead_distance_m, lead_rel_speed_ms, lead_speed_kph,
                              cruise_kph, t_follow_add=0.0, *, lead_prob=1.0, lead_radar=False,
                              min_prob=0.85, min_target_kph=30.0):
  if lead_distance_m <= 0.0 or ego_speed_ms < 1.0:
    return None
  if not lead_radar and lead_prob < min_prob:
    return None

  follow_time = RAY_LEAD_BASE_T_FOLLOW + max(0.0, min(1.0, t_follow_add)) * RAY_LEAD_T_FOLLOW_ADD_FACTOR
  desired_dist = max(RAY_LEAD_MIN_DISTANCE_M, ego_speed_ms * follow_time)
  closing = lead_rel_speed_ms < -0.5 and lead_distance_m < max(35.0, ego_speed_ms * 2.2)
  too_close = lead_distance_m < desired_dist
  if not closing and not too_close:
    return None

  lead_speed_kph = max(0.0, float(lead_speed_kph or 0.0))
  target_kph = min(float(cruise_kph), lead_speed_kph + (2.0 if too_close else 5.0))
  if too_close:
    shortfall = max(0.0, desired_dist - lead_distance_m)
    decel_margin_kph = min(10.0, 2.0 + shortfall * 0.45)
    target_kph = min(target_kph, ego_speed_ms * 3.6 - decel_margin_kph)

  return max(min_target_kph, target_kph)
