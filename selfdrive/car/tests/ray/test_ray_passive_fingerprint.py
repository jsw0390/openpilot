"""Run actual fingerprint and card decision code with memory-only CAN callbacks."""

import ast
import os
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch  # noqa: TID251 - mock utilities only; tests use pytest

ROOT = Path(__file__).resolve().parents[4]
HELPERS = ROOT / 'opendbc_repo/opendbc/car/car_helpers.py'
CARD = ROOT / 'selfdrive/car/card.py'


def compile_nodes(nodes, path):
  future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
  return compile(ast.fix_missing_locations(ast.Module(body=[future] + nodes, type_ignores=[])), str(path), 'exec')


class TestRayPassiveFingerprint:
  def setup_method(self):
    tree = ast.parse(HELPERS.read_text())
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('fingerprint', 'get_car')]
    self.recv, self.send, self.mux = Mock(), Mock(), Mock()
    self.finger = {0: {832: 8, 1265: 4}, 1: {}, 2: {832: 8}}
    self.scope = dict(
      os=os,
      time=NS(monotonic=lambda: 0),
      carlog=Mock(),
      VIN_UNKNOWN='0' * 17,
      is_valid_vin=lambda vin: len(vin) == 17,
      CarParams=NS(FingerprintSource=NS(can='can', fw='fw', fixed='fixed')),
      get_vin=Mock(return_value=(0x7E8, 0, 'TESTVIN0000000000')),
      get_present_ecus=Mock(return_value=set()),
      get_fw_versions_ordered=Mock(return_value=[]),
      match_fw_to_car=Mock(return_value=(True, set())),
      can_fingerprint=Mock(return_value=('KIA_RAY_EV', self.finger)),
    )
    exec(compile_nodes(functions, HELPERS), self.scope)
    self.env = patch.dict(os.environ, {}, clear=True)
    self.env.start()

  def teardown_method(self):
    self.env.stop()

  def fingerprint(self, **kwargs):
    return self.scope['fingerprint'](self.recv, self.send, self.mux, 1, None, **kwargs)

  def test_passive_option_never_queries_or_enables_obd(self):
    result = self.fingerprint(query_fw=False)
    for name in ('get_vin', 'get_present_ecus', 'get_fw_versions_ordered', 'match_fw_to_car'):
      self.scope[name].assert_not_called()
    self.send.assert_not_called()
    self.mux.assert_called_once_with(False)
    self.scope['can_fingerprint'].assert_called_once_with(self.recv)
    assert (result) == (('KIA_RAY_EV', self.finger, '0' * 17, [], 'can', True))

  def test_default_identification_still_queries_vin_and_firmware(self):
    self.fingerprint()
    self.scope['get_vin'].assert_called_once_with(self.recv, self.send, (0, 1))
    self.scope['get_present_ecus'].assert_called_once()
    self.scope['get_fw_versions_ordered'].assert_called_once()
    assert ([c.args[0] for c in self.mux.call_args_list]) == ([True, False])

  def test_existing_skip_environment_remains_supported(self):
    os.environ['SKIP_FW_QUERY'] = '1'
    self.fingerprint()
    self.scope['get_vin'].assert_not_called()
    self.mux.assert_called_once_with(False)

  def test_get_car_forwards_query_choice_without_changing_interface(self):
    cp = NS(carFingerprint='KIA_RAY_EV')
    interface = Mock()
    interface.get_params.return_value = cp
    interface.return_value = 'interface'
    self.scope.update(Params=Mock(return_value=Mock(get=Mock(return_value=None))), interfaces={'KIA_RAY_EV': interface})
    result = self.scope['get_car'](self.recv, self.send, self.mux, True, True, query_fw=False)
    assert (result) == ('interface')
    self.scope['get_vin'].assert_not_called()
    interface.get_params.assert_called_once_with('KIA_RAY_EV', self.finger, [], True, True, docs=False)

  def card_choice(self, enabled, selected, can0_startup=False):
    tree = ast.parse(CARD.read_text())
    assignments = {ast.unparse(n.targets[0]): n for n in ast.walk(tree) if isinstance(n, ast.Assign) and len(n.targets) == 1}
    h = NS(params=NS(get_bool=lambda _: enabled, get=lambda _: selected), can_callbacks=('rx', 'tx'))
    factory = Mock()
    scope = dict(self=h, get_car=factory, obd_callback=lambda _: 'mux', alpha_long_allowed=True,
                 is_release=True, num_pandas=1, cached_params=None, ray_camera_diagnostics=False,
                 ray_can0_startup=can0_startup)
    exec(compile_nodes([assignments['query_fw'], assignments['self.CI']], CARD), scope)
    return factory.call_args.kwargs

  def test_manually_selected_passive_ray_skips_queries(self):
    assert not (self.card_choice(False, 'Kia Ray EV')['query_fw'])

  def test_active_ray_keeps_queries(self):
    assert self.card_choice(True, 'Kia Ray EV')['query_fw']

  def test_other_or_unselected_passive_vehicles_keep_identification(self):
    for selected in (None, 'MOCK', 'Hyundai Ioniq 5', 'Kia Ray'):
      assert self.card_choice(False, selected)['query_fw']

  def test_opt_in_ray_can0_startup_queries_without_obd_bus_one(self):
    choice = self.card_choice(False, 'Kia Ray EV', can0_startup=True)
    assert choice['query_fw']
    assert choice['query_bus0_only']
