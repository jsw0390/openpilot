"""Check independent steering selection and real HUD bytes without sending CAN."""

import ast
import importlib.util
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock  # noqa: TID251

import pytest

from test_ray_passive_fingerprint import ROOT, compile_nodes
from test_ray_stock_message_paths import Flags, PATH, method


HUD_PATH = ROOT / 'opendbc_repo/opendbc/car/hyundai/hyundaican.py'
hud_function = next(n for n in ast.parse(HUD_PATH.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'create_lfahda_mfc')
scope = {}
exec(compile_nodes([hud_function], HUD_PATH), scope)
create_hud = scope['create_lfahda_mfc']


def control(cruise, active):
  return NS(enabled=cruise, latActive=active, hudControl=NS(activeCarrot=0))


@pytest.mark.parametrize('cruise', [False, True])
@pytest.mark.parametrize('selected', [False, True])
@pytest.mark.parametrize('active', [False, True])
def test_ray_selection_is_independent_of_cruise_and_only_active_is_green(cruise, selected, active):
  packer = Mock()
  create_hud(packer, control(cruise, active), False, selected)
  name, bus, values = packer.make_can_msg.call_args.args
  assert (name, bus) == ('LFAHDA_MFC', 0)
  assert values['LFA_Icon_State'] == (2 if active else int(selected))
  assert values['HDA_Icon_Wheel'] == int(active)
  assert values['HDA_Icon_State'] == 0


@pytest.mark.parametrize('cruise', [False, True])
@pytest.mark.parametrize('active', [False, True])
def test_other_vehicles_preserve_existing_hud(cruise, active):
  packer = Mock()
  create_hud(packer, control(cruise, active), False)
  assert packer.make_can_msg.call_args.args[2]['LFA_Icon_State'] == (2 if active else int(cruise))


@pytest.mark.parametrize('ray', [False, True])
@pytest.mark.parametrize('selected', [False, True])
@pytest.mark.parametrize('frame,send_lfa,expected', [(0, True, 1), (1, True, 0), (5, True, 1), (0, False, 0)])
def test_controller_passes_ray_selection_at_existing_rate(ray, selected, frame, send_lfa, expected):
  block = next(n for n in ast.walk(method('update')) if isinstance(n, ast.If)
               and 'self.frame % 5 == 0' in ast.unparse(n.test)
               and 'HyundaiFlags.SEND_LFA.value' in ast.unparse(n.test))
  hyundai = Mock()
  cp = NS(carFingerprint='KIA_RAY_EV' if ray else 'OTHER', flags=Flags.SEND_LFA if send_lfa else Flags(0))
  h = NS(CP=cp, frame=frame, packer=object(), blinking_signal=False)
  cs = NS(out=NS(latEnabled=selected)) if ray else NS()
  cc = control(False, False)
  env = dict(self=h, CC=cc, CS=cs, can_sends=[], HyundaiFlags=Flags,
             CAR=NS(KIA_RAY_EV='KIA_RAY_EV'), hyundaican=hyundai)
  exec(compile_nodes([block], PATH), env)
  assert len(env['can_sends']) == expected
  assert hyundai.create_lfahda_mfc.call_count == expected
  if expected:
    hyundai.create_lfahda_mfc.assert_called_once_with(h.packer, cc, False, selected if ray else None)


@pytest.mark.skipif(sys.platform != 'linux', reason='Requires the device native CAN packer')
@pytest.mark.parametrize('cruise,selected,active,icon', [
  (False, True, False, 1), (True, True, False, 1),
  (False, False, False, 0), (True, False, False, 0), (False, True, True, 2),
])
def test_native_lfa_icon_bytes(cruise, selected, active, icon):
  from opendbc.can.packer import CANPacker

  spec = importlib.util.spec_from_file_location('ray_candidate_hyundaican', HUD_PATH)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  address, data, bus = module.create_lfahda_mfc(CANPacker('hyundai_kia_generic'), control(cruise, active), False, selected)
  assert (address, bus, len(data)) == (0x485, 0, 8)
  assert data[3] & 3 == icon
  assert (data[2] >> 4) & 1 == int(active)
