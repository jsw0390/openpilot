"""Offline regression checks for the Ray driver OFF request.

Runs the actual cruise.py method bodies with a minimal signal fixture. This does
not import the device runtime, send CAN, or validate the vehicle/safety stack.
"""

import ast
import math
import runpy
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[4]
BUTTONS = NS(**{name: name for name in ('accelCruise', 'decelCruise', 'gapAdjustCruise', 'cancel', 'lfaButton', 'mainCruise', 'paddleLeft', 'paddleRight')})
METHODS = {
  '_prepare_buttons',
  '_carrot_command',
  '_update_cruise_buttons',
  '_ray_ev_resume_speed',
  '_ray_ev_set_speed',
  '_cruise_control',
  '_update_cruise_state',
  '_auto_speed_up',
  '_ray_ipedal_enabled',
  '_ray_curve_cruise_pause_enabled',
  '_ray_ipedal_set_cruise',
  '_update_ray_ipedal_assist',
  '_ray_lead_target_kph',
}
namespace = runpy.run_path(str(ROOT / 'selfdrive/carrot/ray_vision.py'))
namespace.update(math=math, ButtonType=BUTTONS, GearShifter=NS(drive='drive'), CV=NS(MPH_TO_KPH=1.609344))
tree = ast.parse((ROOT / 'selfdrive/car/cruise.py').read_text())
source_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'VCruiseCarrot')
methods = [n for n in source_class.body if isinstance(n, ast.FunctionDef) and n.name in METHODS]
assert {n.name for n in methods} == METHODS
exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), str(ROOT / 'selfdrive/car/cruise.py'), 'exec'), namespace)


class Fixture:
  def __init__(self):
    self.__dict__.update(
      is_ray_ev=True,
      is_metric=True,
      button_cnt=0,
      button_prev=None,
      button_long_time=70,
      long_pressed=False,
      _cruise_button_long_delay=40,
      _cruise_button_mode=0,
      _cruise_speed_unit_basic=1,
      _cruise_speed_unit=10,
      carrot_cmd_index=0,
      carrot_cmd_index_last=0,
      _activate_cruise=0,
      _cruise_cancel_state=False,
      _cruise_ready=False,
      _lat_enabled=True,
      _paddle_decel_active=False,
      _paddle_mode=0,
      carrot_cruise_active=False,
      _pause_auto_speed_up=False,
      _ray_ev_cancel_pressed_while_enabled=False,
      _ray_ev_cancel_pressed=False,
      _ray_ev_cancel_long_pressed=False,
      _ray_ev_main_pressed_while_enabled=False,
      _cancel_button_mode=0,
      _soft_hold_active=0,
      _gas_tok=False,
      _gas_pressed_count=-10,
      _brake_pressed_count=-10,
      _gas_tok_timer=20,
      autoGasTokSpeed=0,
      autoCruiseControl=0,
      autoCruiseControl_cancel_timer=0,
      _cancel_timer=0,
      autoGasSyncSpeed=False,
      v_cruise_kph=31.0,
      v_ego_kph_set=32.0,
      _cruise_speed_min=5.0,
      _cruise_speed_max=145.0,
      _v_cruise_kph_at_brake=0,
      applyModelSpeed=0.0,
      nRoadLimitSpeed=50.0,
      autoSpeedUptoRoadSpeedLimit=0.0,
      desiredSpeed=0.0,
      desiredSource='',
      vTurnSpeed=0.0,
      d_rel=0.0,
      v_rel=0.0,
      v_lead_kph=0.0,
      lead_prob=0.0,
      lead_radar=False,
      xState=0,
      aTarget=0.0,
      rayVisionCruiseControl=2,
      rayVisionIPedalAssist=2,
      rayVisionIPedalSpeedDelta=7,
      rayVisionIPedalResumeMargin=2,
      rayVisionCruiseLeadProb=0.85,
      rayVisionCruiseTFollowAdd=0.35,
      _ray_ipedal_active=False,
      _ray_ipedal_timer=0,
      _ray_ipedal_cancel_repeat=0,
      params=NS(get_bool=lambda key: False),
      messages=[],
    )

  def _add_log(self, text):
    self.messages.append(text)


for method in METHODS:
  setattr(Fixture, method, namespace[method])


class TestRayManualOff:
  def setup_method(self):
    self.helper = Fixture()
    self.cs = NS(
      buttonEvents=[],
      vEgo=32 / 3.6,
      aEgo=0.0,
      gasPressed=False,
      brakePressed=False,
      gearShifter='drive',
      leftBlinker=False,
      rightBlinker=False,
      steeringAngleDeg=0.0,
      cruiseState=NS(enabled=False, standstill=False, speed=40 / 3.6),
    )
    self.cc = NS(enabled=True)

  def tick(self, button=None, pressed=False):
    self.cs.buttonEvents = [] if button is None else [NS(type=button, pressed=pressed)]
    self.helper._activate_cruise = 0
    result = self.helper._update_cruise_buttons(self.cs, self.cc, 31.0)
    return self.helper._activate_cruise, result

  def press_main(self):
    self.tick(BUTTONS.mainCruise, True)
    for _ in range(5):
      self.tick()
    return self.tick(BUTTONS.mainCruise, False)

  def test_main_off_with_auto_cruise_disabled_matches_drive_log(self):
    request, target = self.press_main()
    assert (request) == (-1)
    assert (target) == (31.0)

  def test_main_off_is_not_blocked_by_auto_cruise_cooldown(self):
    self.helper.autoCruiseControl = 1
    self.helper.autoCruiseControl_cancel_timer = 1500
    assert (self.press_main()[0]) == (-1)

  def test_manual_off_wins_over_pending_ipedal_resume(self):
    self.tick(BUTTONS.mainCruise, True)
    self.helper._ray_ipedal_active = True
    self.helper._ray_ipedal_timer = 120
    assert (self.tick(BUTTONS.mainCruise, False)[0]) == (-1)
    assert not (self.helper._ray_ipedal_active)
    assert not (self.helper._cruise_ready)

  def test_manual_off_stays_off_despite_delayed_control_feedback(self):
    assert (self.press_main()[0]) == (-1)
    # A slow lead and old enabled feedback must not restart the pause/resume loop.
    self.helper.d_rel = 10.0
    self.helper.lead_prob = 0.99
    self.helper.v_lead_kph = 10.0
    self.helper.v_rel = -6.0
    self.helper.v_ego_kph_set = 50.0
    self.cs.vEgo = 50 / 3.6
    self.cs.cruiseState.enabled = True
    for _ in range(10):
      assert (self.tick()[0]) == (0)
    self.cc.enabled = False
    self.cs.cruiseState.enabled = False
    # Keep ordinary auto-cruise logic out of this i-Pedal latch check.
    self.helper._gas_pressed_count = 0
    for _ in range(250):
      assert (self.tick()[0]) == (0)

  def test_explicit_main_on_clears_manual_off_latch(self):
    self.press_main()
    self.cc.enabled = False
    self.helper._cruise_cancel_state = True
    assert (self.press_main()[0]) == (2)
    assert not (self.helper._cruise_cancel_state)

  def test_long_main_press_cancels_on_release_only(self):
    assert (self.tick(BUTTONS.mainCruise, True)[0]) == (0)
    for _ in range(80):
      assert (self.tick()[0]) == (0)
    assert (self.tick(BUTTONS.mainCruise, False)[0]) == (-1)

  def test_no_button_has_no_manual_cancel(self):
    for _ in range(20):
      assert (self.tick()[0]) == (0)

  def test_other_vehicle_path_is_unchanged(self):
    self.helper.is_ray_ev = False
    assert (self.press_main()[0]) == (0)
