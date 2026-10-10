"""Regressions for missing Ray stock setpoint feedback, without vehicle I/O."""

import test_ray_controller_cancel as lower
import test_ray_manual_off as upper


class TestRayUnknownSetpoint:
  def lower_fixture(self, stock_speed=0.0, fingerprint='KIA_RAY_EV'):
    b = lower.TestRayControllerCancel()
    b.setup_method()
    b.h.CP.carFingerprint = fingerprint
    b.h.ray_ev_activate_retry = 0
    b.h.ray_ev_cruise_enabled_last = True
    b.h.ray_ev_estimated_cruise_speed = 35
    b.cs.out.gearStep = 7
    b.cs.out.cruiseState.speed = stock_speed / 3.6
    b.cs.out.cruiseState.enabled = stock_speed > 0
    b.cs.out.vEgo = 35 / 3.6
    b.cc.hudControl.setSpeed = 40 / 3.6
    return b

  def run_lower(self, b, count=250):
    sent = []
    for _ in range(count):
      sent.append(b.h.make_spam_button(b.cc, b.cs))
      b.h.frame += 1
    return sent

  def test_no_blind_increase_after_resume_with_missing_setpoint(self):
    b = self.lower_fixture()
    assert (lower.Buttons.RES_ACCEL) not in (self.run_lower(b))

  def test_no_blind_decrease_from_guessed_setpoint(self):
    b = self.lower_fixture()
    b.h.ray_ev_estimated_cruise_speed = 48
    b.cs.out.vEgo = 45 / 3.6
    assert (lower.Buttons.SET_DECEL) not in (self.run_lower(b))

  def test_delayed_gear_feedback_does_not_create_speed_increase(self):
    b = self.lower_fixture()
    b.h.ray_ev_pause_resume_frame = b.h.frame
    b.cs.out.gearStep = 3
    b.cs.out.activateCruise = 2
    assert (b.h.make_spam_button(b.cc, b.cs)) == (0)
    b.cs.out.activateCruise = 0
    self.run_lower(b, 15)
    b.cs.out.gearStep = 7
    assert (lower.Buttons.RES_ACCEL) not in (self.run_lower(b))

  def test_measured_setpoint_stays_under_stock_control(self):
    for current in (35, 45):
      b = self.lower_fixture(current)
      assert set(self.run_lower(b)) == {0}

  def test_other_car_keeps_measured_speed_matching(self):
    b = self.lower_fixture(35, fingerprint='OTHER')
    assert (lower.Buttons.RES_ACCEL) in (self.run_lower(b))

  def test_missing_setpoint_never_requests_a_virtual_button(self):
    b = upper.TestRayManualOff()
    b.setup_method()
    h = b.helper
    b.cs.cruiseState.speed = 0.0
    b.cs.cruiseState.enabled = False
    h.d_rel, h.v_rel, h.v_lead_kph, h.lead_prob = 10.0, -6.0, 20.0, 0.99
    h.v_ego_kph_set = 47.0
    b.cs.vEgo = 44.36 / 3.6
    h._update_ray_ipedal_assist(b.cs, b.cc, 40.0)
    assert h._activate_cruise == 0
    assert not h._cruise_cancel_state
    b.cc.enabled = False
    h.v_ego_kph_set = 38.0
    b.cs.vEgo = 35 / 3.6
    for _ in range(1200):
      h._activate_cruise = 0
      h._update_ray_ipedal_assist(b.cs, b.cc, 40.0)
      assert (h._activate_cruise) == (0)

  def test_feedback_loss_never_requests_automatic_resume(self):
    b = upper.TestRayManualOff()
    b.setup_method()
    h = b.helper
    b.cs.cruiseState.speed = 0.0
    b.cc.enabled = False
    h.v_ego_kph_set = 38.0
    h._ray_ipedal_active = True
    h._ray_ipedal_timer = 119
    h._update_ray_ipedal_assist(b.cs, b.cc, 40.0)
    assert h._activate_cruise == 0
    assert not h._cruise_cancel_state
    assert not (h._ray_ipedal_active)

  def test_explicit_pause_resume_can_clear_the_hold(self):
    b = upper.TestRayManualOff()
    b.setup_method()
    b.cs.cruiseState.speed = 0.0
    b.helper._cruise_cancel_state = True
    b.cc.enabled = False
    b.tick(upper.BUTTONS.cancel, True)
    for _ in range(10):
      b.tick()
    request, _ = b.tick(upper.BUTTONS.cancel, False)
    assert (request) == (2)
    assert not (b.helper._cruise_cancel_state)
