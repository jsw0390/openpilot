"""Offline button-selection checks; no CAN frames are emitted."""

import ast
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[4]
path = ROOT / 'opendbc_repo/opendbc/car/hyundai/carcontroller.py'
tree = ast.parse(path.read_text())
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CarController')
names = {'make_spam_button', '_ray_ev_cruise_state_from_gear', '_ray_ev_stock_cruise_button'}
methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
Buttons = NS(NONE=0, RES_ACCEL=1, SET_DECEL=2, CANCEL=4)
env = {
  'Buttons': Buttons,
  'GearShifter': NS(drive='drive'),
  'ButtonType': NS(mainCruise='mainCruise', cancel='cancel', accelCruise='accelCruise', decelCruise='decelCruise'),
  'CAR': NS(KIA_RAY_EV='KIA_RAY_EV'),
  'CV': NS(MS_TO_KPH=3.6, MS_TO_MPH=2.23694),
  'RAY_EV_ACTIVATE_BUTTON': 4,
  'RAY_EV_DRIVER_PAUSE_RESUME_FRAMES': 50,
  'RAY_EV_SPEED_SYNC_BLOCK_FRAMES': 80,
  'RAY_EV_STATE_SYNC_WAIT_FRAMES': 40,
  'np': NS(clip=lambda v, lo, hi: min(hi, max(lo, v))),
}
exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), str(path), 'exec'), env)


class Controller:
  pass


for name in names:
  setattr(Controller, name, env[name])


class TestRayControllerCancel:
  def setup_method(self):
    self.h = Controller()
    self.h.__dict__.update(
      CP=NS(carFingerprint='KIA_RAY_EV'),
      frame=100,
      ray_ev_prev_cruise_button=0,
      ray_ev_request_prev=0,
      ray_ev_pending_resume_until=-1,
      ray_ev_pause_sent=False,
      ray_ev_pause_resume_frame=-100,
      ray_ev_speed_sync_block_frame=-100,
      ray_ev_activate_retry=12,
      ray_ev_cruise_enabled_last=False,
      ray_ev_estimated_cruise_speed=0,
      ray_ev_speed_bias_active=False,
      activateCruise=0,
      button_spamming_count=0,
      prev_clu_speed=0,
      button_spam1=8,
      button_spam2=30,
      button_wait=12,
      last_button_frame=0,
      speed_from_pcm=0,
    )
    self.cc = NS(enabled=True, cruiseControl=NS(resume=False), hudControl=NS(setSpeed=31 / 3.6, leadVisible=False, leadDistance=0.0, leadRelSpeed=0.0))
    self.cs = NS(
      is_metric=True,
      cruise_buttons=[0],
      out=NS(gearShifter='drive', vEgo=32 / 3.6, gearStep=0, activateCruise=0, brakePressed=False, brakeHoldActive=False,
             gasPressed=False, buttonEvents=[], cruiseState=NS(speed=0.0, enabled=False)),
    )

  def test_physical_cancel_blocks_pending_retry_with_old_enabled_feedback(self):
    self.cs.cruise_buttons = [Buttons.CANCEL]
    assert (self.h.make_spam_button(self.cc, self.cs)) == (0)
    assert (self.h.ray_ev_activate_retry) == (0)

  def test_negative_request_blocks_pending_retry_with_old_enabled_feedback(self):
    self.cs.out.activateCruise = -1
    assert (self.h.make_spam_button(self.cc, self.cs)) == (0)
    assert (self.h.ray_ev_activate_retry) == (0)

  def test_physical_cancel_blocks_simultaneous_positive_request(self):
    self.cs.out.activateCruise = 2
    self.cs.cruise_buttons = [Buttons.CANCEL]
    assert (self.h.make_spam_button(self.cc, self.cs)) == (0)
    assert (self.h.ray_ev_activate_retry) == (0)

  def test_disabled_controller_can_still_pause_confirmed_stock_cruise(self):
    self.cc.enabled = False
    self.cs.out.activateCruise = -1
    self.cs.out.gearStep = 7
    sent = []
    for _ in range(20):
      sent.append(self.h.make_spam_button(self.cc, self.cs))
      self.h.frame += 1
    assert (Buttons.CANCEL) in (sent)
    assert all(b in (0, Buttons.CANCEL) for b in sent)

  def test_driver_enabled_stock_cruise_is_never_auto_paused(self):
    self.cs.out.gearStep = 7
    self.cc.enabled = False
    self.cs.out.activateCruise = 0
    assert self.h.make_spam_button(self.cc, self.cs) == 0

  def test_disabled_and_already_paused_does_not_send_button(self):
    self.cc.enabled = False
    self.cs.out.activateCruise = -1
    assert (self.h.make_spam_button(self.cc, self.cs)) == (0)

  def test_distinct_activation_request_remains_possible(self):
    self.cs.out.activateCruise = 2
    assert (self.h.make_spam_button(self.cc, self.cs)) == (Buttons.CANCEL)

  def test_brake_and_gas_prevent_activation(self):
    for pedal in ('brakePressed', 'gasPressed'):
      self.setup_method()
      self.cs.out.activateCruise = 2
      setattr(self.cs.out, pedal, True)
      assert (self.h.make_spam_button(self.cc, self.cs)) == (0)
      assert (self.h.ray_ev_activate_retry) == (0)
