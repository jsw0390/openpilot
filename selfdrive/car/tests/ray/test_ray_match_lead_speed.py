"""Match a nearby lead using acknowledged stock steps, with no vehicle I/O."""
import pytest

import test_ray_manual_off as upper
import test_ray_controller_cancel as lower

target = upper.namespace['ray_lead_target_speed_kph']


@pytest.mark.parametrize('lead,expected', [(45.,45.), (45.2,45.), (45.8,46.), (59.,59.), (25.,30.), (0.,30.)])
def test_close_slower_lead_targets_estimate_without_positive_margin(lead, expected):
  assert target(60 / 3.6, 45., (lead - 60) / 3.6, lead, 60.) == expected


def test_one_kph_difference_is_not_left_above_lead_speed():
  assert target(45 / 3.6, 30., 0., 45., 46.) == 45.


def test_no_acceleration_to_faster_lead_or_reduction_for_distant_lead():
  assert target(60 / 3.6, 100., -5., 42., 60.) is None
  assert target(45 / 3.6, 35., 3., 55.8, 45.) is None


def test_short_gap_still_requires_more_than_speed_matching():
  assert target(50 / 3.6, 10., 0., 50., 60.) < 50.


@pytest.mark.parametrize('index', range(5))
@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_signals_never_produce_a_target(index, bad):
  values = [60 / 3.6, 45., -5., 42., 60.]
  values[index] = bad
  assert target(*values) is None


def test_low_confidence_and_unknown_stock_setting_never_match():
  assert target(60 / 3.6, 45., -5., 42., 60., lead_prob=.5) is None
  assert target(60 / 3.6, 45., -5., 42., 0.) is None


def test_upper_and_lower_controllers_reach_lead_speed_only_after_cluster_feedback():
  b = upper.TestRayManualOff(); b.setup_method()
  c = lower.TestRayControllerCancel(); c.setup_method()
  h = b.helper
  h.rayVisionIPedalAssist = 3
  h.d_rel, h.v_lead_kph, h.lead_prob = 40., 45., .99
  stock = 60.
  presses = []
  pending = None
  for frame in range(1300):
    # Simulated factory acknowledgement after 80 ms. This does not model braking.
    if pending is not None and frame == pending:
      stock -= 1
      pending = None
    b.cs.cruiseState.speed = stock / 3.6
    b.cs.vEgo = stock / 3.6
    h.v_ego_kph_set = stock
    h.v_rel = (45. - stock) / 3.6
    h._activate_cruise = 0
    h._update_ray_ipedal_assist(b.cs, b.cc, stock)
    assert h._activate_cruise in (0, 3)
    assert h._lat_enabled
    c.cs.out.gearStep = 7
    c.cs.out.cruiseState.speed = stock / 3.6
    c.cs.out.vEgo = stock / 3.6
    c.cs.out.activateCruise = h._activate_cruise
    button = c.h.make_spam_button(c.cc, c.cs)
    if button:
      assert button == lower.Buttons.SET_DECEL
      assert pending is None
      pending = frame + 8
      presses.append(frame)
    c.h.frame += 1
  assert stock == 45.
  assert len(presses) == 15
  assert all(y - x >= 50 for x, y in zip(presses, presses[1:]))
  assert h._ray_stock_restore_kph == pytest.approx(60.)


def test_small_estimate_variation_cannot_restore_then_lower_repeatedly():
  b = upper.TestRayManualOff(); b.setup_method()
  h = b.helper
  h.rayVisionIPedalAssist = 3
  h._ray_stock_restore_kph = 60.
  h.v_ego_kph_set = 45.
  b.cs.vEgo = b.cs.cruiseState.speed = 45 / 3.6
  h.d_rel, h.lead_prob = 30., .99
  for estimate in [44.6, 45.2, 45.4, 44.9] * 100:
    h.v_lead_kph = estimate
    h.v_rel = (estimate - 45) / 3.6
    h._activate_cruise = 0
    h._update_ray_ipedal_assist(b.cs, b.cc, 45.)
    assert h._activate_cruise == 0
