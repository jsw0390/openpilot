import math


def lead_speed_text(sm, is_metric: bool, started_frame: int, now: float) -> str | None:
  """Format the primary lead's estimated absolute speed, never a cached setpoint."""
  for service in ('radarState', 'modelV2'):
    age = now - sm.recv_time[service]
    if (not sm.alive[service] or not sm.valid[service] or
        sm.recv_frame[service] < started_frame or not 0 <= age <= 0.5):
      return None

  lead = sm['radarState'].leadOne
  speed = float(lead.vLeadK)
  distance = float(lead.dRel)
  probability = float(lead.modelProb)
  if (not lead.status or not all(math.isfinite(v) for v in (speed, distance, probability)) or
      probability < 0.85 or not 2.5 < distance <= 200 or not -1 <= speed <= 100):
    return None

  # radard may briefly extrapolate a lost vision lead; don't present that as a fresh sighting.
  vision = sm['modelV2'].leadsV3
  if not vision or not vision[0].x:
    return None
  model_probability = float(vision[0].prob)
  model_distance = float(vision[0].x[0])
  if (not math.isfinite(model_probability) or model_probability < 0.85 or
      not math.isfinite(model_distance) or abs(model_distance - distance) > max(8., distance * 0.25)):
    return None

  # Filtering can put a stationary lead slightly below zero.
  speed = max(0.0, speed) * (3.6 if is_metric else 2.2369363)
  return f"{speed:.0f} {'km/h' if is_metric else 'mph'}"
