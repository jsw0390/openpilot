"""Recorded factory/comma state disagreement; all I/O is memory-only."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import test_ray_manual_off as upper
import test_ray_controller_cancel as lower

ROOT = Path(__file__).resolve().parents[4]


def fixture(active=True):
  b = upper.TestRayManualOff()
  b.setup_method()
  b.cc.enabled = False
  b.helper._ray_ev_main_on = False
  b.helper._cruise_cancel_state = True
  b.helper.rayVisionIPedalAssist = 3
  b.cs.vEgo = 56.42 / 3.6
  b.helper.v_ego_kph_set = 59
  b.helper.v_cruise_kph = 60
  b.base_kph = 60
  b.cs.gearStep = 7 if active else 3
  b.cs.cruiseState.speed = 60 / 3.6 if active else 0
  b.helper.d_rel, b.helper.v_rel, b.helper.lead_prob = 23.65, -0.989, 0.9996
  b.helper.v_lead_kph = 56.42 - 0.989 * 3.6
  return b


def tick(b, count=1):
  return [b.tick()[0] for _ in range(count)]


@pytest.mark.parametrize('button', ['cancel', 'accelCruise', 'decelCruise'])
def test_factory_resume_after_restart_rearms_assist_only_after_feedback(button):
  b = fixture(False)
  b.helper._ray_ev_stock_sync_blocked = True
  b.tick(getattr(upper.BUTTONS, button), True)
  b.tick(getattr(upper.BUTTONS, button), False)
  assert 2 not in tick(b, 200)
  assert not b.helper._ray_ev_main_on  # no feedback: no synthesized MAIN
  b.cs.gearStep = 7
  b.cs.cruiseState.speed = 60 / 3.6
  assert tick(b, 19) == [0] * 19
  assert tick(b) == [3]  # ordinary following lowers the setting first
  assert b.helper._ray_ev_main_on and not b.helper._cruise_cancel_state


def test_recorded_60_kph_closing_lead_reduces_then_pauses_at_shorter_gap():
  b = fixture()
  requests = tick(b, 25)
  assert requests[:19] == [0] * 19
  assert requests[19:] == [3] * 6
  b.helper.d_rel = 18
  assert tick(b) == [-1]
  c = lower.TestRayControllerCancel()
  c.setup_method()
  c.cc.enabled = False
  c.cs.out.gearStep = 7
  c.cs.out.cruiseState.speed = 60 / 3.6
  c.cs.out.vEgo = b.cs.vEgo
  c.cs.out.activateCruise = -1
  emitted = []
  for _ in range(80):
    emitted.append(c.h.make_spam_button(c.cc, c.cs))
    c.h.frame += 1
  assert emitted.count(lower.Buttons.CANCEL) == 1
  assert set(emitted) <= {0, lower.Buttons.CANCEL}
  b.cs.gearStep = 3
  b.cs.cruiseState.speed = 0
  assert tick(b, 100) == [0] * 100
  assert b.helper._ray_lead_pause.phase == 'paused'


@pytest.mark.parametrize('manual', ['mainCruise', 'cancel', 'brakePressed', 'brakeHoldActive'])
def test_manual_off_cannot_be_overwritten_by_persistent_old_active_feedback(manual):
  b = fixture()
  b.helper._ray_ev_main_on = True
  b.helper._cruise_cancel_state = False
  if manual in ('brakePressed', 'brakeHoldActive'):
    setattr(b.cs, manual, True)
    b.tick()
    setattr(b.cs, manual, False)
  else:
    b.tick(getattr(upper.BUTTONS, manual), True)
    b.tick(getattr(upper.BUTTONS, manual), False)
  assert b.helper._ray_ev_stock_sync_blocked
  assert tick(b, 2000) == [0] * 2000
  assert b.helper._cruise_cancel_state
  assert b.helper._ray_lead_pause.phase == 'idle'


@pytest.mark.parametrize('bad', ['canValid', 'canTimeout', 'speed', 'unknown', 'park'])
def test_unconfirmed_factory_state_never_arms_assist(bad):
  b = fixture()
  if bad == 'canValid': b.cs.canValid = False
  elif bad == 'canTimeout': b.cs.canTimeout = True
  elif bad == 'speed': b.cs.cruiseState.speed = 0
  elif bad == 'unknown': b.cs.gearStep = 6
  else: b.cs.gearShifter = 'park'
  assert tick(b, 300) == [0] * 300
  assert not b.helper._ray_ev_main_on


def test_confirmation_dwell_restarts_on_bad_can():
  b = fixture()
  assert tick(b, 19) == [0] * 19
  b.cs.canValid = False
  assert tick(b) == [0]
  b.cs.canValid = True
  assert tick(b, 19) == [0] * 19
  assert tick(b) == [3]


def test_main_uses_factory_state_at_press_despite_opposite_software_latch():
  b = fixture()
  b.tick(upper.BUTTONS.mainCruise, True)  # factory ON, software OFF
  b.cs.gearStep = 3
  b.cs.cruiseState.speed = 0
  assert b.tick(upper.BUTTONS.mainCruise, False)[0] == -1
  assert not b.helper._ray_ev_main_on
  b.helper._ray_ev_main_on = True  # software still armed across a pause
  b.tick(upper.BUTTONS.mainCruise, True)  # factory OFF
  b.cs.gearStep = 7
  b.cs.cruiseState.speed = 60 / 3.6
  assert b.tick(upper.BUTTONS.mainCruise, False)[0] == 2
  assert b.helper._ray_ev_main_on


@pytest.mark.parametrize('button', ['accelCruise', 'decelCruise'])
def test_factory_active_rocker_does_not_request_engage_when_cc_disabled(button):
  b = fixture()
  b.helper._ray_ev_main_on = True
  assert b.tick(getattr(upper.BUTTONS, button), True)[0] == 0
  assert b.tick(getattr(upper.BUTTONS, button), False)[0] == 0


def test_observed_factory_on_does_not_take_ownership_of_later_manual_pause():
  b = fixture()
  b.helper.d_rel = b.helper.lead_prob = 0
  assert tick(b, 30) == [0] * 30
  assert b.helper._ray_ev_main_on
  b.cs.gearStep = 3
  b.cs.cruiseState.speed = 0
  b.helper._ray_resume_scene_clear = True
  assert tick(b, 2000) == [0] * 2000
  assert b.helper._ray_lead_pause.phase == 'idle'


# Exercise the actual publisher and controlsd target-selection blocks. Merely
# testing the cruise helper missed the recorded downstream 60 -> cached 73 bug.
card = ast.parse((ROOT / 'selfdrive/car/card.py').read_text())
publication = next(n for n in ast.walk(card) if isinstance(n, ast.If) and ast.unparse(n.test) == 'self.v_cruise_helper._paddle_decel_active')
publication_code = compile(ast.Module(body=[publication], type_ignores=[]), 'card-publication', 'exec')
controls = ast.parse((ROOT / 'selfdrive/controls/controlsd.py').read_text())
control_if = next(n for n in ast.walk(controls) if isinstance(n, ast.If) and ast.unparse(n.test) == 'is_ray_ev' and any(isinstance(x, ast.Assign) and ast.unparse(x.targets[0]) == 'base_cruise_kph' for x in n.body))
control_code = compile(ast.Module(body=control_if.body, type_ignores=[]), 'controlsd-ray-target', 'exec')


def publish(b, cached=73, target=60, request=0):
  h = b.helper
  h.v_cruise_kph = h.v_cruise_cluster_kph = target
  h._activate_cruise = request
  obj = NS(v_cruise_helper=h, sm={'carControl':b.cc}, ray_ev_pause_v_cruise_kph=cached)
  scope = {'self':obj, 'CS':b.cs, 'V_CRUISE_UNSET':255}
  exec(publication_code, scope)
  return scope['v_cruise_kph'], scope['v_cruise_cluster_kph'], obj.ray_ev_pause_v_cruise_kph


def test_recorded_60_stock_overrides_cached_73_even_when_acc_req_and_cc_off():
  b = fixture()
  assert publish(b) == (60, 60, 60)
  b.cs.cruiseState.speed = 61 / 3.6
  assert publish(b, cached=60) == (61, 61, 61)
  b.cs.gearStep = 3
  b.cs.cruiseState.speed = 0
  assert publish(b, cached=61, target=90) == (61, 61, 61)


def test_unacknowledged_enable_never_overwrites_display_cache():
  b = fixture(False)
  assert publish(b, cached=60, target=73, request=2) == (73, 60, 60)
  assert publish(b, cached=255, target=73, request=2)[1:] == (255, 255)


def test_restore_target_stays_separate_from_displayed_acknowledged_setting():
  b = fixture()
  b.cs.cruiseState.speed = 40 / 3.6
  internal, displayed, cached = publish(b, target=60, cached=73, request=4)
  assert (internal, displayed, cached) == (60, 40, 40)
  b.cs.vCruise, b.cs.vCruiseCluster, b.cs.activateCruise = internal, displayed, 4
  hud = NS()
  controller = NS(params=NS(get_int=lambda _:2), _ray_lead_target_speed=lambda *args:None)
  exec(control_code, {'self':controller, 'CS':b.cs, 'hudControl':hud, 'CV':NS(MS_TO_KPH=3.6,KPH_TO_MS=1/3.6)})
  assert hud.setSpeed * 3.6 == pytest.approx(60)
  b.cs.activateCruise = 0
  exec(control_code, {'self':controller, 'CS':b.cs, 'hudControl':hud, 'CV':NS(MS_TO_KPH=3.6,KPH_TO_MS=1/3.6)})
  assert hud.setSpeed * 3.6 == pytest.approx(40)


@pytest.mark.parametrize('button', ['mainCruise', 'cancel', 'accelCruise', 'decelCruise'])
def test_physical_set_while_accelerating_waits_for_release_and_factory_ack(button):
  b = fixture(False)
  b.helper._ray_ev_stock_sync_blocked = True
  b.cs.gasPressed = True
  assert b.tick(getattr(upper.BUTTONS, button), True)[0] <= 0
  assert b.tick(getattr(upper.BUTTONS, button), False)[0] <= 0
  b.cs.gearStep = 7
  b.cs.cruiseState.speed = 60 / 3.6
  assert tick(b, 150) == [0] * 150
  assert not b.helper._ray_ev_main_on
  b.cs.gasPressed = False
  requests = tick(b, 125)
  assert 2 not in requests
  assert 3 in requests  # only now may assist change the already-active cruise
