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


async def proxy_stream(request: web.Request) -> web.StreamResponse:
  body = await request.read()
  ct = request.headers.get("Content-Type", "application/json")

  sess: ClientSession = request.app["http"]

  try:
    async with sess.post(WEBRTCD_URL, data=body, headers={"Content-Type": ct},
                         timeout=ClientTimeout(total=15)) as resp:
      resp_body = await resp.read()
      out = web.Response(body=resp_body, status=resp.status)
      rct = resp.headers.get("Content-Type")
      if rct:
        out.headers["Content-Type"] = rct
      return out
  except asyncio.TimeoutError:
    return web.json_response({"ok": False, "error": "webrtcd timeout"}, status=504)
  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=502)

async def api_heartbeat_status(request: web.Request) -> web.Response:
  return web.json_response({"ok": True, "hb": request.app.get("hb_last")})


async def api_live_runtime(request: web.Request) -> web.Response:
  broker: RealtimeBroker | None = request.app.get("realtime_broker")
  broker_error = request.app.get("realtime_broker_error")
  if broker is None:
    return web.json_response({"ok": False, "error": broker_error or "realtime broker unavailable"}, status=503)

  force = request.query.get("force") == "1"
  runtime = broker.last_snapshot.get("runtime") if isinstance(broker.last_snapshot, dict) else None
  if not isinstance(runtime, dict):
    runtime = {}

  age_ms = broker.snapshot_age_ms()
  if force or age_ms is None or age_ms > 250 or not runtime.get("params"):
    try:
      await asyncio.to_thread(broker.poll, 0)
    except Exception as exc:
      return web.json_response({"ok": False, "error": str(exc)}, status=500)
    runtime = broker.last_snapshot.get("runtime") if isinstance(broker.last_snapshot, dict) else {}

  meta = broker.last_snapshot.get("meta") if isinstance(broker.last_snapshot, dict) else {}
  services = _select_live_runtime_services(broker.last_snapshot if isinstance(broker.last_snapshot, dict) else {})
  return web.json_response(to_transport_safe({
    "ok": True,
    "meta": meta if isinstance(meta, dict) else {},
    "runtime": runtime if isinstance(runtime, dict) else {},
    "services": services,
    "snapshotAgeMs": broker.snapshot_age_ms(),
  }))


_LIVE_RUNTIME_SERVICE_NAMES = (
  "selfdriveState",
  "carState",
  "controlsState",
  "deviceState",
  "peripheralState",
  "longitudinalPlan",
  "lateralPlan",
  "radarState",
  "carrotMan",
)


def _select_live_runtime_services(snapshot: dict[str, Any]) -> dict[str, Any]:
  services = snapshot.get("services")
  if not isinstance(services, dict):
    return {}
  out: dict[str, Any] = {}
  for name in _LIVE_RUNTIME_SERVICE_NAMES:
    value = services.get(name)
    if isinstance(value, dict):
      out[name] = value
  return out


# P3: dashcam handlers/helpers extracted to features/dashcam/
# P3: screenrecord handlers/helpers extracted to features/screenrecord/
# (See routes_api.py for the import names.)



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

# -----------------------
# Web handlers
# -----------------------
async def handle_index(request: web.Request) -> web.Response:
  return web.FileResponse(os.path.join(WEB_DIR, "index.html"))

# Legacy direct-file routes kept for backward compatibility.
async def handle_appjs(request: web.Request) -> web.Response:
  return web.FileResponse(os.path.join(JS_DIR, "app_core.js"))

async def api_settings(request: web.Request) -> web.Response:
  path = _settings_cache["path"]
  if not os.path.exists(path):
    return web.json_response({"ok": False, "error": f"settings file not found: {path}"}, status=404)

  try:
    data, groups, by_name, groups_list = _get_settings_cached()
    # keep insertion order of groups
    items_by_group = {g: items for g, items in groups.items()}
    return web.json_response({
      "ok": True,
      "path": path,
      "apilot": data.get("apilot"),
      "groups": groups_list,
      "items_by_group": items_by_group,
      "unit_cycle": UNIT_CYCLE,
      "has_params": HAS_PARAMS,
      "has_param_type": bool(ParamKeyType is not None and hasattr(Params(), "get_type")) if HAS_PARAMS else False,
    })
  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=500)

async def api_params_bulk(request: web.Request) -> web.Response:
  names = request.query.get("names", "")
  if not names:
    return web.json_response({"ok": False, "error": "missing names"}, status=400)

  req_names = [n for n in names.split(",") if n]
  try:
    _, _, by_name, _ = _get_settings_cached()
  except Exception:
    by_name = {}

  values = {}
  for n in req_names:
    if n == "DeviceType":
      try:
        from openpilot.system.hardware import HARDWARE
        values[n] = HARDWARE.get_device_type()
      except Exception:
        values[n] = "unknown"
    else:
      default = by_name.get(n, {}).get("default", 0)
      values[n] = _get_param_value(n, default)

  return web.json_response({"ok": True, "values": values})

async def api_param_set(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({"ok": False, "error": "invalid json"}, status=400)

  name = body.get("name")
  value = body.get("value")

  if not name:
    return web.json_response({"ok": False, "error": "missing name"}, status=400)

  # clamp using settings if numeric
  p = None
  try:
    _, _, by_name, _ = _get_settings_cached()
    p = by_name.get(name)
  except Exception:
    pass

  # If value numeric -> clamp
  try:
    if p is not None and isinstance(p.get("min"), (int, float)) and isinstance(p.get("max"), (int, float)):
      fv = float(value)
      fv = _clamp_numeric(fv, p)
      # keep int if setting looks int-ish
      if isinstance(p.get("min"), int) and isinstance(p.get("max"), int) and isinstance(p.get("default"), int):
        value = int(round(fv))
      else:
        value = fv
  except Exception:
    # ignore clamp errors (string values etc.)
    pass

  try:
    _set_param_value(name, value)
    return web.json_response({"ok": True, "name": name, "value": value, "has_params": HAS_PARAMS})
  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=500)

SUPPORTED_CAR_GLOB = "/data/params/d/SupportedCars*"

def _load_supported_cars() -> Tuple[List[str], Dict[str, List[str]]]:
  files = sorted(glob.glob(SUPPORTED_CAR_GLOB))
  makers: Dict[str, set] = {}

  for fp in files:
    try:
      with open(fp, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
          line = line.strip()
          if not line:
            continue
          parts = line.split(" ", 1)
          if len(parts) < 2:
            continue
          maker, rest = parts[0], parts[1].strip()
          full = f"{maker} {rest}"
          makers.setdefault(maker, set()).add(full)
    except Exception:
      continue

  makers_sorted: Dict[str, List[str]] = {}
  for mk, s in makers.items():
    makers_sorted[mk] = sorted(s)

  return [os.path.basename(x) for x in files], makers_sorted


async def api_cars(request: web.Request) -> web.Response:
  try:
    sources, makers = _load_supported_cars()
    return web.json_response({
      "ok": True,
      "sources": sources,
      "makers": makers,
    })
  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=500)

async def api_reboot(request: web.Request) -> web.Response:
  try:
    # 보안 최소조치(권장): 로컬/사설 대역만 허용 등
    # ip = request.remote
    # if not (ip.startswith("192.168.") or ip.startswith("10.") or ip in ("127.0.0.1", "::1")):
    #   return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    # 즉시 반환하고 리붓은 백그라운드로
    subprocess.Popen(["sudo", "reboot"])
    return web.json_response({"ok": True})
  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=500)



async def ws_raw(request: web.Request) -> web.WebSocketResponse:
  service = (request.match_info.get("service") or "").strip()
  hub: RawWsHub | None = request.app.get("realtime_raw_hub")
  if hub is None:
    raise web.HTTPServiceUnavailable(text="realtime raw hub unavailable")
  if not service or not hub.is_allowed_service(service):
    raise web.HTTPNotFound(text=f"unknown raw service: {service}")

  ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024, compress=False)
  await ws.prepare(request)
  await ws.send_str(json.dumps(build_raw_hello(service=service), separators=(",", ":")))
  await hub.register(service, ws)
  try:
    async for msg in ws:
      if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
        break
  finally:
    await hub.unregister_client(ws)

  return ws


async def ws_raw_multiplex(request: web.Request) -> web.WebSocketResponse:
  hub: RawWsHub | None = request.app.get("realtime_raw_hub")
  if hub is None:
    raise web.HTTPServiceUnavailable(text="realtime raw hub unavailable")

  services_param = request.query.get("services", "")
  services = [service.strip() for service in services_param.split(",") if service.strip()]
  if not services:
    raise web.HTTPBadRequest(text="missing raw services")
  invalid = [service for service in services if not hub.is_allowed_service(service)]
  if invalid:
    raise web.HTTPNotFound(text=f"unknown raw services: {','.join(invalid)}")

  ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024, compress=False)
  await ws.prepare(request)
  await ws.send_str(json.dumps(build_raw_multiplex_hello(services=services), separators=(",", ":")))
  await hub.register_many(services, ws)
  try:
    async for msg in ws:
      if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
        break
  finally:
    await hub.unregister_client(ws)

  return ws


async def ws_camera(request: web.Request) -> web.WebSocketResponse:
  hub: CameraWsHub | None = request.app.get("realtime_camera_hub")
  if hub is None:
    raise web.HTTPServiceUnavailable(text="realtime camera hub unavailable")
  return await hub.ws_camera(request)


async def handle_download_params_backup(request: web.Request) -> web.Response:
  path = PARAMS_BACKUP_PATH
  if not os.path.exists(path):
    return web.json_response({"ok": False, "error": "file not found"}, status=404)

  return web.FileResponse(
    path,
    headers={"Content-Disposition": "attachment; filename=params_backup.json"}
  )

async def api_params_restore(request: web.Request) -> web.Response:
  if not HAS_PARAMS or ParamKeyType is None:
    return web.json_response({"ok": False, "error": "Params/ParamKeyType not available"}, status=500)

  try:
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "file":
      return web.json_response({"ok": False, "error": "missing file field"}, status=400)

    data = await part.read(decode=False)
    text = data.decode("utf-8", errors="replace")
    j = json.loads(text)

    if not isinstance(j, dict):
      return web.json_response({"ok": False, "error": "bad json format (must be object)"}, status=400)

    values = j
    res = _restore_param_values_from_backup(values)
    return web.json_response({"ok": True, "result": res})

  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=500)



async def api_time_sync(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception as e:
    return web.json_response({"ok": False, "error": f"bad json: {e}"}, status=400)

  epoch_ms = body.get("epoch_ms")
  timezone_name = (body.get("timezone") or "").strip()
  debug = bool(body.get("debug", False))
  client_iso = body.get("client_iso")

  if not isinstance(epoch_ms, (int, float)):
    return web.json_response({"ok": False, "error": "epoch_ms required"}, status=400)

  if not timezone_name:
    timezone_name = "UTC"

  effective_debug = debug or TIME_SYNC_DEBUG_DEFAULT
  if effective_debug:
    print(f"[time_sync] client={request.remote} timezone={timezone_name} client_iso={client_iso} debug={debug}")

  result = await asyncio.to_thread(
    sync_system_time_from_browser,
    int(epoch_ms),
    timezone_name,
    effective_debug,
  )

  if effective_debug:
    print(
      f"[time_sync] result ok={result.get('ok')} "
      f"applied={result.get('applied')} "
      f"diff_sec={result.get('diff_sec')} "
      f"message={result.get('message')}"
    )

  status = 200 if result.get("ok") else 500
  return web.json_response(result, status=status)
