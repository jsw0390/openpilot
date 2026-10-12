"""Read-only UI estimates must not confuse closing speed or set speed with lead speed."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('lead_speed', ROOT / 'selfdrive/ui/onroad/lead_speed.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Inputs(dict):
  def __init__(self):
    super().__init__(radarState=NS(leadOne=NS(status=True, vLeadK=45 / 3.6, vRel=-5., dRel=35., modelProb=.99)))
    self['modelV2'] = NS(leadsV3=[NS(prob=.99, x=[36.5])])
    self.alive = dict.fromkeys(('radarState', 'modelV2'), True)
    self.valid = self.alive.copy()
    self.recv_time = dict.fromkeys(self.alive, 10.)
    self.recv_frame = dict.fromkeys(self.alive, 101)


def test_absolute_speed_and_units_with_cruise_disengaged():
  sm = Inputs()  # No cruise state or setpoint is needed.
  assert module.lead_speed_text(sm, True, 100, 10.1) == '45 km/h'
  assert module.lead_speed_text(sm, False, 100, 10.1) == '28 mph'
  sm['radarState'].leadOne.vLeadK = -.1
  assert module.lead_speed_text(sm, True, 100, 10.1) == '0 km/h'


@pytest.mark.parametrize('service', ['radarState', 'modelV2'])
@pytest.mark.parametrize('problem', ['stale', 'invalid', 'dead', 'previous_drive', 'future'])
def test_old_or_invalid_data_is_not_displayed(service, problem):
  sm = Inputs()
  if problem == 'stale': sm.recv_time[service] = 9.49
  if problem == 'invalid': sm.valid[service] = False
  if problem == 'dead': sm.alive[service] = False
  if problem == 'previous_drive': sm.recv_frame[service] = 99
  if problem == 'future': sm.recv_time[service] = 11.
  assert module.lead_speed_text(sm, True, 100, 10.) is None


@pytest.mark.parametrize('field,value', [
  ('status', False), ('modelProb', .5), ('modelProb', float('nan')),
  ('dRel', 0), ('dRel', 201), ('dRel', float('inf')),
  ('vLeadK', float('nan')), ('vLeadK', float('inf')), ('vLeadK', -5), ('vLeadK', 101),
])
def test_missing_uncertain_or_nonphysical_lead_has_no_number(field, value):
  sm = Inputs()
  setattr(sm['radarState'].leadOne, field, value)
  assert module.lead_speed_text(sm, True, 100, 10.) is None


@pytest.mark.parametrize('problem', ['lost', 'low_probability', 'missing_distance', 'different_lead', 'nan'])
def test_extrapolated_or_mismatched_lead_is_hidden(problem):
  sm = Inputs()
  model = sm['modelV2']
  if problem == 'lost': model.leadsV3 = []
  if problem == 'low_probability': model.leadsV3[0].prob = .3
  if problem == 'missing_distance': model.leadsV3[0].x = []
  if problem == 'different_lead': model.leadsV3[0].x = [70.]
  if problem == 'nan': model.leadsV3[0].x = [float('nan')]
  assert module.lead_speed_text(sm, True, 100, 10.) is None


@pytest.mark.parametrize('problem', ['none', 'offroad', 'alert', 'setting_off', 'lost'])
def test_badge_hides_for_alerts_off_setting_and_lost_lead(problem):
  tree = ast.parse((ROOT / 'selfdrive/ui/mici/onroad/hud_renderer.py').read_text())
  method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_draw_lead_speed')
  draws = []
  state = NS(started=problem != 'offroad', show_radar_info=0 if problem == 'setting_off' else 1,
             sm=Inputs(), is_metric=True, started_frame=100)
  if problem == 'lost': state.sm['radarState'].leadOne.status = False
  env = dict(rl=NS(Rectangle=lambda x,y,w,h: NS(x=x,y=y,width=w,height=h),
                   Color=lambda *args: args, WHITE=(255,255,255,255), draw_rectangle_rounded=lambda *args: None),
             ui_state=state, time=NS(monotonic=lambda:10.), lead_speed_text=module.lead_speed_text,
             draw_text_ui_style=lambda text,*args,**kwargs: draws.append(text))
  exec(compile(ast.Module(body=[method], type_ignores=[]), 'badge', 'exec'), env)
  env['_draw_lead_speed'](NS(_can_draw_top_icons=problem != 'alert', _font_display=None),
                          NS(x=0,y=0,width=476,height=240))
  assert draws == (['앞차 추정', '45 km/h'] if problem == 'none' else [])
