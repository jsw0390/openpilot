"""Exercise real card initialization/step bodies with memory-only I/O."""

import ast
import pytest
from pathlib import Path
from types import SimpleNamespace as NS

PATH = Path(__file__).resolve().parents[4] / 'selfdrive/car/card.py'
tree = ast.parse(PATH.read_text())
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Car')
init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
start = next(i for i, n in enumerate(init.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'self.CP.alternativeExperience')
stop = next(i for i, n in enumerate(init.body[start:], start) if isinstance(n, ast.If) and ast.unparse(n.test) == 'self.CP.secOcRequired')
write_start = next(i for i, n in enumerate(init.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'cp_bytes')
write_stop = next(
  i + 1
  for i, n in enumerate(init.body)
  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and any(isinstance(a, ast.Constant) and a.value == 'CarParamsPersistent' for a in n.value.args)
)
code = compile(ast.fix_missing_locations(ast.Module(body=init.body[start:stop] + init.body[write_start:write_stop], type_ignores=[])), str(PATH), 'exec')
step = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'step')
env = {'EventName': NS(selfdriveInitializing='initializing')}
exec(compile(ast.fix_missing_locations(ast.Module(body=[step], type_ignores=[])), str(PATH), 'exec'), env)


class Params:
  def __init__(self, enabled):
    self.enabled, self.calls = enabled, []

  def get_bool(self, key):
    return self.enabled

  def put(self, key, value):
    self.calls.append((key, value))

  put_bool = put
  put_nonblocking = put


class TestRayPassiveLogging:
  def initialize(self, enabled=True, controller=True, dashcam=False, injected=True, query_fw=True, camera_diagnostics=False):
    cp = NS(dashcamOnly=dashcam, safetyConfigs=[NS(safetyModel='hyundai')])
    cp.to_bytes = lambda: ('serialized', cp.passive, cp.safetyConfigs[0].safetyModel)
    h = NS(CP=cp, CI=NS(CC=object() if controller else None), params=Params(enabled))
    scope = {
      'self': h,
      'CI': object() if injected else None,
      'query_fw': query_fw,
      'ray_camera_diagnostics': camera_diagnostics,
      'ray_can0_startup': False,
      'structs': NS(CarParams=NS(SafetyConfig=NS, SafetyModel=NS(noOutput='noOutput'))),
    }
    exec(code, scope)
    return h

  def test_disabled_toggle_stores_nooutput_before_signalling_ready(self):
    h = self.initialize(enabled=False)
    assert h.CP.passive
    assert (h.CP.safetyConfigs[0].safetyModel) == ('noOutput')
    assert (h.params.calls[:2]) == ([('CarParams', ('serialized', True, 'noOutput')), ('ControlsReady', True)])

  def test_dashcam_only_signals_nooutput_ready(self):
    h = self.initialize(dashcam=True)
    assert h.CP.passive
    assert (('ControlsReady', True)) in (h.params.calls)

  def test_missing_controller_signals_nooutput_ready(self):
    h = self.initialize(controller=False)
    assert h.CP.passive
    assert (h.CP.safetyConfigs[0].safetyModel) == ('noOutput')
    assert (('ControlsReady', True)) in (h.params.calls)

  def test_active_mode_does_not_prematurely_signal_ready(self):
    h = self.initialize()
    assert not (h.CP.passive)
    assert (h.CP.safetyConfigs[0].safetyModel) == ('hyundai')
    assert ('ControlsReady') not in ([key for key, _ in h.params.calls])

  def test_enabling_toggle_during_receive_only_identification_stays_passive(self):
    h = self.initialize(enabled=True, injected=False, query_fw=False)
    assert h.CP.passive
    assert (h.CP.safetyConfigs[0].safetyModel) == ('noOutput')

  def test_normal_identification_keeps_active_mode_available(self):
    h = self.initialize(enabled=True, injected=False, query_fw=True)
    assert not (h.CP.passive)

  def test_camera_diagnostics_force_nooutput_even_with_enabled_toggle(self):
    h = self.initialize(enabled=True, injected=False, query_fw=True, camera_diagnostics=True)
    assert h.CP.passive
    assert h.CP.safetyConfigs[0].safetyModel == 'noOutput'
    assert ('ControlsReady', True) in h.params.calls

  def test_failed_carparams_write_never_signals_ready(self):
    class BrokenParams(Params):
      def put(self, key, value):
        raise OSError('write failed')

    h = self.initialize(enabled=False)
    h.params = BrokenParams(False)
    with pytest.raises(OSError):
      exec(code, {'self': h, 'CI': object(), 'ray_camera_diagnostics': False,
                   'ray_can0_startup': False,
                   'structs': NS(CarParams=NS(SafetyConfig=NS, SafetyModel=NS(noOutput='noOutput')))})
    assert (h.params.calls) == ([])

  def test_passive_publishes_received_state_without_calling_controls(self):
    for initialized in (False, True):
      calls = []

      class Inputs(dict):
        seen = {'onroadEvents': True}

      h = NS(
        CP=NS(passive=True),
        sm=Inputs(onroadEvents=[] if initialized else [NS(name='initializing')]),
        state_update=lambda: ('state', 'radar'),
        state_publish=lambda *x, calls=calls: calls.append(('publish', x)),
        controls_update=lambda *x, calls=calls: calls.append(('controls', x)),
      )
      env['step'](h)
      assert (calls) == ([('publish', ('state', 'radar'))])

  def test_active_initialized_controls_path_is_preserved(self):
    calls = []

    class Inputs(dict):
      seen = {'onroadEvents': True}

    h = NS(
      CP=NS(passive=False),
      sm=Inputs(onroadEvents=[], carControl='control'),
      state_update=lambda: ('state', 'radar'),
      state_publish=lambda *x: None,
      controls_update=lambda *x, calls=calls: calls.append(x),
    )
    env['step'](h)
    assert (calls) == ([('state', 'control')])
