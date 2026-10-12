"""Following decisions and stock-only resume authorization; no vehicle I/O."""
from types import SimpleNamespace as NS
import pytest
import test_ray_manual_off as upper
import test_ray_controller_cancel as lower
from test_ray_lead_pause import paused, update

scene_clear = upper.namespace['ray_resume_scene_clear']
resume_allowed = upper.namespace['ray_stock_resume_allowed']


@pytest.mark.parametrize('ego,stock,distance,relative,expected', [
  (56.51, 60, 45.0, -5.01, 3),
  (61.72, 63, 42.45, -2.32, 3),
  (40.10, 60, 16.36, -0.11, 3),
  (50, 60, 25, 0, 3),
  (50, 50, 60, 0, 0),
  (50, 50, 25, 2, 0),
  (56.51, 60, 20, -5.01, -1),
  (31.14, 60, 10.83, -0.56, -1),
  (60, 60, 60, -60 / 3.6, -1),
])
def test_distance_and_relative_speed_choose_reduction_hold_or_pause(ego, stock, distance, relative, expected):
  b=upper.TestRayManualOff();b.setup_method();h=b.helper
  h.rayVisionIPedalAssist=3;h.v_ego_kph_set=ego
  h.d_rel=distance;h.v_rel=relative;h.v_lead_kph=max(0,ego+relative*3.6);h.lead_prob=.99
  b.cs.vEgo=ego/3.6;b.cs.gearStep=7;b.cs.cruiseState.speed=stock/3.6
  h._update_ray_ipedal_assist(b.cs,b.cc,stock)
  assert h._activate_cruise==expected
  assert h._lat_enabled


def scene():
  lead=NS(status=True,dRel=60.,vRel=2.,vLeadK=65/3.6,modelProb=.99)
  radar=NS(leadOne=lead,leadTwo=NS(status=False))
  model=NS(leadsV3=[NS(prob=.99,x=[60.])],action=NS(shouldStop=False,desiredVelocity=62/3.6))
  return radar,model


def test_stably_distant_faster_lead_can_resume_owned_pause():
  radar,model=scene();p=paused()
  clear=scene_clear(48/3.6,50,radar,model)
  assert clear
  assert update(p,299,clear=clear)==[0]*299
  assert update(p,clear=clear)==[2]


@pytest.mark.parametrize('problem', ['near','closing','slower','uncertain','mismatched_model','model_stop','second_lead','turn'])
def test_present_lead_resume_requires_distance_speed_and_model_agreement(problem):
  radar,model=scene()
  if problem=='near':radar.leadOne.dRel=20
  if problem=='closing':radar.leadOne.vRel=-.1
  if problem=='slower':radar.leadOne.vLeadK=50/3.6
  if problem=='uncertain':model.leadsV3[0].prob=.5
  if problem=='mismatched_model':model.leadsV3[0].x=[20]
  if problem=='model_stop':model.action.shouldStop=True
  if problem=='second_lead':radar.leadTwo=NS(status=True,dRel=15.,vRel=-1.,vLeadK=40/3.6,modelProb=.99)
  assert not scene_clear(48/3.6,50,radar,model,turning=problem=='turn')


def resume_fixture():
  cs=NS(activateCruise=2,canValid=True,canTimeout=False,gearShifter='drive',gearStep=3,vEgo=48/3.6,
        vCruiseCluster=50.,gasPressed=False,brakePressed=False,brakeHoldActive=False)
  args=dict(passive=False,inputs_valid=True,panda_ok=True,events=[],monitor=NS(isActiveMode=True,awarenessStatus=1.))
  return cs,args


@pytest.mark.parametrize('problem', ['valid','stale','panda','passive','dm_off','distracted','brake','gas','park','low_speed','unknown','no_request','no_entry'])
def test_stock_resume_grant_keeps_entry_and_driver_guards(problem):
  cs,args=resume_fixture()
  if problem=='stale':args['inputs_valid']=False
  if problem=='panda':args['panda_ok']=False
  if problem=='passive':args['passive']=True
  if problem=='dm_off':args['monitor'].isActiveMode=False
  if problem=='distracted':args['monitor'].awarenessStatus=0
  if problem=='brake':cs.brakePressed=True
  if problem=='gas':cs.gasPressed=True
  if problem=='park':cs.gearShifter='park'
  if problem=='low_speed':cs.vEgo=29/3.6
  if problem=='unknown':cs.gearStep=5
  if problem=='no_request':cs.activateCruise=0
  if problem=='no_entry':args['events']=[NS(noEntry=True,userDisable=False,softDisable=False,immediateDisable=False,name='doorOpen')]
  assert resume_allowed(cs,**args)==(problem=='valid')


def test_granted_resume_reaches_lower_controller_with_direct_controls_disabled():
  b=lower.TestRayControllerCancel();b.setup_method()
  b.cc.enabled=False;b.cs.out.gearStep=3;b.cs.out.activateCruise=2
  assert b.h.make_spam_button(b.cc,b.cs)==0
  cs,args=resume_fixture();b.cc.cruiseControl.resume=resume_allowed(cs,**args)
  b.h.frame+=1
  assert b.h.make_spam_button(b.cc,b.cs)==lower.Buttons.CANCEL
  for _ in range(100):
    b.h.frame+=1
    assert b.h.make_spam_button(b.cc,b.cs)==0


def test_unacknowledged_pause_reports_failure_without_repeating_toggle():
  b=upper.TestRayManualOff();b.setup_method();h=b.helper
  h.rayVisionIPedalAssist=3;h.v_ego_kph_set=50;h.d_rel=15;h.v_rel=-3;h.lead_prob=.99
  b.cs.vEgo=50/3.6;b.cs.gearStep=7;b.cs.cruiseState.speed=50/3.6
  for _ in range(105):
    h._activate_cruise=0;h._update_ray_ipedal_assist(b.cs,b.cc,50)
  assert h._ray_lead_pause.phase=='lockout'
  assert h.messages[-1]=='Ray lead: pause not acknowledged'


@pytest.mark.parametrize('problem', ['valid','old_events','stale_model','disabled_dm','panda'])
def test_controlsd_grants_resume_without_enabling_direct_control(problem):
  import ast
  from pathlib import Path
  tree=ast.parse((upper.ROOT/'selfdrive/controls/controlsd.py').read_text())
  block=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and
             any(isinstance(c,ast.Call) and isinstance(c.func,ast.Name) and c.func.id=='ray_stock_resume_allowed' for c in ast.walk(n)) and
             isinstance(n.test,ast.Compare))
  cs,args=resume_fixture()
  class Inputs(dict):
    recv_time=dict.fromkeys(['carState','selfdriveState','driverMonitoringState','onroadEvents','radarState','modelV2','carrotMan','pandaStates'],10.)
    def all_checks(self, feeds):return True
  sm=Inputs(onroadEvents=[],driverMonitoringState=args['monitor'],pandaStates=[])
  sm.recv_time=dict(sm.recv_time,onroadEvents=9.2)
  if problem=='old_events':sm.recv_time['onroadEvents']=8.
  if problem=='stale_model':sm.recv_time['modelV2']=9.7
  owner=NS(sm=sm,CP=NS(carFingerprint='KIA_RAY_EV',passive=False),params=NS(get_int=lambda _:int(problem=='disabled_dm')))
  cc=NS(enabled=False,longActive=False,cruiseControl=NS(resume=False))
  exec(compile(ast.Module(body=[block],type_ignores=[]),'resume_gate','exec'),dict(
    self=owner,CS=cs,CC=cc,time=NS(monotonic=lambda:10.),ray_stock_resume_allowed=resume_allowed,
    panda_lateral_ready=lambda *_:problem!='panda'))
  assert cc.cruiseControl.resume==(problem=='valid')
  assert not cc.enabled and not cc.longActive
