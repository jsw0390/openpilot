"""Owned lead pauses, bounded resumes and driver priority; no vehicle I/O."""
import pytest

import test_ray_manual_off as upper
import test_ray_controller_cancel as lower

RayLeadPause = upper.namespace['RayLeadPause']
required = upper.namespace['ray_lead_pause_required']


def update(p, count=1, **changes):
  inputs = dict(stock_active=False, stock_kph=0, ego_kph=48, need_pause=False, clear=False, valid=True)
  inputs.update(changes)
  return [p.update(**inputs) for _ in range(count)]


def paused():
  p = RayLeadPause()
  assert update(p, stock_active=True, stock_kph=50, need_pause=True) == [-1]
  assert update(p) == [0]
  assert p.phase == 'paused'
  return p


@pytest.mark.parametrize('speed,distance,relative,expected', [
  (53.85, 32, -3.8, False),  # begin stock-setting reduction before a pause is necessary
  (61.72, 42.45, -2.32, False),
  (56.51, 45.0, -5.01, False),  # recorded premature pause: closing time almost 9 s
  (53.85, 20, -3.8, True),
  (60, 60, -16.67, True),  # distant stopped lead still needs early release
  (60, 60, -1, False),
  (50, 30, -0.6, False),
  (50, 15, 0, False),
  (50, 9, 0, True),
  (50, 15, 1, False),
  (50, 0, -5, False),
  (0, 5, -5, False),
  (50, float('nan'), -5, False),
])
def test_pause_trigger(speed, distance, relative, expected):
  assert required(speed / 3.6, distance, relative, 0.99) is expected


def test_uncertain_lead_does_not_press_pause():
  assert not required(50 / 3.6, 10, -5, 0.5)


def test_only_our_confirmed_pause_can_resume():
  p = RayLeadPause()
  assert update(p, 1000, clear=True) == [0] * 1000
  assert p.phase == 'idle'
  p = paused()
  assert update(p, 299, clear=True) == [0] * 299
  assert update(p, clear=True) == [2]
  assert p.phase == 'resume_wait'
  assert update(p, clear=True, stock_active=True, stock_kph=50) == [0]
  assert p.phase == 'idle'


def test_dropout_or_reappearing_lead_restarts_clearance():
  p = paused()
  update(p, 200, clear=True)
  assert update(p, clear=False) == [0]
  assert update(p, 299, clear=True) == [0] * 299
  assert update(p, clear=True) == [2]
  assert update(p, clear=False, need_pause=True) == [-1]
  assert p.phase == 'paused'
  assert update(p, 299, clear=True) == [0] * 299


@pytest.mark.parametrize('overrides', [dict(ego_kph=29), dict(ego_kph=0), dict(valid=False), dict(stock_active=None)])
def test_low_speed_invalid_or_unknown_feedback_requires_driver(overrides):
  p = paused()
  update(p, clear=True, **overrides)
  assert p.phase == 'lockout'
  assert 2 not in update(p, 1000, clear=True)


def test_large_speed_gap_or_present_lead_never_resumes():
  for overrides in [dict(ego_kph=35, clear=True), dict(clear=False), dict(clear=True, need_pause=True)]:
    p = paused()
    assert 2 not in update(p, 1600, **overrides)
    assert p.phase == 'lockout'


def test_unacknowledged_pause_and_resume_are_not_retried():
  p = RayLeadPause()
  assert 2 not in update(p, 1500, stock_active=True, stock_kph=50, need_pause=True, clear=True)
  assert p.phase == 'lockout'
  p = paused()
  update(p, 300, clear=True)
  update(p, 51, clear=True)
  assert p.phase == 'lockout'
  assert 2 not in update(p, 1000, clear=True)


def test_bad_resume_ack_cancels_instead_of_accepting_higher_setpoint():
  p = paused()
  update(p, 300, clear=True)
  assert update(p, clear=True, stock_active=True, stock_kph=60) == [-1]
  assert p.phase == 'lockout'


def fixture():
  b = upper.TestRayManualOff()
  b.setup_method()
  h = b.helper
  h.rayVisionIPedalAssist = 3
  h.v_ego_kph_set = 54
  h.d_rel, h.v_rel, h.v_lead_kph, h.lead_prob = 20, -3.8, 40, 0.99
  b.cs.vEgo = 54 / 3.6
  b.cs.cruiseState.speed = 57 / 3.6
  return b


def tick(b):
  b.helper._activate_cruise = 0
  return b.helper._update_ray_ipedal_assist(b.cs, b.cc, 57)


def test_pause_preempts_recovery_and_no_lead_target_floor_hides_slow_lead():
  b = fixture()
  h = b.helper
  h._ray_stock_restore_kph = 65
  h._ray_lead_clear_frames = 200
  tick(b)
  assert h._activate_cruise == -1
  assert h._ray_stock_restore_kph == 65
  assert h._lat_enabled
  b.cs.gearStep = 3
  b.cs.cruiseState.speed = 0
  tick(b)
  assert h._ray_lead_pause.phase == 'paused'
  h.d_rel = 0
  # No clear model corroboration: losing the lead does not resume.
  for _ in range(400):
    tick(b)
    assert h._activate_cruise == 0
  h._ray_resume_scene_clear = True
  for _ in range(299):
    tick(b)
    assert h._activate_cruise == 0
  tick(b)
  assert h._activate_cruise == 2
  assert h._lat_enabled


@pytest.mark.parametrize('button', ['mainCruise', 'cancel', 'accelCruise', 'decelCruise'])
@pytest.mark.parametrize('pressed', [True, False])
def test_manual_switch_discards_automatic_pause_ownership(button, pressed):
  b = fixture()
  tick(b)
  b.cs.gearStep = 3
  b.cs.cruiseState.speed = 0
  tick(b)
  b.helper._ray_resume_scene_clear = True
  b.tick(getattr(upper.BUTTONS, button), pressed)
  assert b.helper._ray_lead_pause.phase == 'idle'
  for _ in range(400):
    tick(b)
    assert b.helper._activate_cruise != 2


@pytest.mark.parametrize('signal', ['brakePressed', 'gasPressed', 'brakeHoldActive', 'canValid', 'gearShifter'])
def test_interruption_revokes_pause_ownership(signal):
  b = fixture()
  tick(b)
  b.cs.gearStep = 3
  b.cs.cruiseState.speed = 0
  tick(b)
  setattr(b.cs, signal, False if signal == 'canValid' else 'park' if signal == 'gearShifter' else True)
  tick(b)
  assert b.helper._ray_lead_pause.phase == 'idle'
  assert b.helper._activate_cruise == 0


def test_lower_toggle_is_one_pulse_per_acknowledged_transition():
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.cs.out.gearStep = 7
  b.cs.out.activateCruise = -1
  sent = []
  for _ in range(100):
    sent.append(b.h.make_spam_button(b.cc, b.cs))
    b.h.frame += 1
  assert sent == [lower.Buttons.CANCEL] + [0] * 99
  b.cs.out.gearStep = 3
  b.cs.out.activateCruise = 0
  b.h.make_spam_button(b.cc, b.cs)
  b.cs.out.activateCruise = 2
  sent = []
  for _ in range(100):
    sent.append(b.h.make_spam_button(b.cc, b.cs))
    b.h.frame += 1
  assert sent == [lower.Buttons.CANCEL] + [0] * 99


def test_invalid_can_withdraws_lower_pending_resume():
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.cs.out.gearStep = 3
  b.cs.out.activateCruise = 2
  b.cc.enabled = False
  assert b.h.make_spam_button(b.cc, b.cs) == 0
  b.cs.out.canValid = False
  b.cc.enabled = True
  b.h.frame += 1
  assert b.h.make_spam_button(b.cc, b.cs) == 0
  assert b.h.ray_ev_pending_resume_until == -1


@pytest.mark.parametrize('mode', [0, 2])
def test_switching_mode_withdraws_queued_resume(mode):
  b = fixture()
  b.helper._ray_lead_pause = paused()
  update(b.helper._ray_lead_pause, 300, clear=True)
  b.helper.rayVisionIPedalAssist = mode
  tick(b)
  assert b.helper._activate_cruise == -1
  assert b.helper._ray_lead_pause.phase == 'idle'


@pytest.mark.parametrize('problem', ['fresh', 'stale_radar', 'stale_model', 'stale_controls', 'invalid', 'second_lead',
                                     'uncertain_model_lead', 'empty_model', 'model_stop', 'model_slower', 'nan_model', 'turn'])
def test_resume_scene_requires_fresh_agreement(problem):
  import ast
  from types import SimpleNamespace as NS

  method = next(n for n in upper.source_class.body if isinstance(n, ast.FunctionDef) and n.name == 'update_v_cruise')
  block = next(n for n in method.body if isinstance(n, ast.If) and 'self.rayVisionIPedalAssist >= 2' in ast.unparse(n.test))

  class Inputs(dict):
    alive = dict.fromkeys(['radarState', 'modelV2', 'carControl'], True)
    recv_time = dict.fromkeys(alive, 10.)

    def all_checks(self, services):
      return problem != 'invalid'

  h = NS(is_ray_ev=True, rayVisionIPedalAssist=3, _ray_resume_scene_clear=False,
         _ray_lead_pause=NS(stock_kph=60), _ray_stock_restore_kph=0, rayVisionCruiseLeadProb=.85)
  cs = NS(vEgo=15., leftBlinker=problem == 'turn', rightBlinker=False, steeringAngleDeg=0., cruiseState=NS(speed=0.))
  model = NS(leadsV3=[NS(prob=.05)], action=NS(shouldStop=problem == 'model_stop', desiredVelocity=16.))
  if problem == 'uncertain_model_lead': model.leadsV3[0].prob = .25
  if problem == 'empty_model': model.leadsV3 = []
  if problem == 'model_slower': model.action.desiredVelocity = 10.
  if problem == 'nan_model': model.action.desiredVelocity = float('nan')
  inputs = Inputs(radarState=NS(leadOne=NS(status=False), leadTwo=NS(status=problem == 'second_lead')), modelV2=model)
  if problem.startswith('stale_'):
    service = {'stale_radar': 'radarState', 'stale_model': 'modelV2', 'stale_controls': 'carControl'}[problem]
    inputs.recv_time = dict(inputs.recv_time, **{service: 9.})
  code = compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])), 'resume_scene', 'exec')
  exec(code, dict(self=h, sm=inputs, CS=cs, math=upper.math, time=NS(monotonic=lambda: 10.),
                 ray_resume_scene_clear=upper.namespace['ray_resume_scene_clear'],
                 ray_lead_recovery_target_kph=upper.namespace['ray_lead_recovery_target_kph'], CV=NS(MS_TO_KPH=3.6)))
  assert h._ray_resume_scene_clear == (problem == 'fresh')


def test_curve_below_stock_minimum_blocks_resume():
  b = fixture()
  tick(b)
  b.cs.gearStep = 3
  b.cs.cruiseState.speed = 0
  tick(b)
  b.helper.d_rel = 0
  b.helper._ray_resume_scene_clear = True
  b.helper.desiredSource, b.helper.desiredSpeed = 'model', 20
  for _ in range(400):
    tick(b)
    assert b.helper._activate_cruise == 0


def test_late_resume_acknowledgement_is_cancelled_once_after_timeout():
  p = paused()
  update(p, 300, clear=True)
  update(p, 51, clear=True)
  assert p.phase == 'lockout'
  assert update(p, clear=True, stock_active=True, stock_kph=50) == [-1]
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.cs.out.gearStep, b.cs.out.activateCruise = 7, -1
  sent = []
  for _ in range(100):
    sent.append(b.h.make_spam_button(b.cc, b.cs))
    b.h.frame += 1
  assert sent == [lower.Buttons.CANCEL] + [0] * 99
