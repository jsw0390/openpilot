"""Exercise real message/initialization blocks with recording-only packers."""

import ast
from enum import IntFlag
from types import SimpleNamespace as NS
from unittest.mock import Mock  # noqa: TID251

import pytest

import test_ray_controller_cancel as lower
from test_ray_passive_fingerprint import ROOT, compile_nodes


class Flags(IntFlag):
  CANFD = 1
  CANFD_CAMERA_SCC = 2
  CANFD_HDA2 = 4
  CAMERA_SCC = 8
  SEND_LFA = 16
  USE_FCA = 32
  ENABLE_BLINKERS = 64


PATH = ROOT / 'opendbc_repo/opendbc/car/hyundai/carcontroller.py'
cls = next(n for n in ast.parse(PATH.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'CarController')


def method(name):
  return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)


@pytest.mark.parametrize('activation,stock_active,enabled', [(-1, True, False), (2, False, True)])
def test_button_pack_path_sends_one_toggle_without_a_second_cancel_path(activation, stock_active, enabled):
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.h.packer, b.cs.clu11 = object(), {}
  b.cc.enabled = enabled
  b.cs.out.activateCruise = activation
  b.cs.out.gearStep = 7 if stock_active else 3
  packer = Mock()
  packer.create_clu11_button.return_value = 'recorded toggle'
  scope = dict(lower.env, hyundaican=packer)
  exec(compile_nodes([method('create_button_messages')], PATH), scope)
  sent = []
  for _ in range(1000):
    sent.extend(scope['create_button_messages'](b.h, b.cc, b.cs, True))
    b.h.frame += 1
  assert sent == ['recorded toggle']
  packer.create_clu11.assert_not_called()
  packer.create_clu11_button.assert_called_once()


def test_button_pack_path_clears_pending_resume_during_brake_hold():
  b = lower.TestRayControllerCancel()
  b.setup_method()
  b.cs.out.brakeHoldActive = True
  b.h.ray_ev_pending_resume_until = b.h.frame + 10
  scope = dict(lower.env, hyundaican=Mock())
  exec(compile_nodes([method('create_button_messages')], PATH), scope)
  assert scope['create_button_messages'](b.h, b.cc, b.cs, True) == []
  assert b.h.ray_ev_pending_resume_until == -1


@pytest.mark.parametrize('ray', [True, False])
@pytest.mark.parametrize('camera_scc', [True, False])
def test_ray_sends_lkas_and_buttons_but_no_direct_accel_or_ecu_disable(ray, camera_scc):
  body = method('update').body
  direct = next(n for n in body if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'direct_longitudinal')
  start = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'can_sends')
  end = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'new_actuators')
  hyundai = Mock()
  hyundai.create_lkas11.return_value = 'steer'
  hyundai.create_acc_commands.return_value = ['accel']
  hyundai.create_acc_commands_scc.return_value = ['camera accel']
  hyundai.create_acc_opt.return_value = ['acc option']
  hyundai.create_frt_radar_opt.return_value = 'radar option'
  cp = NS(carFingerprint='KIA_RAY_EV' if ray else 'OTHER', openpilotLongitudinalControl=True,
          flags=Flags.CAMERA_SCC if camera_scc else Flags(0))
  h = NS(CP=cp, frame=0, CAN=NS(ECAN=0), lkas11_active=True, is_ldws_car=False, packer=object(),
         create_button_messages=Mock(return_value=['button']), hyundai_jerk=Mock(), soft_hold_mode=0)
  cs = NS(lkas11={}, scc13=None)
  scope = dict(self=h, CC=NS(enabled=True, cruiseControl=NS(override=False)), CS=cs,
               CAR=NS(KIA_RAY_EV='KIA_RAY_EV', HYUNDAI_CASPER_EV='CASPER'), HyundaiFlags=Flags,
               CAN_GEARS={'send_mdps12': set()}, hyundaican=hyundai,
               make_tester_present_msg=Mock(return_value='tester'), apply_torque=0, apply_steer_req=False,
               torque_fault=False, sys_warning=False, sys_state=0, lateral_hud_enabled=True,
               hud_control=NS(leftLaneVisible=True, rightLaneVisible=True), left_lane_warning=0, right_lane_warning=0,
               accel=1.0, actuators=NS(aTarget=1.0), stopping=False, set_speed_in_units=40)
  exec(compile_nodes([direct] + body[start:end], PATH), scope)
  assert 'steer' in scope['can_sends']
  if ray:
    assert scope['can_sends'] == ['steer', 'button']
    scope['make_tester_present_msg'].assert_not_called()
    hyundai.create_acc_commands.assert_not_called()
    hyundai.create_acc_commands_scc.assert_not_called()
    hyundai.create_acc_opt.assert_not_called()
    hyundai.create_frt_radar_opt.assert_not_called()
  else:
    assert ('camera accel' if camera_scc else 'accel') in scope['can_sends']


@pytest.mark.parametrize('ray', [True, False])
def test_ray_interface_preserves_stock_ecus(ray):
  path = ROOT / 'opendbc_repo/opendbc/car/hyundai/interface.py'
  cls = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'CarInterface')
  init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'init')
  init.decorator_list = []
  scope = dict(CAR=NS(KIA_RAY_EV='KIA_RAY_EV'), HyundaiFlags=Flags, Params=Mock(return_value=Mock(get_int=Mock(return_value=1))),
               disable_ecu=Mock(), enable_radar_tracks=Mock())
  exec(compile_nodes([init], path), scope)
  scope['init'](NS(carFingerprint='KIA_RAY_EV' if ray else 'OTHER', openpilotLongitudinalControl=True, flags=Flags(0)), Mock(), Mock())
  assert scope['disable_ecu'].call_count == (0 if ray else 1)
  assert scope['enable_radar_tracks'].call_count == (0 if ray else 1)
