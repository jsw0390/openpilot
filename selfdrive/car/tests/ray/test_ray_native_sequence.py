"""Device-only offline tests of full cruise/event classes and real StateMachine.

All parameters are in memory. No messaging sockets, controller, or CAN sender is
created. These tests do not validate actuator execution or vehicle dynamics.
"""

import ast
import contextlib
import importlib.util
import io
import sys
import time
import pytest
from pathlib import Path
from types import SimpleNamespace as NS

if sys.platform != 'linux':
  pytest.skip('Requires the comma device native runtime', allow_module_level=True)

from cereal import car, log
from opendbc.car.interfaces import CarStateBase
from openpilot.selfdrive.selfdrived.events import EventName, ET
from openpilot.selfdrive.selfdrived.state import StateMachine
import openpilot.selfdrive.selfdrived.selfdrived as selfdrived_source

ROOT = Path(__file__).resolve().parents[4]


def load(name, relative):
  spec = importlib.util.spec_from_file_location(name, ROOT / relative)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class MemoryParams:
  values = {
    'AutoEngage': 2,
    'AutoCruiseControl': 0,
    'CruiseSpeedUnit': 10,
    'CruiseSpeedUnitBasic': 1,
    'CruiseButtonLongDelay': 40,
    'CruiseButtonMode': 0,
    'RayVisionCruiseControl': 2,
    'RayVisionIPedalAssist': 2,
    'RayVisionIPedalSpeedDelta': 7,
    'RayVisionIPedalResumeMargin': 2,
    'RayVisionCruiseLeadProb': 85,
    'RayVisionCruiseTFollowAdd': 35,
    'AutoGasTokSpeed': 30,
  }

  def __init__(self, *args):
    self.values = dict(type(self).values)

  def get_int(self, key):
    return int(self.values.get(key, 0))

  def get_float(self, key):
    return float(self.values.get(key, 0))

  def get_bool(self, key):
    return bool(self.values.get(key, 0))

  def put_bool_nonblocking(self, key, value):
    self.values[key] = value

  def put_bool(self, key, value):
    self.values[key] = value


cruise = load('ray_candidate_cruise', 'selfdrive/car/cruise.py')
common = load('ray_candidate_events', 'selfdrive/car/car_specific.py')
controller = load('ray_candidate_controller', 'opendbc_repo/opendbc/car/hyundai/carcontroller.py')
cruise.Params = common.Params = MemoryParams

# Execute the installed selfdrived pedal-event conditional, without starting
# Selfdrive or importing any of its I/O into the test object.
tree = ast.parse(Path(selfdrived_source.__file__).read_text())
pedal_if = next(
  node
  for node in ast.walk(tree)
  if isinstance(node, ast.If)
  and any(
    isinstance(statement, ast.Expr)
    and isinstance(statement.value, ast.Call)
    and isinstance(statement.value.func, ast.Attribute)
    and statement.value.func.attr == 'add'
    and any(isinstance(arg, ast.Attribute) and arg.attr == 'pedalPressed' for arg in statement.value.args)
    for statement in node.body
  )
)
pedal_code = compile(ast.fix_missing_locations(ast.Module(body=[pedal_if], type_ignores=[])), selfdrived_source.__file__, 'exec')


class NativeHarness:
  def __init__(self, enabled=True, gear='drive'):
    cp = car.CarParams.new_message(carFingerprint='KIA_RAY_EV', brand='hyundai', openpilotLongitudinalControl=True, pcmCruise=False)
    self.h = cruise.VCruiseCarrot(cp)
    self.h._ray_ev_main_on = enabled
    self.h.v_cruise_kph = 31.0
    self.h.cruise_state_available_last = True
    self.events = common.CarSpecificEvents(cp)
    self.cs = car.CarState.new_message(vEgo=32 / 3.6, vEgoCluster=32 / 3.6, gearShifter=gear, standstill=False, canValid=True)
    self.cs.cruiseState.available = True
    self.cs.gearStep = 7 if enabled else 3
    self.cs.cruiseState.speed = 31 / 3.6 if enabled else 0
    self.prev = car.CarState.new_message(**self.cs.to_dict())
    self.cc = car.CarControl.new_message(enabled=enabled)
    self.sm = StateMachine()
    self.sm.state = log.SelfdriveState.OpenpilotState.enabled if enabled else log.SelfdriveState.OpenpilotState.disabled

  def tick(self, button=None, pressed=False):
    self.cs.buttonEvents = [] if button is None else [{'type': button, 'pressed': pressed}]
    return self.update()

  def update(self):
    class Inputs(dict):
      alive = dict.fromkeys(['carrotMan', 'longitudinalPlan', 'radarState', 'drivingModelData', 'modelV2'], False)
      alive['carControl'] = True
      recv_time = dict.fromkeys(['radarState', 'carrotMan', 'modelV2', 'carControl'], time.monotonic())

      def all_checks(self, services):
        return all(self.alive.get(service, False) for service in services)

    inputs = Inputs(carControl=self.cc)
    if hasattr(self, 'lead'):
      inputs.alive = dict(inputs.alive, radarState=True)
      inputs['radarState'] = NS(leadOne=self.lead, leadTwo=NS(status=False))
    if hasattr(self, 'model'):
      inputs.alive = dict(inputs.alive, modelV2=True)
      inputs['modelV2'] = self.model
    if hasattr(self, 'carrot'):
      inputs.alive = dict(inputs.alive, carrotMan=True)
      inputs['carrotMan'] = self.carrot

    # Include the generic interface's RES/SET edge, which previously bypassed
    # the Ray-specific activation check in CarSpecificEvents.
    self.cs.buttonEnable = CarStateBase.update_button_enable(NS(CP=self.h.CP), self.cs.buttonEvents)
    self.h.update_v_cruise(self.cs, inputs, True)
    self.cs.activateCruise = self.h._activate_cruise
    self.cs.latEnabled = self.h._lat_enabled
    self.cs.vCruise = float(self.h.v_cruise_kph)
    events = self.events.update(self.cs, self.prev, self.cc)
    scope = {'CS': self.cs, 'self': NS(CS_prev=self.prev, disengage_on_accelerator=True, events=events), 'EventName': EventName}
    exec(pedal_code, scope)
    with contextlib.redirect_stdout(io.StringIO()):
      enabled, active = self.sm.update(events)
    self.cc.enabled = enabled
    self.prev = car.CarState.new_message(**self.cs.to_dict())
    return enabled, active, events, self.h._activate_cruise


class TestNativeRaySequence:
  @pytest.mark.parametrize('button', ['accelCruise', 'decelCruise', 'cancel'])
  def test_cold_start_cannot_enable_without_main(self, button):
    h = NativeHarness(False)
    h.tick(button, True)
    for _ in range(10):
      h.tick()
    enabled, _, events, request = h.tick(button, False)
    assert not enabled
    assert request <= 0
    assert not events.contains(ET.ENABLE)

  @pytest.mark.parametrize('button,expected', [('cancel', 40.0), ('accelCruise', 32.0), ('decelCruise', 32.0)])
  @pytest.mark.parametrize('pause', ['brake', 'cancel'])
  def test_main_pause_and_stock_resume_speed(self, button, expected, pause):
    h = NativeHarness(False)
    h.cs.vEgo = h.cs.vEgoCluster = 40 / 3.6
    h.cs.cruiseState.speed = 40 / 3.6
    h.tick('mainCruise', True)
    assert h.tick('mainCruise', False)[0]
    assert h.h.v_cruise_kph == 40.0
    h.cs.gearStep = 7
    if pause == 'brake':
      h.cs.brakePressed = True
      assert not h.tick()[0]
      h.cs.brakePressed = False
    else:
      assert not h.tick('cancel', True)[0]
      assert not h.tick('cancel', False)[0]
    h.cs.gearStep = 3
    h.cs.cruiseState.speed = 0
    h.cs.vEgo = h.cs.vEgoCluster = 32 / 3.6
    for _ in range(100):
      assert not h.tick()[0]
      assert h.h.v_cruise_kph == 40.0
    h.tick(button, True)
    assert h.tick(button, False)[0]
    assert h.h.v_cruise_kph == expected

  def test_boot_selects_lateral_and_first_lfa_press_turns_it_off(self):
    h = NativeHarness(False)
    h.h.cruise_state_available_last = False
    h.tick()
    assert h.h._lat_enabled
    h.tick('lfaButton', True)
    enabled, _, events, request = h.tick('lfaButton', False)
    assert not h.h._lat_enabled
    assert not enabled
    assert request == 0
    assert not events.contains(ET.ENABLE)

  def test_cluster_stock_setpoint_replaces_stale_comma_speed(self):
    h = NativeHarness()
    h.h.v_cruise_kph = 60.0
    h.cs.cruiseState.speed = 47.0 / 3.6
    h.tick()
    assert h.h.v_cruise_kph == 47.0

  def test_cluster_setpoint_is_the_base_for_one_kph_button_change(self):
    h = NativeHarness()
    h.h.v_cruise_kph = 60.0
    h.cs.cruiseState.speed = 47.0 / 3.6
    h.tick('accelCruise', True)
    h.tick()
    h.tick('accelCruise', False)
    assert h.h.v_cruise_kph == 48.0

  def test_missing_stock_setpoint_never_requests_automatic_buttons(self):
    h = NativeHarness()
    h.h.v_cruise_kph = 40.0
    h.lead = NS(status=True, dRel=10.0, vRel=-6.0, vLeadK=20 / 3.6, radar=False, modelProb=0.99)
    h.cs.cruiseState.speed = 0.0
    h.cs.gearStep = 7
    h.cs.vEgo = 44.36 / 3.6
    h.cs.vEgoCluster = 47.0 / 3.6
    for _ in range(1000):
      assert h.tick()[-1] == 0
    assert not h.h._ray_ipedal_active
    assert h.h._ray_stock_restore_kph == 0
    # Explicit driver pause/resume remains available with missing feedback.
    assert not h.tick('cancel', True)[0]
    assert not h.tick('cancel', False)[0]
    h.cs.gearStep = 3  # physical pause acknowledged before the next RES
    h.tick('cancel', True)
    for _ in range(10):
      h.tick()
    assert h.tick('cancel', False)[0]

  def test_native_controller_does_not_match_an_unknown_stock_setpoint(self):
    import test_ray_unknown_setpoint as cases

    fixture = cases.TestRayUnknownSetpoint().lower_fixture()
    ctrl = controller.CarController.__new__(controller.CarController)
    ctrl.__dict__.update(fixture.h.__dict__)
    for _ in range(250):
      assert (ctrl.make_spam_button(fixture.cc, fixture.cs)) not in ((1, 2))
      ctrl.frame += 1

  def test_native_controller_respects_cancel_before_enabled_feedback_catches_up(self):
    import test_ray_controller_cancel as controller_fixture

    fixture = controller_fixture.TestRayControllerCancel()
    fixture.setup_method()
    ctrl = controller.CarController.__new__(controller.CarController)
    ctrl.__dict__.update(fixture.h.__dict__)
    h = NativeHarness()
    h.h._ray_ipedal_active = True
    h.h._ray_ipedal_timer = 119
    assert not (h.tick('cancel', True)[0])
    fixture.cs.out = h.cs
    fixture.cs.cruise_buttons = [4]
    # The lower controller still has the previous enabled feedback here.
    assert fixture.cc.enabled
    assert (ctrl.make_spam_button(fixture.cc, fixture.cs)) == (0)
    assert (ctrl.ray_ev_activate_retry) == (0)

  def test_cancel_press_disables_and_same_release_stays_disabled(self):
    h = NativeHarness()
    assert not (h.tick('cancel', True)[0])
    h.cs.gearStep = 3
    h.cs.cruiseState.speed = 0
    for _ in range(20):
      assert not (h.tick()[0])
    assert not (h.tick('cancel', False)[0])
    for _ in range(250):
      assert not (h.tick()[0])
    assert not (h.tick('cancel', True)[0])
    for _ in range(20):
      assert not (h.tick()[0])
    assert h.tick('cancel', False)[0]

  def test_pending_resume_cannot_override_cancel_press(self):
    h = NativeHarness()
    h.h._ray_ipedal_active = True
    h.h._ray_ipedal_timer = 119
    enabled, _, events, request = h.tick('cancel', True)
    assert not (enabled)
    assert (request) < (0)
    assert events.contains(ET.USER_DISABLE)
    assert not (events.contains(ET.ENABLE))

  def test_park_blocks_pause_resume_main_res_and_set(self):
    for button in ('cancel', 'mainCruise', 'accelCruise', 'decelCruise'):
      h = NativeHarness(False, 'park')
      h.cs.vEgo = h.cs.vEgoCluster = 0.0
      h.cs.standstill = True
      assert not (h.tick(button, True)[0])
      for _ in range(20):
        assert not (h.tick()[0])
      assert not (h.tick(button, False)[0])
      assert not (h.events.params.get_bool('DoShutdown'))

  def test_brake_prevents_pending_resume_then_stays_disabled(self):
    h = NativeHarness()
    h.h._ray_ipedal_active = True
    h.h._ray_ipedal_timer = 119
    h.cs.brakePressed = True
    for _ in range(20):
      assert not (h.tick()[0])
    h.cs.brakePressed = False
    for _ in range(250):
      assert not (h.tick()[0])

  def test_main_off_disables_real_state_machine_and_stays_off(self):
    # Synthetic timing sequence; no user log or vehicle identifier is needed.
    h = NativeHarness()
    h.h._ray_ipedal_active = True
    h.h._ray_ipedal_timer = 119
    h.tick('mainCruise', True)
    for _ in range(5):
      h.tick()
    assert not h.tick('mainCruise', False)[0]
    for _ in range(250):
      assert not h.tick()[0]


@pytest.mark.parametrize('invalid_curve', [False, True])
def test_native_restore_requires_fresh_clear_inputs_and_preserves_original_limit(invalid_curve):
  h = NativeHarness()
  h.cs.gearStep = 7
  h.cs.vEgo = h.cs.vEgoCluster = 50 / 3.6
  h.cs.cruiseState.speed = 60 / 3.6
  h.lead = NS(status=True, dRel=25, vRel=-3, vLeadK=38 / 3.6, radar=False, modelProb=0.99)
  h.carrot = NS(nRoadLimitSpeed=0, desiredSpeed=250, vTurnSpeed=0, desiredSource='',
                carrotCmdIndex=0, carrotCmd='', carrotArg='')
  assert h.tick()[-1] == 3
  assert h.h._ray_stock_restore_kph == pytest.approx(60)
  h.cs.cruiseState.speed = 40 / 3.6
  h.cs.vEgo = h.cs.vEgoCluster = 40 / 3.6
  h.lead.status = False
  if invalid_curve:
    h.carrot.vTurnSpeed = float('nan')
    for _ in range(250):
      assert h.tick()[-1] == 0
    h.carrot.vTurnSpeed = 0
  for _ in range(199):
    assert h.tick()[-1] == 0
  assert h.tick()[-1] == 4
  assert h.h.v_cruise_kph == pytest.approx(60)
  h.cs.cruiseState.speed = 60 / 3.6
  assert h.tick()[-1] == 0
  assert h.h._ray_stock_restore_kph == 0


def lead_pause_harness():
  h = NativeHarness()
  h.h.params.values['RayVisionIPedalAssist'] = 3
  h.cs.vEgo = h.cs.vEgoCluster = 54 / 3.6
  h.cs.gearStep = 7
  h.cs.cruiseState.speed = 57 / 3.6
  h.lead = NS(status=True, dRel=32, vRel=-3.8, vLeadK=40 / 3.6, radar=False, modelProb=.99)
  h.model = NS(leadsV3=[NS(prob=.99)], action=NS(shouldStop=False, desiredVelocity=54 / 3.6))
  h.carrot = NS(nRoadLimitSpeed=80, desiredSpeed=250., vTurnSpeed=250., desiredSource='',
                carrotCmdIndex=0, carrotCmd='', carrotArg='')
  h.update()
  assert h.h._activate_cruise == -1
  assert not h.cc.enabled
  assert h.cs.latEnabled  # cruise cancellation must not deselect steering
  h.cs.gearStep = 3
  h.cs.cruiseState.speed = 0
  h.update()
  assert h.h._ray_lead_pause.phase == 'paused'
  return h


def test_native_owned_lead_pause_and_corroborated_resume_use_normal_entry_checks():
  h = lead_pause_harness()
  h.lead.status = False
  for _ in range(310):
    h.update()
    assert h.h._activate_cruise == 0  # radar loss while model still sees lead
  h.model.leadsV3[0].prob = .05
  for _ in range(299):
    h.update()
    assert not h.cc.enabled
  h.update()
  assert h.h._activate_cruise == 2
  assert h.cc.enabled
  assert h.cs.latEnabled
  h.cs.gearStep = 7
  h.cs.cruiseState.speed = 57 / 3.6
  h.update()
  assert h.h._ray_lead_pause.phase == 'idle'
  h.lead.status = True
  h.update()
  assert h.h._activate_cruise == -1
  assert not h.cc.enabled


@pytest.mark.parametrize('blocker', ['brake', 'gas', 'park', 'model_stop', 'blinker', 'low_speed', 'manual_cancel'])
def test_native_pause_never_resumes_after_driver_intervention_or_unclear_scene(blocker):
  h = lead_pause_harness()
  h.lead.status = False
  h.model.leadsV3[0].prob = .05
  if blocker == 'brake':
    h.cs.brakePressed = True
  elif blocker == 'gas':
    h.cs.gasPressed = True
  elif blocker == 'park':
    h.cs.gearShifter = 'park'
  elif blocker == 'model_stop':
    h.model.action.shouldStop = True
  elif blocker == 'blinker':
    h.cs.leftBlinker = True
  elif blocker == 'low_speed':
    h.cs.vEgo = h.cs.vEgoCluster = 25 / 3.6
  else:
    h.tick('cancel', True)
    h.tick('cancel', False)
  for _ in range(310):
    h.update()
    assert h.h._activate_cruise != 2


@pytest.mark.parametrize('button', ['cancel', 'accelCruise', 'decelCruise'])
def test_native_factory_feedback_reconciles_restart_then_closing_lead_pauses(button):
  h = NativeHarness(False)
  h.h.params.values['RayVisionIPedalAssist'] = 3
  h.h._cruise_cancel_state = True
  h.h._ray_ev_stock_sync_blocked = True
  h.cs.vEgo = 56.42 / 3.6
  h.cs.vEgoCluster = 59.375 / 3.6
  h.lead = NS(status=True, dRel=23.65, vRel=-.989, vLeadK=52.9 / 3.6, radar=False, modelProb=.9996)
  h.tick(button, True)
  assert h.tick(button, False)[-1] <= 0
  for _ in range(120):
    assert h.tick()[-1] == 0
  assert not h.h._ray_ev_main_on
  h.cs.gearStep = 7
  h.cs.cruiseState.speed = 60 / 3.6
  for _ in range(19):
    assert h.tick()[-1] == 0
  assert h.tick()[-1] == -1
  assert not h.cc.enabled
  assert h.cs.latEnabled
  h.cs.gearStep = 3
  h.cs.cruiseState.speed = 0
  assert h.tick()[-1] == 0
  assert h.h._ray_lead_pause.phase == 'paused'
