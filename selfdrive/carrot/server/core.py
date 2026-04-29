#!/usr/bin/env python3
# /data/openpilot/selfdrive/carrot/carrot_server.py
#
# aiohttp dashboard:
# - Home / Setting
# - loads carrot_settings.json
# - group buttons
# - bulk values load (fast on phone)
# - typed param set (ParamKeyType 기반) with fallback inference
#
# Run:
#   python3 /data/openpilot/selfdrive/carrot/carrot_server.py --host 0.0.0.0 --port 7000
#
# Open:
#   http://<device_ip>:7000/

import argparse
import base64
import json
import os
import math
import time
from datetime import datetime
import asyncio
import glob
import subprocess
import traceback
import numpy as np
from typing import Dict, Any, Tuple, Optional, List

from aiohttp import web, ClientSession, ClientTimeout, WSMsgType
from cereal import messaging
from opendbc.car import structs
import shlex
import shutil
import socket
import urllib.request
import urllib.error
import ssl
import getpass
import uuid
import hashlib
import mimetypes
from ftplib import FTP
from openpilot.common.realtime import set_core_affinity
from openpilot.system.hardware import HARDWARE

from ..realtime.raw_protocol import build_raw_hello, build_raw_multiplex_hello
from ..realtime.transports import CameraWsHub, RawWsHub
from .live_compat.broker import RealtimeBroker
from .live_compat.normalize import to_transport_safe

# P2: extracted modules — aliased for backward compatibility within this file.
from .config import (
  BASE_DIR, ROOT_DIR, WEB_DIR, CSS_DIR, JS_DIR, ASSETS_DIR, PAGES_DIR,
  DEFAULT_SETTINGS_PATH, CARROT_DATA_DIR, CARROT_STATE_DIR, CARROT_GIT_STATE_PATH,
  DASHCAM_ROOT, DASHCAM_CACHE_DIR, SCREEN_RECORDING_DIRS, SCREEN_RECORDING_EXTS,
  DASHCAM_DEFAULT_DISCORD_WEBHOOK, DASHCAM_DEFAULT_DISCORD_KEY,
  WEBRTCD_URL, TMUX_WEB_SESSION, TMUX_CAPTURE_LINES, TMUX_START_DIR,
  PARAMS_BACKUP_PATH,
  UNIT_CYCLE,
)
from .services.settings import (
  settings_cache as _settings_cache,
  read_settings_file as _read_settings_file,
  group_index as _group_index,
  get_settings_cached as _get_settings_cached,
)
from .services.params import (
  HAS_PARAMS, Params, ParamKeyType,
  _mem_store,
  infer_type_from_setting as _infer_type_from_setting,
  clamp_numeric as _clamp_numeric,
  get_param_value as _get_param_value,
  put_typed as _put_typed,
  set_param_value as _set_param_value,
  get_all_param_values_for_backup as _get_all_param_values_for_backup,
  restore_param_values_from_backup as _restore_param_values_from_backup,
)
from .services.git_state import (
  read_git_state as _read_git_state,
  write_git_state as _write_git_state,
  read_custom_meta_value as _read_custom_meta_value,
  write_git_pull_time as _write_git_pull_time,
  did_git_pull_update as _did_git_pull_update,
)
from .services.heartbeat import (
  get_local_ip as _get_local_ip,
  register_my_ip_sync as _register_my_ip_sync,
  heartbeat_loop,
)
from .services.time_sync import (
  TIME_SYNC_THRESHOLD_SEC,
  TIME_SYNC_DEBUG_DEFAULT,
  sync_system_time_from_browser,
)

GearShifter = structs.CarState.GearShifter


# ===== request log middleware =====
@web.middleware
async def log_mw(request, handler):
  ua = request.headers.get("User-Agent", "")
  ip = request.remote
  t0 = time.time()
  try:
    resp = await handler(request)
    return resp
  finally:
    #dt = (time.time() - t0) * 1000
    #print(f"[REQ] {ip} {request.method} {request.path_qs} {dt:.1f}ms UA={ua[:80]}")
    pass


def _do_gc_and_trim() -> None:
  """gc.collect + malloc_trim — runs in thread pool (GIL acquired there)."""
  import gc as _gc
  _gc.collect()
  try:
    import ctypes
    libc = ctypes.CDLL("libc.so.6")
    libc.malloc_trim(0)
  except Exception:
    pass

async def _malloc_trim_loop():
  """Periodic gc + malloc_trim to reclaim leaked objects and return C heap.
  Runs via to_thread so the event loop is never blocked."""
  while True:
    await asyncio.sleep(30.0)
    await asyncio.to_thread(_do_gc_and_trim)

async def on_startup(app: web.Application):
  app["http"] = ClientSession()
  app["hb_last"] = {"ok": None, "msg": "not yet", "ts": 0}
  # Eager broker creation — single SubMaster via RealtimeBroker
  try:
    broker = RealtimeBroker(repo_flavor="c3")
    app["realtime_broker"] = broker
    app["realtime_broker_error"] = None
  except Exception as exc:
    app["realtime_broker"] = None
    app["realtime_broker_error"] = str(exc)
  app["realtime_camera_hub"] = CameraWsHub(messaging)
  app["realtime_raw_hub"] = RawWsHub(messaging)
  if HAS_PARAMS:
    app["hb_task"] = asyncio.create_task(heartbeat_loop(app))
  asyncio.create_task(_malloc_trim_loop())

async def on_cleanup(app: web.Application):
  realtime_camera_hub = app.get("realtime_camera_hub")
  if realtime_camera_hub is not None:
    try:
      await realtime_camera_hub.stop_all()
    except Exception:
      traceback.print_exc()

  realtime_raw_hub = app.get("realtime_raw_hub")
  if realtime_raw_hub is not None:
    try:
      await realtime_raw_hub.stop_all()
    except Exception:
      traceback.print_exc()
    
  t = app.get("hb_task")
  if t:
    t.cancel()
    try:
      await t
    except Exception:
      pass

  sess = app.get("http")
  if sess:
    await sess.close()

