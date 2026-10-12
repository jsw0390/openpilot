import math


RAY_CURVE_SOURCES = {"atc", "atc2", "vturn", "model", "route"}
RAY_VTURN_SHARP_KPH = 35.0
RAY_CURVE_DROP_KPH = 12.0
RAY_LEAD_BASE_T_FOLLOW = 1.0
RAY_LEAD_T_FOLLOW_ADD_FACTOR = 0.5
RAY_LEAD_MIN_DISTANCE_M = 8.0


def ray_lead_pause_required(ego_speed_ms, distance_m, relative_ms, lead_prob, *, radar=False, min_prob=0.85):
  """Request release of stock cruise; this does not command braking or a stop."""
  if not all(math.isfinite(v) for v in (ego_speed_ms, distance_m, relative_ms, lead_prob)):
    return False
  if ego_speed_ms < 15 / 3.6 or distance_m <= 0 or (not radar and lead_prob < min_prob):
    return False
  # Ordinary following uses acknowledged -1 km/h steps first. Release cruise
  # when the projected gap is short, or the closing time is already small.
  closing_ms = max(0.0, -relative_ms)
  projected_gap = distance_m - closing_ms * 2.0
  closing = closing_ms >= 0.5 and (
    projected_gap < max(8.0, ego_speed_ms * 1.2) or distance_m / closing_ms <= 4.0)
  short_gap = distance_m < max(6.0, ego_speed_ms * 0.8) and relative_ms <= 0
  return closing or short_gap


def ray_resume_scene_clear(ego_speed_ms, reference_kph, radar, model, *, turning=False, min_prob=0.85):
  """Evaluate fresh inputs supplied by the caller; a dropout never grants resume."""
  action = model.action
  if (turning or action.shouldStop or not math.isfinite(action.desiredVelocity) or
      action.desiredVelocity < ego_speed_ms or not model.leadsV3):
    return False
  if not radar.leadOne.status and not radar.leadTwo.status:
    return all(math.isfinite(l.prob) and 0 <= l.prob < 0.2 for l in model.leadsV3)
  if not (30 <= reference_kph <= 160 and radar.leadOne.status):
    return False
  # A visible lead must be pulling away at a speed above the retained setting.
  # Require raw-model agreement, not just disappearance of the tracked lead.
  raw_lead = model.leadsV3[0]
  if (not math.isfinite(raw_lead.prob) or raw_lead.prob < min_prob or
      not raw_lead.x or not math.isfinite(raw_lead.x[0]) or
      abs(raw_lead.x[0] - radar.leadOne.dRel) > max(8.0, radar.leadOne.dRel * 0.25) or
      action.desiredVelocity < reference_kph / 3.6):
    return False
  for lead in (radar.leadOne, radar.leadTwo):
    if not lead.status:
      continue
    if (not all(math.isfinite(v) for v in (lead.dRel, lead.vRel, lead.vLeadK, lead.modelProb)) or
        lead.modelProb < min_prob or lead.dRel < max(35.0, reference_kph / 3.6 * 2.5) or
        lead.vRel < 1.0 / 3.6 or lead.vLeadK * 3.6 < reference_kph + 2):
      return False
  return True


def ray_stock_resume_allowed(CS, *, passive, inputs_valid, panda_ok, events, monitor):
  """Authorize a stock-button resume without enabling direct acceleration."""
  blocked = any(e.noEntry or e.userDisable or e.softDisable or e.immediateDisable or
                str(e.name) in ('driverDistracted1', 'driverDistracted2', 'driverDistracted3',
                                'driverUnresponsive1', 'driverUnresponsive2', 'driverUnresponsive3', 'tooDistracted')
                for e in events)
  return bool(not passive and inputs_valid and panda_ok and not blocked and monitor.isActiveMode and monitor.awarenessStatus > 0 and
              CS.activateCruise == 2 and CS.canValid and not CS.canTimeout and str(CS.gearShifter) == 'drive' and
              0 <= CS.gearStep <= 4 and CS.vEgo * 3.6 >= 30 and 30 <= CS.vCruiseCluster <= 160 and
              not CS.gasPressed and not CS.brakePressed and not CS.brakeHoldActive)


class RayLeadPause:
  """Own only a pause initiated here, with bounded acknowledgement and resume.

  Called at 100 Hz. No CAN I/O. A missing acknowledgement never retries the
  vehicle's toggle switch. Driver input must reset ownership immediately.
  """

  def __init__(self):
    self.reset()

  def reset(self):
    self.phase = 'idle'
    self.frames = 0
    self.clear_frames = 0
    self.stock_kph = 0.0

  def update(self, *, stock_active, stock_kph, ego_kph, need_pause, clear, valid):
    if not valid or stock_active is None or not math.isfinite(ego_kph):
      self.phase = 'lockout' if self.phase != 'idle' else 'idle'
      self.clear_frames = 0
      return -1 if self.phase == 'lockout' else 0
    if self.phase == 'lockout':
      # Keep withdrawing a queued resume. A late stock acknowledgement must
      # be paused once, not silently accepted after our timeout.
      return -1

    if self.phase == 'idle':
      if not (need_pause and stock_active and 30 <= stock_kph <= 160):
        return 0
      self.stock_kph = stock_kph
      self.phase = 'pause_wait'
      self.frames = 0
      self.clear_frames = 0

    self.frames += 1
    if self.phase == 'pause_wait':
      if stock_active is False:
        self.phase = 'paused'
        self.frames = 0
      elif self.frames > 100:
        self.phase = 'lockout'
      else:
        return -1

    if self.phase == 'paused':
      # Never start from a stop or inherit someone else's later cruise enable.
      if stock_active or ego_kph < 30 or self.frames > 1500:
        self.phase = 'lockout'
        return -1
      # Resume would immediately target the setting held at the pause. Limit
      # that first acceleration step; larger recovery requires driver input.
      can_resume = clear and not need_pause and abs(self.stock_kph - ego_kph) <= 10
      self.clear_frames = self.clear_frames + 1 if can_resume else 0
      if self.clear_frames >= 300:
        self.phase = 'resume_wait'
        self.frames = 0
        return 2

    elif self.phase == 'resume_wait':
      if need_pause or not clear or ego_kph < 30:
        self.phase = 'pause_wait' if stock_active else 'paused'
        self.frames = 0
        self.clear_frames = 0
        return -1  # also withdraw a queued resume before stock acknowledgement
      if stock_active:
        if not (30 <= stock_kph <= self.stock_kph + 1):
          self.phase = 'lockout'
          return -1
        self.reset()
      elif self.frames > 50:
        self.phase = 'lockout'
        return -1
      else:
        return 2
    return 0


def ray_restore_lead_clear(lead_distance_m, lead_rel_speed_ms, lead_speed_kph, restore_kph,
                           t_follow_add=0.0, *, lead_prob=1.0, lead_radar=False, min_prob=0.85):
  """Require clearance at the speed being restored, not just at current speed.

  The caller must first check the radar feed's validity and age. A slow lead
  that we have settled behind must not trigger repeated acceleration cycles.
  """
  if not all(math.isfinite(v) for v in (lead_distance_m, lead_rel_speed_ms, lead_speed_kph, restore_kph, lead_prob)):
    return False
  if lead_distance_m == 0.0:
    return True
  if lead_distance_m < 0.0 or (not lead_radar and lead_prob < min_prob):
    return False
  follow_time = RAY_LEAD_BASE_T_FOLLOW + max(0.0, min(1.0, t_follow_add)) * RAY_LEAD_T_FOLLOW_ADD_FACTOR
  clearance_m = max(RAY_LEAD_MIN_DISTANCE_M, restore_kph / 3.6 * follow_time) + 10.0
  return lead_distance_m >= clearance_m and lead_rel_speed_ms >= 0.0 and lead_speed_kph >= restore_kph + 2.0


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
  within_follow_range = lead_distance_m < max(35.0, ego_speed_ms * 3.5)
  closing = lead_rel_speed_ms < -0.5 and within_follow_range
  slower_setting = cruise_kph > lead_speed_kph + 3.0 and within_follow_range
  too_close = lead_distance_m < desired_dist
  if not closing and not too_close and not slower_setting:
    return None

  lead_speed_kph = max(0.0, float(lead_speed_kph or 0.0))
  target_kph = min(float(cruise_kph), lead_speed_kph + (2.0 if too_close else 5.0))
  if too_close:
    shortfall = max(0.0, desired_dist - lead_distance_m)
    decel_margin_kph = min(10.0, 2.0 + shortfall * 0.45)
    target_kph = min(target_kph, ego_speed_ms * 3.6 - decel_margin_kph)

  return max(min_target_kph, target_kph)
