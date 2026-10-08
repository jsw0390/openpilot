"""Device-only offline tests of full cruise/event classes and real StateMachine.

All parameters are in memory. No messaging sockets, controller, or CAN sender is
created. These tests do not validate actuator execution or vehicle dynamics.
"""

import ast
import contextlib
import importlib.util
import io
import sys
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
    self.prev = car.CarState.new_message(**self.cs.to_dict())
    self.cc = car.CarControl.new_message(enabled=enabled)
    self.sm = StateMachine()
    self.sm.state = log.SelfdriveState.OpenpilotState.enabled if enabled else log.SelfdriveState.OpenpilotState.disabled

  def tick(self, button=None, pressed=False):
    self.cs.buttonEvents = [] if button is None else [{'type': button, 'pressed': pressed}]
    return self.update()

  def update(self):
    class Inputs(dict):
      alive = dict.fromkeys(['carrotMan', 'longitudinalPlan', 'radarState', 'drivingModelData'], False)

    # Include the generic interface's RES/SET edge, which previously bypassed
    # the Ray-specific activation check in CarSpecificEvents.
    self.cs.buttonEnable = CarStateBase.update_button_enable(NS(CP=self.h.CP), self.cs.buttonEvents)
    self.h.update_v_cruise(self.cs, Inputs(carControl=self.cc), True)
    self.cs.activateCruise = self.h._activate_cruise
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
    if pause == 'brake':
      h.cs.brakePressed = True
      assert not h.tick()[0]
      h.cs.brakePressed = False
    else:
      assert not h.tick('cancel', True)[0]
      assert not h.tick('cancel', False)[0]
    h.cs.vEgo = h.cs.vEgoCluster = 32 / 3.6
    for _ in range(100):
      assert not h.tick()[0]
      assert h.h.v_cruise_kph == 40.0
    h.tick(button, True)
    assert h.tick(button, False)[0]
    assert h.h.v_cruise_kph == expected

  def test_first_lfa_press_after_availability_does_not_enable_cruise(self):
    h = NativeHarness(False)
    h.h.cruise_state_available_last = False
    h.tick()
    assert not h.h._lat_enabled
    h.tick('lfaButton', True)
    enabled, _, events, request = h.tick('lfaButton', False)
    assert h.h._lat_enabled
    assert not enabled
    assert request == 0
    assert not events.contains(ET.ENABLE)

  def test_missing_stock_setpoint_pause_disables_until_explicit_driver_resume(self):
    h = NativeHarness()
    h.h.v_cruise_kph = 40.0
    h.cs.cruiseState.speed = 0.0
    h.cs.vEgo = 44.36 / 3.6
    h.cs.vEgoCluster = 47.0 / 3.6
    enabled, _, events, request = h.tick()
    assert (request) == (-1)
    assert not (enabled)
    assert h.h._cruise_cancel_state
    h.cs.vEgo = 35.0 / 3.6
    h.cs.vEgoCluster = 38.0 / 3.6
    for _ in range(1000):
      enabled, _, _, request = h.tick()
      assert not (enabled)
      assert (request) <= (0)
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
