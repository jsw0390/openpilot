"""Independent Ray steering checks. All inputs and publishers stay in memory."""

import __future__
import ast
import bisect
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest
from cereal import car, log
from test_ray_stock_buttons import Sequence, BUTTONS

ROOT = Path(__file__).resolve().parents[4]
EventName = log.OnroadEvent.EventName


def source_nodes(path, names):
  return [n for n in ast.parse((ROOT / path).read_text()).body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]


def compile_nodes(nodes, filename):
  return compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), filename, 'exec', flags=__future__.annotations.compiler_flag)


if sys.platform == 'linux':
  from openpilot.selfdrive.selfdrived.events import Events, ET
  from openpilot.selfdrive.selfdrived.state import StateMachine

  spec = importlib.util.spec_from_file_location('candidate_ray_lateral', ROOT / 'selfdrive/selfdrived/ray_lateral.py')
  lateral = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(lateral)
else:
  # Run the real state machine and Events methods on macOS as well. Alert
  # construction needs device libraries; only their event-type keys are needed
  # here, so extract those from the real table instead of duplicating it.
  scope = dict(car=car, log=log, EventName=EventName, bisect=bisect, DT_CTRL=0.01)
  event_path = 'selfdrive/selfdrived/events.py'
  exec(compile_nodes(source_nodes(event_path, {'Events', 'ET'}), event_path), scope)
  event_tree = ast.parse((ROOT / event_path).read_text())
  event_map = next(n.value for n in event_tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == 'EVENTS')
  scope['EVENTS'] = {
    getattr(EventName, key.attr): {getattr(scope['ET'], kind.attr): None for kind in event_map.values[index].keys} for index, key in enumerate(event_map.keys)
  }
  state_tree = ast.parse((ROOT / 'selfdrive/selfdrived/state.py').read_text())
  exec(compile_nodes([n for n in state_tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))], 'state.py'), scope)
  lateral_tree = ast.parse((ROOT / 'selfdrive/selfdrived/ray_lateral.py').read_text())
  exec(compile_nodes([n for n in lateral_tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))], 'ray_lateral.py'), scope)
  lateral = NS(**scope)
  Events, ET, StateMachine = scope['Events'], scope['ET'], scope['StateMachine']


def events(*names):
  e = Events()
  for name in names:
    e.add(getattr(EventName, name))
  return e


def update(state, selected, *names, **overrides):
  health = dict(initialized=True, passive=False, can_valid=True, inputs_ok=True, monitoring_ok=True, panda_ok=True)
  health.update(overrides)
  with contextlib.redirect_stdout(io.StringIO()):
    return state.update(selected, events(*names), **health)


def engaged_state():
  s = lateral.RayLateralState()
  assert update(s, False) == (False, False)
  assert update(s, True) == (True, True)
  return s


@pytest.mark.parametrize('initial', [False, True])
@pytest.mark.parametrize('button', [BUTTONS.mainCruise, BUTTONS.cancel, BUTTONS.accelCruise, BUTTONS.decelCruise])
@pytest.mark.parametrize('hold', [5, 150])
def test_cruise_switches_never_change_steering_selection(initial, button, hold):
  s = Sequence(armed=True, enabled=True)
  s.h._lat_enabled = initial
  s.h._cancel_button_mode = 1
  s.press(button, hold)
  assert s.h._lat_enabled == initial


@pytest.mark.parametrize('mode', [0, 1, 2])
def test_lfa_press_toggles_once_even_while_cruise_cancel_is_held(mode):
  s = Sequence(armed=True, enabled=True)
  s.h._lfa_button_mode = mode
  s.tick(BUTTONS.cancel, True)
  s.tick(BUTTONS.lfaButton, True)
  for _ in range(200):
    s.tick()
    assert s.h._lat_enabled
    assert not s.b.cc.enabled
  s.tick(BUTTONS.lfaButton, False)
  assert s.h._lat_enabled
  s.tick(BUTTONS.lfaButton, True)
  assert not s.h._lat_enabled


@pytest.mark.parametrize(
  'name',
  [
    'buttonCancel',
    'buttonEnable',
    'pcmDisable',
    'pcmEnable',
    'pedalPressed',
    'preEnableStandstill',
    'gasPressedOverride',
    'wrongCarMode',
    'wrongCruiseMode',
    'resumeBlocked',
  ],
)
def test_longitudinal_events_do_not_disengage_or_enable_steering(name):
  s = engaged_state()
  for _ in range(400):
    assert update(s, True, name) == (True, True)
  assert update(s, False, name) == (False, False)
  for _ in range(5):
    assert update(s, False, name) == (False, False)


def test_restart_with_old_on_selection_requires_a_new_off_on_cycle():
  s = lateral.RayLateralState()
  for _ in range(10):
    assert update(s, True) == (False, False)
  assert update(s, False) == (False, False)
  assert update(s, True) == (True, True)


@pytest.mark.parametrize(
  'flag,value', [('initialized', False), ('passive', True), ('can_valid', False), ('inputs_ok', False), ('monitoring_ok', False), ('panda_ok', False)]
)
def test_health_failure_stops_lateral_and_never_silently_reengages(flag, value):
  s = engaged_state()
  assert update(s, True, **{flag: value}) == (False, False)
  for _ in range(100):
    assert update(s, True) == (False, False)
  update(s, False)
  assert update(s, True) == (True, True)


@pytest.mark.parametrize(
  'name',
  ['canError', 'canBusMissing', 'controlsMismatch', 'steerUnavailable', 'parkBrake', 'wrongGear', 'driverDistracted3', 'driverUnresponsive3', 'tooDistracted'],
)
def test_critical_events_stop_steering_and_block_new_engagement(name):
  s = engaged_state()
  assert update(s, True, name) == (False, False)
  update(s, False, name)
  assert update(s, True, name) == (False, False)
  assert update(s, True) == (False, False)


def test_soft_disable_and_driver_override_keep_normal_state_machine_behavior():
  s = engaged_state()
  assert update(s, True, 'steerOverride') == (True, True)
  assert update(s, True, 'steerTempUnavailable') == (True, True)
  assert s.state_machine.state == log.SelfdriveState.OpenpilotState.softDisabling
  for _ in range(301):
    update(s, True, 'steerTempUnavailable')
  assert not s.active
  assert update(s, True) == (False, False)


def panda_fixture():
  cp = car.CarParams.new_message(alternativeExperience=0, safetyConfigs=[{'safetyModel': 'hyundai', 'safetyParam': 4}])
  p = log.PandaState.new_message(safetyModel='hyundai', safetyParam=4, alternativeExperience=0, controlsAllowed=False)
  return cp, p


@pytest.mark.parametrize('bad', ['fault', 'heartbeat', 'rxCheck', 'busOff', 'errorPassive', 'mode', 'param', 'alternative', 'missing'])
def test_faulted_or_mismatched_panda_cannot_admit_lateral(bad):
  cp, p = panda_fixture()
  assert lateral.panda_lateral_ready(cp, [p])
  if bad == 'fault':
    p.faults = ['interruptRateCan2']
  elif bad == 'heartbeat':
    p.heartbeatLost = True
  elif bad == 'rxCheck':
    p.safetyRxChecksInvalid = True
  elif bad == 'busOff':
    p.canState1.busOff = True
  elif bad == 'errorPassive':
    p.canState1.errorPassive = True
  elif bad == 'mode':
    p.safetyModel = 'noOutput'
  elif bad == 'param':
    p.safetyParam = 0
  elif bad == 'alternative':
    p.alternativeExperience = 1
  assert not lateral.panda_lateral_ready(cp, [] if bad == 'missing' else [p])


control_tree = ast.parse((ROOT / 'selfdrive/controls/controlsd.py').read_text())
control_class = next(n for n in control_tree.body if isinstance(n, ast.ClassDef) and n.name == 'Controls')
control_method = next(n for n in control_class.body if isinstance(n, ast.FunctionDef) and n.name == 'state_control')
start = next(i for i, n in enumerate(control_method.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'CC')
stop = next(i for i, n in enumerate(control_method.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'actuators')
control_code = compile_nodes(control_method.body[start:stop], 'controlsd.py')


def controls(lateral_active=True, selected=True, cruise=False, valid=True, passive=False, gear='drive', speed=10, min_steer_speed=0):
  class Inputs(dict):
    def all_checks(self, services):
      return valid

  cs = car.CarState.new_message(vEgo=speed, standstill=speed == 0, canValid=True, gearShifter=gear, latEnabled=selected)
  ss = log.SelfdriveState.new_message(enabled=cruise, active=cruise, lateralActive=lateral_active)
  h = NS(
    CP=car.CarParams.new_message(carFingerprint='KIA_RAY_EV', passive=passive, openpilotLongitudinalControl=True, minSteerSpeed=min_steer_speed),
    sm=Inputs(selfdriveState=ss, onroadEvents=[]),
    params=NS(get_bool=lambda key: True),
    carrot_controls=NS(lat_suspend_control=lambda cs, active: active),
  )
  scope = dict(self=h, CS=cs, car=car, MIN_LATERAL_CONTROL_SPEED=0.3)
  exec(control_code, scope)
  return scope['CC']


def test_lateral_only_never_enables_longitudinal():
  cc = controls()
  assert cc.latActive
  assert not cc.enabled
  assert not cc.longActive


def test_cruise_only_never_enables_steering_even_with_always_lateral_setting():
  cc = controls(lateral_active=False, selected=False, cruise=True)
  assert not cc.latActive
  assert cc.enabled and cc.longActive


@pytest.mark.parametrize(
  'case',
  [
    dict(selected=False),
    dict(lateral_active=False),
    dict(valid=False),
    dict(passive=True),
    dict(gear='park'),
    dict(gear='reverse'),
    dict(gear='neutral'),
    dict(speed=0),
  ],
)
def test_control_output_stays_off_when_not_admitted(case):
  assert not controls(**case).latActive


def test_stop_and_move_retains_selected_state_but_does_not_steer_at_standstill():
  s = engaged_state()
  assert not controls(lateral_active=s.active, speed=0).latActive
  assert update(s, True, 'preEnableStandstill') == (True, True)
  assert controls(lateral_active=s.active, speed=10).latActive


@pytest.mark.parametrize('speed_kph', [0.5, 1, 3, 5, 10])
def test_creeping_and_low_speed_steering_do_not_require_cruise(speed_kph):
  cc = controls(speed=speed_kph / 3.6)
  assert cc.latActive
  assert not cc.enabled and not cc.longActive


def test_creeping_does_not_bypass_vehicle_minimum_or_zero_speed_guard():
  assert not controls(speed=1 / 3.6, min_steer_speed=5 / 3.6).latActive
  assert not controls(speed=0.09).latActive


def test_schema_keeps_lateral_and_cruise_independent():
  msg = log.SelfdriveState.new_message(enabled=False, active=False, lateralEnabled=True, lateralActive=True)
  with log.SelfdriveState.from_bytes(msg.to_bytes()) as decoded:
    assert decoded.lateralEnabled and decoded.lateralActive
    assert not decoded.enabled and not decoded.active


def test_driver_monitoring_counts_lateral_only_as_engaged():
  tree = ast.parse((ROOT / 'selfdrive/monitoring/helpers.py').read_text())
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DriverMonitoring')
  method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'run_step')
  scope = dict(car=car)
  exec(compile_nodes([method], 'monitoring/helpers.py'), scope)
  observed = {}
  monitor = NS(_set_policy=lambda **kw: None, _update_states=lambda **kw: observed.update(states=kw), _update_events=lambda **kw: observed.update(events=kw))
  for cruise, steer in ((False, True), (True, False), (False, False)):
    sm = dict(
      carState=car.CarState.new_message(vEgo=10, gearShifter='drive'),
      selfdriveState=log.SelfdriveState.new_message(enabled=cruise, lateralEnabled=steer),
      modelV2=NS(meta=NS(disengagePredictions=NS(brakeDisengageProbs=[0.0]))),
      liveCalibration=NS(rpyCalib=[0, 0, 0]),
      driverStateV2=NS(),
    )
    scope['run_step'](monitor, sm)
    assert observed['states']['op_engaged'] == (cruise or steer)
    assert observed['events']['op_engaged'] == (cruise or steer)


def test_lateral_only_retains_process_loss_audible_alert():
  scope = dict(time=NS(monotonic=lambda: 10.0), SELFDRIVE_STATE_TIMEOUT=5)
  exec(compile_nodes(source_nodes('selfdrive/ui/soundd.py', {'check_selfdrive_timeout_alert'}), 'soundd.py'), scope)

  class Inputs(dict):
    recv_time = {'selfdriveState': 1.0}

  sm = Inputs(selfdriveState=log.SelfdriveState.new_message(enabled=False, lateralEnabled=True))
  assert scope['check_selfdrive_timeout_alert'](sm)
  sm['selfdriveState'].lateralEnabled = False
  assert not scope['check_selfdrive_timeout_alert'](sm)
