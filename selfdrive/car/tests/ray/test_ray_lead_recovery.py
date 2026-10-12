"""Owned recovery behind a faster lead: limits, curves, and positive-command gates."""
from types import SimpleNamespace as NS

import pytest
import test_ray_manual_off as upper
import test_ray_controller_cancel as lower
from test_ray_stock_cruise_policy import step
from test_ray_stock_restore import slowed
from test_ray_following_policy import resume_fixture

target = upper.namespace['ray_lead_recovery_target_kph']
grant = upper.namespace['ray_stock_resume_allowed']


def scene(speed=52., distance=50.):
  radar = NS(leadOne=NS(status=True, dRel=distance, vRel=(speed - 45.) / 3.6, vLeadK=speed / 3.6, modelProb=.99),
             leadTwo=NS(status=False))
  model = NS(leadsV3=[NS(prob=.99, x=[distance])], action=NS(shouldStop=False, desiredVelocity=speed / 3.6))
  return radar, model


@pytest.mark.parametrize('original,lead,distance,expected', [(60.,52.,50.,52.), (60.,80.,60.,60.), (120.,110.,80.,100.)])
def test_recovery_uses_lower_of_lead_original_and_100(original, lead, distance, expected):
  radar, model = scene(lead, distance)
  assert target(45 / 3.6, 45., original, radar, model) == expected


@pytest.mark.parametrize('problem', ['near','closing','matched','lost','uncertain','mismatch','stop','model_slower',
                                     'second_lead','nan','turn','no_ownership','low_speed'])
def test_unclear_or_closing_scene_never_accelerates(problem):
  radar, model = scene()
  if problem == 'near': radar.leadOne.dRel = model.leadsV3[0].x[0] = 20.
  if problem == 'closing': radar.leadOne.vRel = -1.
  if problem == 'matched': radar.leadOne.vRel = 0.
  if problem == 'lost': radar.leadOne.status = False
  if problem == 'uncertain': model.leadsV3[0].prob = .3
  if problem == 'mismatch': model.leadsV3[0].x = [20.]
  if problem == 'stop': model.action.shouldStop = True
  if problem == 'model_slower': model.action.desiredVelocity = 40 / 3.6
  if problem == 'second_lead': radar.leadTwo = NS(status=True, dRel=20., vRel=-1., vLeadK=40 / 3.6, modelProb=.99)
  if problem == 'nan': radar.leadOne.vLeadK = float('nan')
  assert target((29 if problem == 'low_speed' else 45) / 3.6, 45., 0. if problem == 'no_ownership' else 60.,
                radar, model, turning=problem == 'turn') is None


def ready():
  b = slowed()
  b.helper.rayVisionIPedalAssist = 3
  radar, model = scene()
  b.helper.d_rel, b.helper.v_rel, b.helper.v_lead_kph, b.helper.lead_prob = 50., 2., 52., .99
  b.helper._ray_lead_recovery_target_kph = target(40 / 3.6, 40., 60., radar, model)
  return b


def test_partial_recovery_waits_two_seconds_and_retains_original_limit():
  b = ready()
  assert step(b, 199) == [0] * 199
  assert step(b, 1) == [4]
  assert b.helper._update_ray_ipedal_assist(b.cs, b.cc, 40.) == 52.
  assert b.helper._ray_stock_restore_kph == pytest.approx(60.)
  b.cs.cruiseState.speed = 52 / 3.6
  b.helper.v_rel = 0.
  b.helper._ray_lead_recovery_target_kph = None
  assert step(b, 300) == [0] * 300


@pytest.mark.parametrize('problem', ['curve','sharp_curve','stale_lead','stale_curve','lead_closing'])
def test_recovery_stops_when_curve_or_scene_changes(problem):
  b = ready()
  assert step(b, 200)[-1] == 4
  if problem in ('curve', 'sharp_curve'):
    b.helper.desiredSource = 'model'
    b.helper.desiredSpeed = 35. if problem == 'curve' else 20.
  elif problem == 'stale_lead': b.helper._ray_lead_data_valid = False
  elif problem == 'stale_curve': b.helper._ray_curve_data_valid = False
  else:
    b.helper.v_rel = -2.
    b.helper.v_lead_kph = 33.
    b.helper._ray_lead_recovery_target_kph = None
  assert 4 not in step(b, 300)


def test_clear_road_recovery_also_caps_at_100():
  b = slowed()
  b.helper._ray_stock_restore_kph = 120.
  assert step(b, 199) == [0] * 199
  assert b.helper._update_ray_ipedal_assist(b.cs, b.cc, 40.) == 100.
  assert b.helper._activate_cruise == 4
  b.cs.cruiseState.speed = 100 / 3.6
  assert step(b, 300) == [0] * 300


@pytest.mark.parametrize('problem', ['valid','dm_off','distracted','stale','panda','paused','above_100'])
def test_positive_steps_require_checked_scene_driver_and_safety(problem):
  cs, args = resume_fixture()
  cs.activateCruise, cs.gearStep, cs.vCruise = 4, 7, 60.
  if problem == 'dm_off': args['monitor'].isActiveMode = False
  if problem == 'distracted': args['events'] = [NS(noEntry=False,userDisable=False,softDisable=False,immediateDisable=False,name='driverDistracted1')]
  if problem == 'stale': args['inputs_valid'] = False
  if problem == 'panda': args['panda_ok'] = False
  if problem == 'paused': cs.gearStep = 3
  if problem == 'above_100': cs.vCruise = 101.
  c = lower.TestRayControllerCancel(); c.setup_method()
  c.cs.out.activateCruise, c.cs.out.gearStep = 4, cs.gearStep
  c.cs.out.cruiseState.speed = 50 / 3.6
  c.cc.hudControl.setSpeed = cs.vCruise / 3.6
  c.cc.cruiseControl.resume = grant(cs, **args)
  assert c.h.make_spam_button(c.cc, c.cs) == (lower.Buttons.RES_ACCEL if problem == 'valid' else 0)


def test_100_kph_float32_boundary_and_lost_grant():
  c = lower.TestRayControllerCancel(); c.setup_method()
  c.cs.out.activateCruise, c.cs.out.gearStep = 4, 7
  c.cs.out.cruiseState.speed = 99 / 3.6
  c.cc.hudControl.setSpeed = 27.77777862548828  # 100 km/h as a cereal Float32
  assert c.h.make_spam_button(c.cc, c.cs) == 0
  c.cc.cruiseControl.resume = True
  assert c.h.make_spam_button(c.cc, c.cs) == lower.Buttons.RES_ACCEL
  c.cs.out.cruiseState.speed = 100 / 3.6
  for _ in range(300):
    c.h.frame += 1
    assert c.h.make_spam_button(c.cc, c.cs) == 0


def test_automatic_resume_cannot_jump_to_retained_speed_above_100():
  p = upper.namespace['RayLeadPause']()
  p.phase, p.stock_kph = 'paused', 110.
  for _ in range(400):
    assert p.update(stock_active=False,stock_kph=0.,ego_kph=105.,need_pause=False,clear=True,valid=True) == 0
