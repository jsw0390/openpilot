"""Stock speed ownership, lead clearance and toggle acknowledgement regressions."""

import ast
import math
from types import SimpleNamespace as NS

import pytest

import test_ray_controller_cancel as lower
import test_ray_manual_off as upper


def paused_lead():
  b = upper.TestRayManualOff()
  b.setup_method()
  h = b.helper
  h.v_ego_kph_set = 50
  h.d_rel, h.v_rel, h.v_lead_kph, h.lead_prob = 25, -3, 38, 0.99
  b.cs.vEgo = 50 / 3.6
  b.cs.cruiseState.speed = 60 / 3.6
  b.cs.cruiseState.enabled = True
  h._update_ray_ipedal_assist(b.cs, b.cc, 60)
  assert h._activate_cruise == -2
  b.cs.cruiseState.enabled = b.cc.enabled = False
  return b


def step(b, count):
  requests = []
  for _ in range(count):
    b.helper._activate_cruise = 0
    b.helper._update_ray_ipedal_assist(b.cs, b.cc, 60)
    requests.append(b.helper._activate_cruise)
  return requests


@pytest.mark.parametrize('speed', [35, 47, 60, 100])
@pytest.mark.parametrize('source', ['', 'road', 'atc', 'vturn', 'model', 'route'])
def test_no_lead_never_regulates_stock_speed(speed, source):
  b = upper.TestRayManualOff()
  b.setup_method()
  b.helper.v_ego_kph_set = speed
  b.helper.desiredSource, b.helper.desiredSpeed, b.helper.vTurnSpeed = source, 30, 20
  b.cs.vEgo = speed / 3.6
  b.cs.cruiseState.speed = 0
  for _ in range(1000):
    assert b.helper._update_ray_ipedal_assist(b.cs, b.cc, 40) == 40
    assert b.helper._activate_cruise == 0
  assert not b.helper._cruise_cancel_state


def test_fresh_lead_clear_must_persist_then_resume_once():
  b = paused_lead()
  b.helper.d_rel = 0
  assert step(b, 199) == [0] * 199
  assert step(b, 1) == [2]
  assert step(b, 1000) == [0] * 1000


def test_reappearing_lead_restarts_clearance_wait():
  b = paused_lead()
  b.helper.d_rel = 0
  assert step(b, 199) == [0] * 199
  b.helper.d_rel = 20
  assert step(b, 1) == [0]
  b.helper.d_rel = 0
  assert step(b, 199) == [0] * 199
  assert step(b, 1) == [2]


def test_slow_lead_blocks_resume_even_after_ego_matches_lead_speed():
  b = paused_lead()
  b.helper.v_ego_kph_set = b.helper.v_lead_kph = 38
  b.helper.v_rel = 0
  b.cs.vEgo = 38 / 3.6
  assert step(b, 3000) == [0] * 3000
  b.helper.v_lead_kph = 65
  assert step(b, 200)[-1] == 2


def test_uncertain_track_never_counts_as_clear():
  b = paused_lead()
  b.helper.lead_prob = 0.5
  assert step(b, 1000) == [0] * 1000


def test_stale_feed_and_feedback_loss_require_driver_resume():
  for missing in ('lead', 'setpoint'):
    b = paused_lead()
    b.helper.d_rel = 0
    step(b, 199)
    if missing == 'lead':
      b.helper._ray_lead_data_valid = False
    else:
      b.cs.cruiseState.speed = 0
    assert step(b, 1) == [-1]
    b.helper._ray_lead_data_valid = True
    b.cs.cruiseState.speed = 60 / 3.6
    assert step(b, 1000) == [0] * 1000
    assert b.helper._cruise_cancel_state


def test_unexpectedly_high_stock_setpoint_is_never_restored():
  b = paused_lead()
  b.cs.cruiseState.speed = 67 / 3.6
  b.helper.d_rel = 0
  assert step(b, 1) == [-1]
  assert step(b, 1000) == [0] * 1000


def test_generic_automatic_speed_rules_do_not_rewrite_ray_driver_setpoint():
  b = upper.TestRayManualOff()
  b.setup_method()
  b.helper._gas_tok = True
  b.helper.autoSpeedUptoRoadSpeedLimit = 2
  b.helper.nRoadLimitSpeed = 100
  b.helper.applyModelSpeed = 1
  assert b.helper._update_cruise_state(b.cs, b.cc, 40) == 40


class TestStockToggle:
  def setup_method(self):
    self.b = lower.TestRayControllerCancel()
    self.b.setup_method()

  def run(self, count=1):
    values = []
    for _ in range(count):
      values.append(self.b.h.make_spam_button(self.b.cc, self.b.cs))
      self.b.h.frame += 1
    return values

  def test_no_resume_without_explicit_request_despite_old_enabled_state(self):
    assert self.run(1000) == [0] * 1000

  def test_resume_is_one_pulse_even_with_delayed_stock_acknowledgement(self):
    self.b.cs.out.activateCruise = 2
    assert self.run(1000) == [lower.Buttons.CANCEL] + [0] * 999

  def test_pause_is_one_pulse_and_never_repeated_against_stale_feedback(self):
    self.b.cc.enabled = False
    self.b.cs.out.gearStep = 7
    self.b.cs.out.activateCruise = -1
    assert self.run(1000) == [lower.Buttons.CANCEL] + [0] * 999

  def test_resume_waits_for_controls_ack_then_expires(self):
    self.b.cc.enabled = False
    self.b.cs.out.activateCruise = 2
    assert self.run() == [0]
    self.b.cs.out.activateCruise = 0
    self.b.cc.enabled = True
    assert self.run() == [lower.Buttons.CANCEL]
    self.setup_method()
    self.b.cc.enabled = False
    self.b.cs.out.activateCruise = 2
    self.run(31)
    self.b.cc.enabled = True
    assert self.run(10) == [0] * 10

  @pytest.mark.parametrize('pedal', ['brakePressed', 'brakeHoldActive', 'gasPressed'])
  def test_pedal_clears_queued_resume(self, pedal):
    self.b.cc.enabled = False
    self.b.cs.out.activateCruise = 2
    self.run()
    self.b.cs.out.activateCruise = 0
    setattr(self.b.cs.out, pedal, True)
    assert self.run() == [0]
    setattr(self.b.cs.out, pedal, False)
    self.b.cc.enabled = True
    assert self.run(100) == [0] * 100

  @pytest.mark.parametrize('gear', ['park', 'reverse', 'neutral', 'unknown'])
  def test_nondriving_gears_block_commands(self, gear):
    self.b.cs.out.gearShifter = gear
    self.b.cs.out.activateCruise = 2
    assert self.run(100) == [0] * 100


@pytest.mark.parametrize('age,valid,distance,expected', [
  (0.1, True, 20, True), (0.3, True, 20, False), (-1, True, 20, False),
  (0.1, False, 20, False), (0.1, True, math.nan, False), (0.1, True, -1, False),
])
def test_radar_freshness_and_finite_inputs(age, valid, distance, expected):
  tree = ast.parse((upper.ROOT / 'selfdrive/car/cruise.py').read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'VCruiseCarrot')
  method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'update_v_cruise')
  start = next(i for i, n in enumerate(method.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'self._ray_lead_data_valid')

  class Inputs(dict):
    alive = {'radarState': True}
    recv_time = {'radarState': 10 - age}

    def all_checks(self, services):
      return valid

  h = NS()
  inputs = Inputs(radarState=NS(leadOne=NS(status=True, dRel=distance, vRel=-1, vLeadK=10, radar=False, modelProb=0.9)))
  code = compile(ast.fix_missing_locations(ast.Module(body=method.body[start:start + 2], type_ignores=[])), 'radar_inputs', 'exec')
  exec(code, dict(self=h, sm=inputs, math=math, time=NS(monotonic=lambda: 10), CV=NS(MS_TO_KPH=3.6)))
  assert bool(h._ray_lead_data_valid) == expected
