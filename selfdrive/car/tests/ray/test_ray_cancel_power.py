"""Exercise the actual common-event method without the device runtime or power I/O."""

import __future__
import ast
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[4]


class Names:
  def __getattr__(self, name):
    return name


class EventFixture:
  def __init__(self):
    self.names = []

  def add(self, name):
    self.names.append(name)

  def contains(self, kind):
    # This fixture only generates the parked-gear no-entry condition.
    return kind == 'NO_ENTRY' and 'wrongGear' in self.names


env = dict(Events=EventFixture, EventName=Names(), ButtonType=Names(), GearShifter=Names(), ET=Names(), MAX_CTRL_SPEED=55.0, DT_CTRL=0.01)
tree = ast.parse((ROOT / 'selfdrive/car/car_specific.py').read_text())
source_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CarSpecificEvents')
method = next(n for n in source_class.body if isinstance(n, ast.FunctionDef) and n.name == 'create_common_events')
exec(
  compile(
    ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
    str(ROOT / 'selfdrive/car/car_specific.py'),
    'exec',
    flags=__future__.annotations.compiler_flag,
  ),
  env,
)


class TestRayCancelPower:
  def setup_method(self):
    self.writes = []
    self.helper = NS(
      CP=NS(carFingerprint='KIA_RAY_EV', pcmCruise=False, openpilotLongitudinalControl=True),
      mute_door=False,
      mute_seatbelt=False,
      do_shutdown=False,
      params=NS(put_bool=lambda key, value: self.writes.append((key, value))),
      steering_unpressed=0,
      no_steer_warning=False,
      silent_steer_warning=0,
    )
    self.cs = NS(
      **dict.fromkeys(
        (
          'doorOpen',
          'seatbeltUnlatched',
          'espDisabled',
          'espActive',
          'stockFcw',
          'stockAeb',
          'brakeHoldActive',
          'parkingBrake',
          'accFaulted',
          'steeringPressed',
          'brakePressed',
          'gasPressed',
          'vehicleSensorsInvalid',
          'invalidLkasSetting',
          'lowSpeedAlert',
          'buttonEnable',
          'steerFaultTemporary',
          'steerFaultPermanent',
        ),
        False,
      )
    )
    self.cs.__dict__.update(
      gearShifter='park',
      vEgo=0.0,
      standstill=True,
      cruiseState=NS(available=True, enabled=False, nonAdaptive=False),
      buttonEvents=[],
      activateCruise=0,
      softHoldActive=0,
    )
    self.previous = NS(cruiseState=NS(enabled=False), activateCruise=0, steerFaultTemporary=False)

  def evaluate(self, button='cancel', pressed=True, activation=0):
    self.cs.buttonEvents = [] if button is None else [NS(type=button, pressed=pressed)]
    self.cs.activateCruise = activation
    return env['create_common_events'](self.helper, self.cs, self.previous, pcm_enable=False, allow_button_cancel=False).names

  def test_ray_park_cancel_press_does_not_power_off(self):
    assert ('buttonCancel') in (self.evaluate())
    assert (self.writes) == ([])
    assert not (self.helper.do_shutdown)

  def test_ray_park_cancel_release_does_not_power_off(self):
    assert ('buttonCancel') in (self.evaluate(pressed=False))
    assert (self.writes) == ([])

  def test_recorded_press_then_resume_release_keeps_power_on(self):
    assert ('buttonCancel') in (self.evaluate(pressed=True, activation=0))
    release = self.evaluate(pressed=False, activation=2)
    assert ('wrongGear') in (release)
    assert ('buttonEnable') not in (release)
    assert (self.writes) == ([])

  def test_ray_drive_cancel_still_disengages(self):
    self.cs.gearShifter = 'drive'
    self.cs.standstill = False
    self.cs.vEgo = 10.0
    assert ('buttonCancel') in (self.evaluate())
    assert (self.writes) == ([])

  def test_ray_explicit_resume_remains_available_in_drive(self):
    self.cs.gearShifter = 'drive'
    events = self.evaluate(pressed=False, activation=2)
    assert ('buttonEnable') in (events)
    assert ('buttonCancel') not in (events)
    assert (self.writes) == ([])

  def test_ray_main_button_does_not_power_off(self):
    self.evaluate('mainCruise', True)
    self.evaluate('mainCruise', False, activation=2)
    assert (self.writes) == ([])

  def test_no_button_does_not_request_shutdown(self):
    self.evaluate(None)
    assert (self.writes) == ([])

  def test_other_vehicle_park_shortcut_is_unchanged(self):
    self.helper.CP.carFingerprint = 'OTHER_CAR'
    assert ('buttonCancel') in (self.evaluate())
    assert (self.writes) == ([('DoShutdown', True)])
