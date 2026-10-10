"""Driver-reported Ray MAIN / pause / resume / SET contract, without CAN I/O."""

from types import SimpleNamespace as NS

import pytest
import test_ray_cancel_power as event_harness
import test_ray_controller_cancel as lower
from test_ray_manual_off import BUTTONS, TestRayManualOff as ButtonHarness


class Sequence:
  def __init__(self, armed=False, enabled=False):
    self.b = ButtonHarness()
    self.b.setup_method()
    self.h = self.b.helper
    self.h._ray_ev_main_on = armed
    self.h._lat_enabled = False
    self.b.cc.enabled = enabled
    self.speed = 40.0
    self.h.v_ego_kph_set = 32.0

  def tick(self, button=None, pressed=False):
    self.b.cs.buttonEvents = [] if button is None else [NS(type=button, pressed=pressed)]
    self.h._activate_cruise = 0
    self.h.v_cruise_kph = self.speed
    self.speed = self.h._update_cruise_buttons(self.b.cs, self.b.cc, self.speed)
    if self.h._activate_cruise:
      self.b.cc.enabled = self.h._activate_cruise > 0
    return self.h._activate_cruise

  def press(self, button, hold=5):
    requests = [self.tick(button, True)]
    requests.extend(self.tick() for _ in range(hold))
    requests.append(self.tick(button, False))
    return requests


@pytest.mark.parametrize('button', [BUTTONS.accelCruise, BUTTONS.decelCruise, BUTTONS.cancel])
@pytest.mark.parametrize('hold', [5, 150])
def test_cold_start_requires_main_even_for_long_press(button, hold):
  s = Sequence()
  assert all(request <= 0 for request in s.press(button, hold))
  assert not s.h._ray_ev_main_on
  assert not s.b.cc.enabled
  assert s.speed == 40.0


def test_main_sets_current_pause_resume_restores_previous():
  s = Sequence()
  s.h.v_ego_kph_set = 40.0
  assert s.press(BUTTONS.mainCruise)[-1] == 2
  assert s.speed == 40.0
  assert s.h._ray_ev_main_on
  assert all(request <= 0 for request in s.press(BUTTONS.cancel))
  s.h.v_ego_kph_set = 32.0
  for _ in range(200):
    assert s.tick() <= 0
  assert s.speed == 40.0
  assert s.press(BUTTONS.cancel)[-1] == 2
  assert s.speed == 40.0


@pytest.mark.parametrize('button', [BUTTONS.accelCruise, BUTTONS.decelCruise])
def test_both_rocker_directions_set_current_speed_after_pause(button):
  s = Sequence(armed=True)
  s.h._cruise_cancel_state = True
  s.h._v_cruise_kph_at_brake = 47.0
  assert s.press(button)[-1] == 2
  assert s.speed == 32.0
  assert s.h._v_cruise_kph_at_brake == 0


@pytest.mark.parametrize('button,expected', [(BUTTONS.accelCruise, 41.0), (BUTTONS.decelCruise, 39.0)])
def test_active_short_press_changes_one_kph_even_with_carrot_speed_mode(button, expected):
  s = Sequence(armed=True, enabled=True)
  s.h._cruise_button_mode = 2
  s.h._cruise_speed_unit_basic = 5
  s.h._v_cruise_kph_at_brake = 47.0
  assert s.press(button)[-1] == 0
  assert s.speed == expected


@pytest.mark.parametrize('button', [BUTTONS.accelCruise, BUTTONS.decelCruise, BUTTONS.cancel])
def test_main_off_while_paused_requires_main_again(button):
  s = Sequence(armed=True)
  assert s.press(BUTTONS.mainCruise)[-1] == -1
  assert not s.h._ray_ev_main_on
  assert all(request <= 0 for request in s.press(button))
  assert not s.b.cc.enabled


def test_park_main_does_not_arm_later_resume():
  s = Sequence()
  s.b.cs.gearShifter = 'park'
  assert all(request <= 0 for request in s.press(BUTTONS.mainCruise))
  s.b.cs.gearShifter = 'drive'
  assert all(request <= 0 for request in s.press(BUTTONS.accelCruise))
  assert not s.h._ray_ev_main_on


@pytest.mark.parametrize('button', [BUTTONS.accelCruise, BUTTONS.decelCruise, BUTTONS.cancel])
def test_brake_blocks_resume_and_preserves_setpoint(button):
  s = Sequence(armed=True)
  s.b.cs.brakePressed = True
  assert all(request <= 0 for request in s.press(button))
  s.b.cs.brakePressed = False
  s.h.autoCruiseControl = 2
  s.h._gas_tok = True
  s.h.v_ego_kph_set = 47.0
  for _ in range(200):
    assert s.tick() <= 0
  assert s.speed == 40.0


@pytest.mark.parametrize('button', [BUTTONS.accelCruise, BUTTONS.decelCruise])
def test_raw_interface_enable_does_not_bypass_ray_main_gate(button):
  e = event_harness.TestRayCancelPower()
  e.setup_method()
  e.cs.gearShifter = 'drive'
  e.cs.buttonEnable = True
  assert 'buttonEnable' not in e.evaluate(button, False, activation=0)
  assert 'buttonEnable' in e.evaluate(button, False, activation=2)
  e.helper.CP.carFingerprint = 'OTHER_CAR'
  assert 'buttonEnable' in e.evaluate(button, False, activation=0)


def test_lfa_toggle_does_not_request_longitudinal_engagement():
  s = Sequence()
  assert s.press(BUTTONS.lfaButton)[-1] == 0
  assert s.h._lat_enabled
  assert not s.h._ray_ev_main_on
  assert not s.b.cc.enabled


@pytest.mark.parametrize('button', ['mainCruise', 'accelCruise', 'decelCruise', 'cancel'])
def test_lower_controller_does_not_echo_physical_engagement(button):
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.cs.out.buttonEvents = [NS(type=button, pressed=False)]
  b.cs.out.activateCruise = 2
  assert b.h.make_spam_button(b.cc, b.cs) == 0
  assert b.h.ray_ev_activate_retry == 0
  b.cs.out.buttonEvents = []
  b.cs.out.activateCruise = 0
  for _ in range(40):
    b.h.frame += 1
    assert b.h.make_spam_button(b.cc, b.cs) == 0
