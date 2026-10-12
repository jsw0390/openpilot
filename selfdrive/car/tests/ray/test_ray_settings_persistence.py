"""Native, memory-only parameter initialization regression tests. No CAN I/O."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import pytest
from unittest.mock import patch  # noqa: TID251 - mock utilities only; tests use pytest

if sys.platform != 'linux':
  pytest.skip('Requires the device native opendbc runtime', allow_module_level=True)

import opendbc.car.interfaces as base
from opendbc.car.hyundai.values import CAR

ROOT = Path(__file__).resolve().parents[4]
SOURCE = Path(os.environ.get('RAY_INTERFACE_TEST_PATH', ROOT / 'opendbc_repo/opendbc/car/hyundai/interface.py'))
spec = importlib.util.spec_from_file_location('ray_settings_candidate', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MemoryParams:
  def __init__(self, values):
    self.values, self.writes = dict(values), []

  def get(self, key, **kwargs):
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.values.get(key, False))

  def get_int(self, key):
    return int(self.values.get(key, 0))

  def get_float(self, key):
    return float(self.values.get(key, 0))

  def put_int(self, key, value):
    self.writes.append((key, value))
    self.values[key] = value

  put_bool = put_int
  put_nonblocking = put_int


class TestRaySettingsPersistence:
  def initialize(self, params, candidate=CAR.KIA_RAY_EV, alpha=True):
    with patch.object(module, 'Params', return_value=params), patch.object(base, 'Params', return_value=params), contextlib.redirect_stdout(io.StringIO()):
      return module.CarInterface.get_params(candidate, {0: {}, 1: {}, 2: {}, 3: {}}, [], alpha_long=alpha, is_release=True, docs=False)

  def test_explicit_off_values_survive_identification(self):
    values = {
      'AutoEngage': 0,
      'RayVisionCruiseControl': 0,
      'RayVisionIPedalAssist': 0,
      'LateralTorqueCustom': 0,
      'TurnSpeedControlMode': 0,
      'OpenpilotEnabledToggle': False,
    }
    p = MemoryParams(values)
    self.initialize(p)
    assert (p.values) == (values)
    assert (p.writes) == ([])

  def test_custom_values_survive_repeated_initialization(self):
    values = {
      'RayVisionCruiseRoadOffset': -5,
      'RayVisionIPedalSpeedDelta': 9,
      'RayVisionIPedalResumeMargin': 1,
      'RayVisionCruiseLeadProb': 90,
      'PathOffset': -12,
      'UseLaneLineSpeed': 40,
      'CustomSteerMax': 350,
      'LateralTorqueAccelFactor': 2300,
      'AutoCurveSpeedLowerLimit': 35,
    }
    p = MemoryParams(values)
    for _ in range(3):
      self.initialize(p)
    assert (p.values) == (values)
    assert (p.writes) == ([])

  def test_empty_preferences_do_not_auto_enable_features(self):
    p = MemoryParams({})
    self.initialize(p)
    assert (p.values) == ({})
    assert (p.writes) == ([])

  def test_setting_changed_between_initializations_is_preserved(self):
    p = MemoryParams({'AutoEngage': 2, 'RayVisionCruiseControl': 2, 'RayVisionIPedalAssist': 2})
    self.initialize(p)
    p.values.update(AutoEngage=0, RayVisionCruiseControl=1, RayVisionIPedalAssist=0)
    expected = dict(p.values)
    self.initialize(p)
    assert (p.values) == (expected)
    assert (p.writes) == ([])

  def test_longitudinal_vehicle_configuration_still_follows_alpha_toggle(self):
    for alpha in (False, True):
      cp = self.initialize(MemoryParams({}), alpha=alpha)
      assert (cp.openpilotLongitudinalControl) == (alpha)
      assert (cp.pcmCruise) == (not alpha)
      assert (cp.carFingerprint) == (CAR.KIA_RAY_EV)
      assert (cp.mass) > (0)
      assert (cp.wheelbase) > (0)

  def test_other_hyundai_keeps_preferences(self):
    p = MemoryParams({'AutoEngage': 0, 'RayVisionCruiseControl': 0, 'PathOffset': -12})
    expected = dict(p.values)
    self.initialize(p, candidate=CAR.HYUNDAI_SONATA)
    assert (p.values) == (expected)
    assert (p.writes) == ([])
