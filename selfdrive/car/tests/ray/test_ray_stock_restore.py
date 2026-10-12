"""Automatic speed recovery, driver priority and physical cluster feedback."""
import pytest
from types import SimpleNamespace as NS

import test_ray_manual_off as upper
import test_ray_controller_cancel as lower
from test_ray_stock_cruise_policy import slower_lead, step


def slowed():
  b = slower_lead()
  b.cs.cruiseState.speed = 40 / 3.6
  b.cs.vEgo = 40 / 3.6
  b.helper.v_ego_kph_set = 40
  b.helper.d_rel = 0
  b.helper.v_rel = b.helper.v_lead_kph = b.helper.lead_prob = 0
  return b


def test_lead_clear_restores_original_speed_after_two_seconds():
  b = slowed()
  assert b.helper._ray_stock_restore_kph == pytest.approx(60)
  assert step(b, 199) == [0] * 199
  assert step(b, 1) == [4]
  assert b.helper._update_ray_ipedal_assist(b.cs, b.cc, 40) == pytest.approx(60)
  b.cs.cruiseState.speed = 60 / 3.6
  assert step(b, 1) == [0]
  assert b.helper._ray_stock_restore_kph == 0


@pytest.mark.parametrize('distance,relative,speed,prob', [
  (25, 0, 40, 0.99),       # matched a slower lead: no acceleration oscillation
  (120, 0, 40, 0.99),      # slow lead remains ahead even at long range
  (25, 2, 80, 0.99),       # insufficient distance at the restored speed
  (70, -1, 80, 0.99),      # closing lead
  (70, 2, 80, 0.5),        # uncertain track
  (70, 2, float('nan'), 0.99),
])
def test_unresolved_lead_never_restores(distance, relative, speed, prob):
  b = slowed()
  b.helper.d_rel, b.helper.v_rel, b.helper.v_lead_kph, b.helper.lead_prob = distance, relative, speed, prob
  assert 4 not in step(b, 500)


def test_faster_lead_with_clearance_allows_restoration():
  b = slowed()
  b.helper.d_rel, b.helper.v_rel, b.helper.v_lead_kph, b.helper.lead_prob = 70, 2, 80, 0.99
  radar = NS(leadOne=NS(status=True, dRel=70., vRel=2., vLeadK=80 / 3.6, modelProb=.99), leadTwo=NS(status=False))
  model = NS(leadsV3=[NS(prob=.99, x=[70.])], action=NS(shouldStop=False, desiredVelocity=80 / 3.6))
  b.helper._ray_lead_recovery_target_kph = upper.namespace['ray_lead_recovery_target_kph'](40 / 3.6, 40., 60., radar, model)
  assert step(b, 199) == [0] * 199
  assert step(b, 1) == [4]


@pytest.mark.parametrize('feed', ['_ray_lead_data_valid', '_ray_curve_data_valid'])
def test_stale_feed_restarts_clearance_dwell(feed):
  b = slowed()
  step(b, 150)
  setattr(b.helper, feed, False)
  assert step(b, 300) == [0] * 300
  setattr(b.helper, feed, True)
  assert step(b, 199) == [0] * 199
  assert step(b, 1) == [4]


def test_lead_reappearing_immediately_stops_recovery():
  b = slowed()
  assert step(b, 200)[-1] == 4
  b.helper.d_rel, b.helper.v_rel, b.helper.v_lead_kph, b.helper.lead_prob = 25, 0, 40, 0.99
  assert step(b, 300) == [0] * 300
  b.helper.d_rel = 0
  assert step(b, 199) == [0] * 199
  assert step(b, 1) == [4]


def test_lead_then_curve_keeps_original_setting():
  b = slowed()
  b.helper.desiredSource, b.helper.desiredSpeed, b.helper.vTurnSpeed = 'model', 30, 25
  assert step(b, 1) == [3]
  assert b.helper._ray_stock_restore_kph == pytest.approx(60)
  b.cs.cruiseState.speed = 30 / 3.6
  assert 4 not in step(b, 300)
  b.helper.desiredSource = ''
  assert step(b, 199) == [0] * 199
  assert step(b, 1) == [4]


@pytest.mark.parametrize('button', ['mainCruise', 'cancel', 'accelCruise', 'decelCruise'])
@pytest.mark.parametrize('pressed', [False, True])
def test_manual_button_discards_remembered_speed(button, pressed):
  b = slowed()
  step(b, 199)
  b.tick(getattr(upper.BUTTONS, button), pressed)
  assert b.helper._ray_stock_restore_kph == 0
  assert 4 not in step(b, 300)


@pytest.mark.parametrize('signal', ['gasPressed', 'brakePressed', 'brakeHoldActive', 'park', 'paused', 'disabled', 'invalid', 'missing_speed'])
def test_interruption_cannot_recover_from_stale_intent(signal):
  b = slowed()
  if signal in ('gasPressed', 'brakePressed', 'brakeHoldActive'):
    setattr(b.cs, signal, True)
  elif signal == 'park':
    b.cs.gearShifter = 'park'
  elif signal == 'paused':
    b.cs.gearStep = 3
  elif signal == 'disabled':
    b.helper.rayVisionCruiseControl = 0
  elif signal == 'invalid':
    b.cs.canValid = False
  else:
    b.cs.cruiseState.speed = 0
  assert step(b, 1) == [0]
  assert b.helper._ray_stock_restore_kph == 0
  b.cs.gearShifter, b.cs.gearStep, b.cs.canValid = 'drive', 7, True
  b.cs.gasPressed = b.cs.brakePressed = b.cs.brakeHoldActive = False
  b.cs.cruiseState.speed = 40 / 3.6
  b.helper.rayVisionCruiseControl = 2
  assert step(b, 300) == [0] * 300


def test_controller_recovers_one_kph_at_a_time_to_original_setting():
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.cc.cruiseControl.resume = True
  b.cs.out.gearStep, b.cs.out.activateCruise = 7, 4
  b.cs.out.cruiseState.speed = 40 / 3.6
  b.cc.hudControl.setSpeed = 43 / 3.6
  presses = []
  for _ in range(250):
    button = b.h.make_spam_button(b.cc, b.cs)
    if button:
      assert button == lower.Buttons.RES_ACCEL
      presses.append(b.h.frame)
      b.cs.out.cruiseState.speed += 1 / 3.6
    b.h.frame += 1
  assert len(presses) == 3
  assert min(y - x for x, y in zip(presses, presses[1:])) >= 50
  assert b.cs.out.cruiseState.speed * 3.6 == pytest.approx(43)


@pytest.mark.parametrize('gear_step', [0, 3, 5, 6])
def test_recovery_never_reenables_inactive_or_unknown_stock_cruise(gear_step):
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.cs.out.gearStep, b.cs.out.activateCruise = gear_step, 4
  b.cs.out.cruiseState.speed = 40 / 3.6
  b.cc.hudControl.setSpeed = 60 / 3.6
  for _ in range(300):
    assert b.h.make_spam_button(b.cc, b.cs) == 0
    b.h.frame += 1


def test_direction_change_cannot_acknowledge_the_wrong_pulse():
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.cc.cruiseControl.resume = True
  b.cs.out.gearStep, b.cs.out.activateCruise = 7, 3
  b.cs.out.vEgo = 50 / 3.6
  b.cs.out.cruiseState.speed = 60 / 3.6
  assert b.h.make_spam_button(b.cc, b.cs) == lower.Buttons.SET_DECEL
  b.h.frame += 1
  b.cs.out.activateCruise = 4
  b.cc.hudControl.setSpeed = 60 / 3.6
  assert b.h.make_spam_button(b.cc, b.cs) == 0
  assert b.h.ray_ev_set_adjust_wait_until > b.h.frame
  b.cs.out.cruiseState.speed = 59 / 3.6
  b.h.frame += 1
  assert b.h.make_spam_button(b.cc, b.cs) == 0
  assert b.h.ray_ev_set_adjust_wait_until == -1


@pytest.mark.parametrize('button', ['accelCruise', 'decelCruise'])
def test_manual_hold_cannot_relatch_old_speed_while_lead_remains(button):
  b = slower_lead()
  b.tick(getattr(upper.BUTTONS, button), True)
  for _ in range(200):
    b.tick()
    assert b.helper._ray_stock_restore_kph == 0
    assert b.helper._activate_cruise != 4
  b.tick(getattr(upper.BUTTONS, button), False)
  for _ in range(99):
    b.tick()
    assert b.helper._ray_stock_restore_kph == 0
