"""Cancel priority and pause hysteresis regressions using actual method bodies."""

import test_ray_manual_off as button_harness
import test_ray_cancel_power as event_harness
from test_ray_manual_off import BUTTONS


class TestRayCancelPriority:
  def setup_method(self):
    self.b = button_harness.TestRayManualOff()
    self.b.setup_method()
    self.e = event_harness.TestRayCancelPower()
    self.e.setup_method()
    self.e.cs.gearShifter = 'drive'
    self.e.cs.standstill = False
    self.e.cs.vEgo = self.b.cs.vEgo

  def tick(self, button=None, pressed=False):
    request, target = self.b.tick(button, pressed)
    names = self.e.evaluate(button, pressed, request)
    self.e.previous.activateCruise = request
    return request, names

  def arm_resume(self):
    self.b.helper._ray_ipedal_active = True
    self.b.helper._ray_ipedal_timer = 119
    self.b.cs.cruiseState.enabled = False

  def test_press_wins_over_automatic_resume_boundary(self):
    self.arm_resume()
    request, names = self.tick(BUTTONS.cancel, True)
    assert (request) < (0)
    assert ('buttonCancel') in (names)
    assert ('buttonEnable') not in (names)

  def test_held_cancel_blocks_new_automatic_and_remote_requests(self):
    self.tick(BUTTONS.cancel, True)
    self.b.cc.enabled = False
    for _ in range(150):
      self.arm_resume()
      self.b.helper._activate_cruise = 2
      self.b.helper.carrot_cmd_index += 1
      self.b.helper.carrot_cmd = 'CRUISE'
      self.b.helper.carrot_arg = 'ON'
      request, names = self.tick()
      assert (request) < (0)
      assert ('buttonEnable') not in (names)

  def test_same_release_stays_off_then_distinct_press_resumes(self):
    self.tick(BUTTONS.cancel, True)
    self.b.cc.enabled = False
    for _ in range(20):
      self.tick()
    request, names = self.tick(BUTTONS.cancel, False)
    assert (request) <= (0)
    assert ('buttonEnable') not in (names)
    for _ in range(250):
      request, names = self.tick()
      assert (request) <= (0)
      assert ('buttonEnable') not in (names)
    self.tick(BUTTONS.cancel, True)
    for _ in range(20):
      self.tick()
    request, names = self.tick(BUTTONS.cancel, False)
    assert (request) == (2)
    assert ('buttonEnable') in (names)

  def test_long_hold_from_disabled_does_not_resume_on_release(self):
    self.b.cc.enabled = False
    self.tick(BUTTONS.cancel, True)
    for _ in range(150):
      request, names = self.tick()
      assert (request) <= (0)
      assert ('buttonEnable') not in (names)
    request, names = self.tick(BUTTONS.cancel, False)
    assert (request) <= (0)
    assert ('buttonEnable') not in (names)

  def test_event_layer_never_enables_on_cancel_press(self):
    names = self.e.evaluate('cancel', True, activation=2)
    assert ('buttonCancel') in (names)
    assert ('buttonEnable') not in (names)

  def test_park_still_blocks_distinct_resume(self):
    self.b.cc.enabled = False
    self.b.cs.gearShifter = self.e.cs.gearShifter = 'park'
    self.b.cs.vEgo = self.e.cs.vEgo = 0.0
    self.e.cs.standstill = True
    self.tick(BUTTONS.cancel, True)
    for _ in range(20):
      self.tick()
    _, names = self.tick(BUTTONS.cancel, False)
    assert ('wrongGear') in (names)
    assert ('buttonEnable') not in (names)
    assert (self.e.writes) == ([])


class TestRayPauseHysteresis:
  def setup_method(self):
    self.b = button_harness.TestRayManualOff()
    self.b.setup_method()
    self.h = self.b.helper
    self.h.v_ego_kph_set = 50.0
    self.h.d_rel = 30.0
    self.h.v_rel = -0.6
    self.h.v_lead_kph = 47.84
    self.h.lead_prob = 0.9
    self.b.cs.vEgo = 50 / 3.6
    self.b.cs.cruiseState.enabled = True
    self.b.cs.cruiseState.speed = 60 / 3.6

  def run_frames(self, count=1000, target=60.0):
    requests = []
    for frame in range(count):
      self.h._activate_cruise = 0
      self.h._update_ray_ipedal_assist(self.b.cs, self.b.cc, target)
      if self.h._activate_cruise:
        requests.append((frame, self.h._activate_cruise))
        self.b.cc.enabled = self.h._activate_cruise > 0
        self.b.cs.cruiseState.enabled = self.b.cc.enabled
    return requests

  def test_lead_target_above_ego_does_not_start_pause_cycle(self):
    assert (self.run_frames()) == ([])

  def test_slower_lead_requests_stock_setpoint_reduction(self):
    self.h.v_lead_kph = 40.0
    requests = self.run_frames()
    assert requests[0] == (0, 3)
    assert {request for _, request in requests} == {3}

  def test_slowdown_alone_never_requests_an_increase(self):
    self.h.v_lead_kph = 40.0
    assert {request for _, request in self.run_frames(121)} == {3}
    self.h.v_ego_kph_set = 46.0
    self.b.cs.vEgo = 46 / 3.6
    assert self.run_frames(1000) == []

  def test_curve_without_lead_does_not_pause_stock_cruise(self):
    self.h.d_rel = 0.0
    self.h.desiredSource = 'atc'
    self.h.desiredSpeed = 45.0
    assert self.run_frames(1200) == []

  def test_overspeed_without_lead_does_not_pause_stock_cruise(self):
    self.h.d_rel = 0.0
    self.h.rayVisionIPedalResumeMargin = 10
    assert self.run_frames(1200, target=42.0) == []

  def test_brake_gas_park_and_low_speed_clear_pending_resume(self):
    for signal in ('brake', 'gas', 'park', 'slow'):
      self.setup_method()
      self.b.cc.enabled = False
      self.b.cs.cruiseState.enabled = False
      self.h._ray_ipedal_active = True
      self.h._ray_ipedal_timer = 119
      if signal == 'brake':
        self.b.cs.brakePressed = True
      if signal == 'gas':
        self.b.cs.gasPressed = True
      if signal == 'park':
        self.b.cs.gearShifter = 'park'
      if signal == 'slow':
        self.h.v_ego_kph_set = 0.0
      assert (self.run_frames(300)) == ([])
      assert not (self.h._ray_ipedal_active)

  def test_vision_assist_disabled_does_not_pause(self):
    self.h.rayVisionCruiseControl = 0
    self.h.v_lead_kph = 0.0
    assert (self.run_frames()) == ([])
