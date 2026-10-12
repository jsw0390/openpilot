"""Query routing isolation. All CAN transports and firmware responses are mocks."""

import ast
from types import SimpleNamespace as NS
from unittest.mock import Mock  # noqa: TID251

import pytest

import test_ray_passive_fingerprint as fingerprint_harness
from test_ray_passive_fingerprint import CARD, ROOT, compile_nodes


class TestCameraFingerprint:
  setup_method = fingerprint_harness.TestRayPassiveFingerprint.setup_method
  teardown_method = fingerprint_harness.TestRayPassiveFingerprint.teardown_method
  fingerprint = fingerprint_harness.TestRayPassiveFingerprint.fingerprint

  def test_camera_query_never_selects_obd_or_vin_bus_one(self):
    self.fingerprint(query_bus0_only=True)
    self.scope['get_vin'].assert_called_once_with(self.recv, self.send, (0,))
    assert [c.args[0] for c in self.mux.call_args_list] == [False, False]
    for name in ('get_present_ecus', 'get_fw_versions_ordered'):
      assert self.scope[name].call_args.kwargs['query_bus0_only']

  def test_camera_query_does_not_reuse_a_previous_obd_cache(self):
    cached = NS(brand='hyundai', carFw=['old'], carVin='TESTVIN0000000000')
    self.scope['fingerprint'](self.recv, self.send, self.mux, 1, cached, query_bus0_only=True)
    self.scope['get_vin'].assert_called_once()
    self.scope['get_present_ecus'].assert_called_once()

  def test_passive_query_disable_wins_over_camera_bus_option(self):
    self.fingerprint(query_fw=False, query_bus0_only=True)
    self.scope['get_vin'].assert_not_called()
    self.send.assert_not_called()


class TestFirmwareBuses:
  def setup_method(self):
    path = ROOT / 'opendbc_repo/opendbc/car/fw_versions.py'
    names = {'get_present_ecus', 'get_fw_versions', 'get_fw_versions_ordered'}
    nodes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name in names]
    config = NS(get_all_ecus=lambda versions: [('engine', 0x700, None), ('engine', 0x701, 1)], extra_ecus=[])
    requests = [('hyundai', config, NS(bus=bus, obd_multiplexing=mux, whitelist_ecus=[], rx_offset=8,
                                     request=[b'\x22\xf1\x00'], response=[b'\x62\xf1\x00'], logging=False))
                for bus, mux in [(0, True), (1, True), (1, False), (2, False), (4, True)]]
    self.sent, self.present = [], []

    def query(send, recv, bus, addrs, *args):
      self.sent.append(bus)
      return NS(get_data=lambda timeout: dict.fromkeys(addrs, b'version'))

    def present(recv, send, queries, responses, timeout):
      self.present.extend(queries)
      return {(addr + 8, sub, bus) for addr, sub, bus in queries}

    self.scope = dict(
      VERSIONS={'hyundai': {}}, FW_QUERY_CONFIGS={'hyundai': config}, REQUESTS=requests,
      uds=NS(get_rx_addr_for_tx_addr=lambda addr, offset: addr + offset),
      get_ecu_addrs=present, IsoTpParallelQuery=query, tqdm=lambda values, **kwargs: values,
      chunks=lambda values: [values], is_brand=lambda brand, selected: selected is None or selected == brand,
      CarParams=NS(CarFw=NS), Ecu=NS(unknown='unknown'),
      carlog=NS(exception=Mock(side_effect=AssertionError('query raised'))),
      get_brand_ecu_matches=lambda addresses: {'hyundai': [True]},
      match_fw_to_car=lambda fw, vin, **kwargs: (True, {'KIA_RAY_EV'}),
    )
    exec(compile_nodes(nodes, path), self.scope)
    self.mux = Mock()

  def test_present_ecus_only_bus_zero_and_no_obd_routing(self):
    responses = self.scope['get_present_ecus'](Mock(), Mock(), self.mux, 2, query_bus0_only=True)
    assert {bus for _, _, bus in self.present} == {0}
    assert {bus for _, _, bus in responses} == {0}
    assert [c.args[0] for c in self.mux.call_args_list] == [False]

  def test_firmware_queries_skip_all_other_buses(self):
    fw = self.scope['get_fw_versions'](Mock(), Mock(), self.mux, num_pandas=2, query_bus0_only=True)
    assert self.sent == [0, 0]
    assert len(fw) == 2
    assert all(f.bus == 0 and not f.obdMultiplexing for f in fw)
    assert True not in [c.args[0] for c in self.mux.call_args_list]

  def test_ordered_query_forwards_restriction(self):
    fw = self.scope['get_fw_versions_ordered'](Mock(), Mock(), self.mux, 'TESTVIN', set(), num_pandas=2, query_bus0_only=True)
    assert len(fw) == 2
    assert set(self.sent) == {0}

  def test_default_keeps_other_buses_and_obd_identification(self):
    self.scope['get_fw_versions'](Mock(), Mock(), self.mux, num_pandas=2)
    assert set(self.sent) == {0, 1, 2, 4}
    assert True in [c.args[0] for c in self.mux.call_args_list]


@pytest.mark.parametrize('selected,pandas,allowed', [
  ('Kia Ray EV', ['cuatro'], True), ('Kia Ray EV', ['tres'], False),
  ('Kia Ray EV', [], False), ('Kia Ray EV', ['cuatro', 'cuatro'], False),
  ('Hyundai Ioniq 5', ['cuatro'], False), (None, ['cuatro'], False),
])
def test_diagnostic_guard_requires_explicit_ray_and_one_comma_four(selected, pandas, allowed):
  tree = ast.parse(CARD.read_text())
  node = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and ast.unparse(n.test) == 'ray_can0_startup')
  scope = dict(ray_can0_startup=True, self=NS(params=NS(get=lambda key: selected)),
               num_pandas=len(pandas), panda_states=[NS(pandaType=p) for p in pandas], cloudlog=Mock())
  if allowed:
    exec(compile_nodes([node], CARD), scope)
  else:
    with pytest.raises(RuntimeError):
      exec(compile_nodes([node], CARD), scope)
