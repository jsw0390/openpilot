"""Device-runtime alarm integration; no publishers or audio devices opened."""
import importlib.util
import sys
from pathlib import Path

import pytest

if sys.platform != 'linux':
  pytest.skip('Requires the device alert runtime', allow_module_level=True)

from cereal import car
from openpilot.selfdrive.selfdrived.alertmanager import AlertManager
from openpilot.selfdrive.selfdrived.events import AudibleAlert, ET

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('candidate_ray_alerts', ROOT / 'selfdrive/selfdrived/ray_lateral_alerts.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def sample(monitor, **overrides):
  cs = car.CarState.new_message(vEgo=10, gearShifter='drive', standstill=False)
  args = dict(state_active=True, selected=True, soft_disabling=False,
              command_active=True, command_fresh=True, car_state_fresh=True)
  for name in ('vEgo', 'gearShifter', 'standstill'):
    if name in overrides:
      setattr(cs, name, overrides.pop(name))
  args.update(overrides)
  return monitor.update(cs, **args)


@pytest.mark.parametrize('reason', ['state_active', 'command_active', 'command_fresh', 'car_state_fresh'])
def test_loss_of_steering_has_audible_persistent_warning(reason):
  monitor = module.RayLateralAlerts()
  assert sample(monitor) == []
  alerts = sample(monitor, **{reason: False})
  assert len(alerts) == 1
  assert alerts[0].audible_alert == AudibleAlert.warningImmediate
  manager = AlertManager()
  manager.add_many(10, alerts)
  for frame in (10, 11, 200):
    manager.process_alerts(frame, {ET.WARNING})
    assert manager.current_alert.alert_type == 'rayLateralLost'
    assert manager.current_alert.audible_alert == AudibleAlert.warningImmediate
  assert sample(monitor, **{reason: False}) == []


def test_soft_disable_warns_before_command_stops():
  monitor = module.RayLateralAlerts()
  assert sample(monitor) == []
  assert sample(monitor, soft_disabling=True)[0].audible_alert == AudibleAlert.warningSoft
  assert sample(monitor, state_active=False)[0].audible_alert == AudibleAlert.warningImmediate


def test_manual_off_is_audible():
  monitor = module.RayLateralAlerts()
  sample(monitor)
  assert sample(monitor, selected=False, state_active=False)[0].audible_alert == AudibleAlert.prompt


def test_terminal_driver_monitoring_reason_is_not_hidden_by_takeover_alert():
  monitor = module.RayLateralAlerts()
  sample(monitor)
  alert = sample(monitor, state_active=False, disengage_reason='운전자 주시 경고로 조향 중단')[0]
  assert alert.alert_text_2 == '운전자 주시 경고로 조향 중단'
  assert alert.audible_alert == AudibleAlert.warningImmediate


def test_park_stop_and_initial_wait_do_not_raise_false_takeover_alarms():
  monitor = module.RayLateralAlerts()
  assert sample(monitor, command_active=False) == []
  sample(monitor)
  assert sample(monitor, command_active=False, vEgo=0, standstill=True) == []
  sample(monitor)
  assert sample(monitor, state_active=False, gearShifter='park', vEgo=0) == []
