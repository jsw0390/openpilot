"""
Tool action dispatchers.

- `run_tool_job(job)` — async streaming dispatcher (used by /api/tools/start).
- `dispatch_sync(request, body)` — synchronous dispatcher (used by /api/tools).

Both share helpers from `jobs.py` (job state, exec wrappers, branch utils) and
action/command policy from `actions.py`. Some action implementations still have
async/sync variants, so keep behavior changes small and deliberate.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from openpilot.system.hardware import HARDWARE

from ...config import PARAMS_BACKUP_PATH
from ...services.git_state import did_git_pull_update, write_git_pull_time
from ...services.params import HAS_PARAMS, Params, ParamKeyType, get_all_param_values_for_backup
from . import jobs
from .actions import normalize_action, validate_action, validate_shell_argv


TMUX_LOG_PATH = "/data/media/tmux.log"
MAPD_DATA_DIR = "/data/media/0/osm"
MAPD_DOWNLOAD_META_PATH = os.path.join(MAPD_DATA_DIR, ".carrot_mapd_downloads.json")
MAPD_DOWNLOAD_PATH_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAPD_INPUT_TYPE_IDS = {
  "download": 0,
  "cancelDownload": 27,
}


def normalize_mapd_download_path(path: Any) -> str:
  parts = [part.strip() for part in str(path or "").split(",") if part.strip()]
  if not parts:
    raise ValueError("missing mapd download path")
  if len(parts) > 12:
    raise ValueError("too many mapd download areas")
  for part in parts:
    if len(part) > 128 or MAPD_DOWNLOAD_PATH_RE.match(part) is None:
      raise ValueError(f"invalid mapd download path: {part}")
  return ",".join(parts)


def mapd_today() -> str:
  return time.strftime("%Y-%m-%d", time.localtime())


def mapd_download_locations(path: str) -> List[str]:
  return [part.strip() for part in str(path or "").split(",") if part.strip()]


def read_mapd_download_meta() -> Dict[str, Any]:
  try:
    with open(MAPD_DOWNLOAD_META_PATH, "r", encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except FileNotFoundError:
    return {}
  except Exception:
    return {}


def write_mapd_download_meta(meta: Dict[str, Any]) -> None:
  os.makedirs(MAPD_DATA_DIR, exist_ok=True)
  tmp_path = f"{MAPD_DOWNLOAD_META_PATH}.tmp"
  with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
  os.replace(tmp_path, MAPD_DOWNLOAD_META_PATH)


def mapd_local_data_snapshot() -> Dict[str, Any]:
  newest_mtime = 0.0
  newest_path = ""
  total_bytes = 0
  file_count = 0

  try:
    for root, dirs, files in os.walk(MAPD_DATA_DIR):
      dirs[:] = [name for name in dirs if not name.startswith(".")]
      for name in files:
        if name.startswith("."):
          continue
        path = os.path.join(root, name)
        if path == MAPD_DOWNLOAD_META_PATH:
          continue
        try:
          st = os.stat(path)
        except OSError:
          continue
        file_count += 1
        total_bytes += int(st.st_size)
        if st.st_mtime > newest_mtime:
          newest_mtime = st.st_mtime
          newest_path = path
  except OSError:
    pass

  data_date = time.strftime("%Y-%m-%d", time.localtime(newest_mtime)) if newest_mtime > 0 else ""
  return {
    "present": file_count > 0,
    "data_date": data_date,
    "file_count": file_count,
    "total_bytes": total_bytes,
    "newest_mtime": int(newest_mtime) if newest_mtime > 0 else 0,
    "newest_file": newest_path,
  }


def mapd_download_meta_entry(path: str) -> Optional[Dict[str, Any]]:
  downloads = read_mapd_download_meta().get("downloads")
  if not isinstance(downloads, dict):
    return None
  entry = downloads.get(path)
  return entry if isinstance(entry, dict) else None


def mapd_download_skip_snapshot(path: str, force: bool = False) -> Optional[Dict[str, Any]]:
  if force:
    return None

  local_data = mapd_local_data_snapshot()
  if not local_data.get("present"):
    return None

  entry = mapd_download_meta_entry(path)
  if not entry:
    return None

  checked_date = str(entry.get("checked_date") or entry.get("data_date") or "")
  data_date = str(entry.get("data_date") or "")
  today = mapd_today()
  if checked_date != today or data_date != str(local_data.get("data_date") or ""):
    return None

  return {
    "path": path,
    "locations": mapd_download_locations(path),
    "data_date": data_date,
    "entry": entry,
    "local_data": local_data,
  }


def update_mapd_download_meta(path: str) -> Dict[str, Any]:
  local_data = mapd_local_data_snapshot()
  today = mapd_today()
  data_date = str(local_data.get("data_date") or today)
  entry = {
    "path": path,
    "locations": mapd_download_locations(path),
    "data_date": data_date,
    "checked_date": today,
    "downloaded_at": int(time.time()),
    "local_data": local_data,
  }

  meta = read_mapd_download_meta()
  downloads = meta.get("downloads")
  if not isinstance(downloads, dict):
    downloads = {}
  downloads[path] = entry
  meta.update({
    "schema": 1,
    "updated_at": int(time.time()),
    "downloads": downloads,
  })
  write_mapd_download_meta(meta)
  return entry


def set_mapd_download_active(active: bool) -> None:
  if not HAS_PARAMS:
    return
  try:
    Params().put_bool("MapdDownloadActive", bool(active))
  except Exception:
    pass


def set_mapd_input_type(mapd_in: Any, input_type: str) -> None:
  try:
    mapd_in.type = input_type
    return
  except Exception:
    pass

  try:
    from cereal import custom
    mapd_in.type = getattr(custom.MapdInputType, input_type)
    return
  except Exception:
    pass

  mapd_in.type = MAPD_INPUT_TYPE_IDS[input_type]


def send_mapd_input(pm: Any, input_type: str, *, str_value: str = "",
                    float_value: float = 0.0, bool_value: bool = False) -> None:
  from cereal import messaging

  msg = messaging.new_message("mapdIn", valid=True)
  set_mapd_input_type(msg.mapdIn, input_type)
  msg.mapdIn.str = str(str_value or "")
  msg.mapdIn.float = float(float_value)
  msg.mapdIn.bool = bool(bool_value)
  pm.send("mapdIn", msg)


def mapd_progress_snapshot(progress: Any) -> Dict[str, Any]:
  total = int(getattr(progress, "totalFiles", 0) or 0)
  downloaded = int(getattr(progress, "downloadedFiles", 0) or 0)
  percent = int(max(0, min(100, round((downloaded / total) * 100)))) if total > 0 else None
  details = []
  try:
    for item in progress.locationDetails:
      details.append({
        "location": str(getattr(item, "location", "") or ""),
        "total_files": int(getattr(item, "totalFiles", 0) or 0),
        "downloaded_files": int(getattr(item, "downloadedFiles", 0) or 0),
      })
  except Exception:
    details = []

  try:
    locations = [str(item) for item in progress.locations]
  except Exception:
    locations = []

  return {
    "active": bool(getattr(progress, "active", False)),
    "cancelled": bool(getattr(progress, "cancelled", False)),
    "total_files": total,
    "downloaded_files": downloaded,
    "percent": percent,
    "locations": locations,
    "location_details": details,
  }


def mapd_status_snapshot(sm: Any, include_local: bool = False) -> Dict[str, Any]:
  extended_alive = bool(sm.alive.get("mapdExtendedOut"))
  out_alive = bool(sm.alive.get("mapdOut"))
  result: Dict[str, Any] = {
    "ok": True,
    "enabled": False,
    "mapd_alive": extended_alive or out_alive,
    "mapd_extended_alive": extended_alive,
    "mapd_out_alive": out_alive,
    "download": None,
    "map": None,
    "manager": None,
    "device": None,
    "car": None,
  }

  if HAS_PARAMS:
    try:
      result["enabled"] = bool(Params().get_bool("MapdEnabled"))
    except Exception:
      result["enabled"] = False

  try:
    progress = sm["mapdExtendedOut"].downloadProgress
    result["download"] = mapd_progress_snapshot(progress)
  except Exception:
    result["download"] = None

  try:
    mapd_out = sm["mapdOut"]
    result["map"] = {
      "tile_loaded": bool(mapd_out.tileLoaded),
      "road_name": str(mapd_out.roadName or ""),
      "speed_limit_ms": float(mapd_out.speedLimit or 0.0),
      "suggested_speed_ms": float(mapd_out.suggestedSpeed or 0.0),
      "map_curve_speed_ms": float(mapd_out.mapCurveSpeed or 0.0),
    }
  except Exception:
    result["map"] = None

  try:
    manager_state = sm["managerState"]
    for proc in manager_state.processes:
      if str(proc.name) == "mapd":
        result["manager"] = {
          "running": bool(proc.running),
          "should_be_running": bool(proc.shouldBeRunning),
          "pid": int(proc.pid),
          "exit_code": int(proc.exitCode),
        }
        break
  except Exception:
    result["manager"] = None

  try:
    device_state = sm["deviceState"]
    result["device"] = {"started": bool(device_state.started)}
  except Exception:
    result["device"] = None

  try:
    car_state = sm["carState"]
    result["car"] = {"gear_shifter": str(car_state.gearShifter)}
  except Exception:
    result["car"] = None

  if include_local:
    result["local_data"] = mapd_local_data_snapshot()
    result["downloads"] = read_mapd_download_meta().get("downloads", {})

  return result


def mapd_progress_message(download: Dict[str, Any], fallback: str = "mapd download") -> str:
  if not download:
    return fallback
  downloaded = int(download.get("downloaded_files") or 0)
  total = int(download.get("total_files") or 0)
  percent = download.get("percent")
  locations = ", ".join(download.get("locations") or [])
  base = locations or fallback
  if total > 0 and percent is not None:
    return f"{base}: {downloaded}/{total} ({percent}%)"
  return base


def append_mapd_progress(job: Dict[str, Any], download: Dict[str, Any], last: str) -> str:
  message = mapd_progress_message(download)
  detail_parts = []
  for item in download.get("location_details") or []:
    location = item.get("location") or "unknown"
    downloaded = int(item.get("downloaded_files") or 0)
    total = int(item.get("total_files") or 0)
    detail_parts.append(f"{location} {downloaded}/{total}")
  if detail_parts:
    message = f"{message}\n" + "\n".join(detail_parts)
  if message != last:
    jobs.append(job, message + "\n")
  return message


async def update_mapd_submaster(sm: Any, timeout_ms: int = 1000) -> None:
  await asyncio.to_thread(sm.update, timeout_ms)


async def get_mapd_status(timeout_ms: int = 1200) -> Dict[str, Any]:
  from cereal import messaging

  sm = messaging.SubMaster(["mapdOut", "mapdExtendedOut", "managerState", "deviceState", "carState"])
  await update_mapd_submaster(sm, timeout_ms)
  return mapd_status_snapshot(sm, include_local=True)


def capture_tmux_log_sync() -> Tuple[int, str]:
  try:
    os.remove(TMUX_LOG_PATH)
  except FileNotFoundError:
    pass
  except OSError as e:
    return 1, str(e)

  proc = subprocess.run(
    ["tmux", "capture-pane", "-pq", "-S-1000"],
    capture_output=True,
    text=True,
  )
  if proc.returncode != 0:
    return proc.returncode, (proc.stderr or proc.stdout or "").strip()

  os.makedirs(os.path.dirname(TMUX_LOG_PATH), exist_ok=True)
  with open(TMUX_LOG_PATH, "w", encoding="utf-8") as f:
    f.write(proc.stdout or "")
  return 0, ""


async def run_tool_job(job: Dict[str, Any]) -> None:
  action = normalize_action(job.get("action"))
  body = job.get("payload") or {}
  repo_dir = "/data/openpilot"

  try:
    action_error = validate_action(action)
    if action_error:
      error, error_code = action_error
      jobs.finish(job, ok=False, result={"ok": False, "error": error, "error_code": error_code}, error=error, error_code=error_code)
      return

    if action == "mapd_download":
      try:
        download_path = normalize_mapd_download_path(body.get("path") or body.get("download_path") or "nation.KR")
      except ValueError as e:
        jobs.finish(
          job,
          ok=False,
          result={"ok": False, "error": str(e), "error_code": "INVALID_MAPD_PATH"},
          error=str(e),
          error_code="INVALID_MAPD_PATH",
        )
        return

      skip_snapshot = mapd_download_skip_snapshot(download_path, bool(body.get("force")))
      if skip_snapshot:
        status = {
          "ok": True,
          "skipped": True,
          "download": {
            "active": False,
            "cancelled": False,
            "total_files": 0,
            "downloaded_files": 0,
            "percent": 100,
            "locations": skip_snapshot.get("locations", []),
            "location_details": [],
          },
          "local_data": skip_snapshot.get("local_data", {}),
          "downloads": read_mapd_download_meta().get("downloads", {}),
        }
        message = f"map data already current: {download_path} ({skip_snapshot.get('data_date')})"
        jobs.progress(job, message=message, percent=100)
        jobs.append(job, f"{message}\n")
        jobs.finish(job, ok=True, result={"ok": True, "skipped": True, "out": message, "status": status})
        return

      if HAS_PARAMS:
        try:
          params = Params()
          if not params.get_bool("MapdEnabled"):
            params.put_bool("MapdEnabled", True)
            jobs.append(job, "MapdEnabled enabled for map download\n")
          params.put_bool("MapdRunOnroad", True)
          set_mapd_download_active(True)
        except Exception as e:
          jobs.append(job, f"mapd params set failed: {e}\n")

      from cereal import messaging
      pm = messaging.PubMaster(["mapdIn"])
      sm = messaging.SubMaster(["mapdOut", "mapdExtendedOut", "managerState", "deviceState", "carState"])

      jobs.progress(job, message="waiting for mapd", current=0, total=100, percent=0)
      ready = False
      wait_seconds = 8.0
      wait_deadline = time.monotonic() + wait_seconds
      while time.monotonic() < wait_deadline:
        await update_mapd_submaster(sm, 1000)
        if sm.alive.get("mapdExtendedOut") or sm.alive.get("mapdOut"):
          ready = True
          break
        remaining = max(0.0, wait_deadline - time.monotonic())
        waited = max(0.0, wait_seconds - remaining)
        jobs.progress(job, message="waiting for mapd", percent=int(min(15, waited / wait_seconds * 15.0)))

      if not ready:
        jobs.append(job, "mapd is not publishing yet; sending download request anyway\n")

      jobs.append(job, f"$ mapd download {download_path}\n")
      jobs.progress(job, message=f"download requested: {download_path}", percent=20)

      active_seen = False
      last_progress_log = ""
      start_deadline = time.monotonic() + 120.0
      download_deadline = time.monotonic() + 2 * 60 * 60
      last_snapshot: Dict[str, Any] = {}
      last_request_time = 0.0

      while time.monotonic() < download_deadline:
        now = time.monotonic()
        if not active_seen and now - last_request_time >= 2.0:
          send_mapd_input(pm, "download", str_value=download_path)
          last_request_time = now

        await update_mapd_submaster(sm, 1000)
        status = mapd_status_snapshot(sm)
        download = status.get("download") or {}
        last_snapshot = status
        ready = ready or bool(status.get("mapd_alive"))

        if download.get("active"):
          active_seen = True
          percent = download.get("percent")
          jobs.progress(
            job,
            message=mapd_progress_message(download, "downloading map"),
            percent=int(percent) if percent is not None else None,
          )
          last_progress_log = append_mapd_progress(job, download, last_progress_log)
          continue

        if active_seen:
          if download.get("cancelled"):
            set_mapd_download_active(False)
            jobs.finish(
              job,
              ok=False,
              result={"ok": False, "error": "map download cancelled", "error_code": "MAPD_DOWNLOAD_CANCELLED", "status": status},
              error="map download cancelled",
              error_code="MAPD_DOWNLOAD_CANCELLED",
            )
            return

          downloaded = int(download.get("downloaded_files") or 0)
          total = int(download.get("total_files") or 0)
          if total > 0 and downloaded < total:
            set_mapd_download_active(False)
            error = f"map download incomplete: {downloaded}/{total}"
            jobs.finish(
              job,
              ok=False,
              result={"ok": False, "error": error, "error_code": "MAPD_DOWNLOAD_INCOMPLETE", "status": status},
              error=error,
              error_code="MAPD_DOWNLOAD_INCOMPLETE",
            )
            return

          jobs.progress(job, message="map download complete", percent=100)
          set_mapd_download_active(False)
          status["local_data"] = update_mapd_download_meta(download_path).get("local_data", {})
          status["downloads"] = read_mapd_download_meta().get("downloads", {})
          jobs.finish(job, ok=True, result={"ok": True, "out": "map download complete", "status": status})
          return

        if time.monotonic() > start_deadline:
          set_mapd_download_active(False)
          jobs.finish(
            job,
            ok=False,
            result={
              "ok": False,
              "error": "map download did not start. Keep the device powered and retry after mapd finishes starting.",
              "error_code": "MAPD_DOWNLOAD_NOT_STARTED",
              "status": last_snapshot,
            },
            error="map download did not start",
            error_code="MAPD_DOWNLOAD_NOT_STARTED",
          )
          return

        jobs.progress(job, message="waiting for download progress", percent=40 if ready else 25)

      set_mapd_download_active(False)
      jobs.finish(
        job,
        ok=False,
        result={"ok": False, "error": "map download timeout", "error_code": "MAPD_DOWNLOAD_TIMEOUT", "status": last_snapshot},
        error="map download timeout",
        error_code="MAPD_DOWNLOAD_TIMEOUT",
      )
      return

    if action == "mapd_cancel_download":
      set_mapd_download_active(True)
      from cereal import messaging
      pm = messaging.PubMaster(["mapdIn"])
      jobs.progress(job, message="cancel map download", current=1, total=1)
      for _ in range(3):
        send_mapd_input(pm, "cancelDownload")
        await asyncio.sleep(0.1)
      jobs.append(job, "$ mapd cancel download\n")
      set_mapd_download_active(False)
      jobs.finish(job, ok=True, result={"ok": True, "out": "map download cancel requested"})
      return

    if action == "mapd_status":
      jobs.progress(job, message="read mapd status", current=1, total=1)
      status = await get_mapd_status()
      jobs.finish(job, ok=True, result={"ok": True, "status": status, "out": json.dumps(status, ensure_ascii=False)})
      return

    if action == "git_pull":
      jobs.progress(job, message="git reset --hard", current=1, total=2)
      jobs.append(job, "$ git reset --hard\n")
      rc_reset = await jobs.stream_exec(job, ["git", "reset", "--hard"], cwd=repo_dir, timeout=120)
      if rc_reset != 0:
        jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc_reset))
        return

      jobs.append(job, "\n$ git pull\n")
      jobs.progress(job, message="git pull", current=2, total=2)
      rc = await jobs.stream_exec(job, ["git", "pull"], cwd=repo_dir, timeout=180)
      if rc == 0 and did_git_pull_update(job.get("log") or ""):
        write_git_pull_time()
      result = jobs.result_from_log(job, rc)
      jobs.finish(job, ok=rc == 0, result=result)
      return

    if action == "git_sync":
      jobs.progress(job, message="delete local branches", current=1, total=2)
      rc1 = await jobs.stream_exec(
        job,
        ["bash", "-lc", "git branch | grep -v '^\\*' | xargs -r git branch -D"],
        cwd=repo_dir,
        timeout=120,
      )
      if rc1 != 0:
        jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc1))
        return

      jobs.progress(job, message="fetch --all --prune", current=2, total=2)
      rc2 = await jobs.stream_exec(job, ["git", "fetch", "--all", "--prune"], cwd=repo_dir, timeout=180)
      jobs.finish(job, ok=rc2 == 0, result=jobs.result_from_log(job, rc2))
      return

    if action == "git_reset":
      mode = (body.get("mode") or "hard").strip()
      target = (body.get("target") or "HEAD").strip()
      if mode not in ("hard", "soft", "mixed"):
        jobs.finish(
          job,
          ok=False,
          result={"ok": False, "error": "bad mode", "error_code": "INVALID_RESET_MODE"},
          error="bad mode",
          error_code="INVALID_RESET_MODE",
        )
        return

      jobs.progress(job, message=f"git reset --{mode} {target}", current=1, total=1)
      rc = await jobs.stream_exec(job, ["git", "reset", f"--{mode}", target], cwd=repo_dir, timeout=120)
      jobs.finish(job, ok=rc == 0, result=jobs.result_from_log(job, rc))
      return

    if action == "git_checkout":
      branch = (body.get("branch") or "").strip()
      kind = str(body.get("kind") or "").strip()
      item_name = str(body.get("name") or "").strip()
      item_remote = str(body.get("remote") or "").strip()
      if not branch and not item_name:
        jobs.finish(
          job,
          ok=False,
          result={"ok": False, "error": "missing branch", "error_code": "MISSING_BRANCH"},
          error="missing branch",
          error_code="MISSING_BRANCH",
        )
        return

      jobs.progress(job, message="fetch --all --prune", current=1, total=2)
      rc_fetch = await jobs.stream_exec(job, ["git", "fetch", "--all", "--prune"], cwd=repo_dir, timeout=180)
      if rc_fetch != 0:
        jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc_fetch))
        return

      jobs.progress(job, message=f"switch {branch}", current=2, total=2)

      rc_remotes, remotes_out = await jobs.capture_exec(["git", "remote"], cwd=repo_dir, timeout=30)
      known_remotes = remotes_out.split() if rc_remotes == 0 else ["origin"]

      if kind == "local":
        local_branch = item_name or branch
        script = f"git switch {shlex.quote(local_branch)}"
      elif kind == "remote":
        if not item_remote or not item_name:
          jobs.finish(job, ok=False, result={"ok": False, "error": "missing remote branch info"}, error="missing remote branch info")
          return
        if item_remote not in known_remotes:
          jobs.finish(job, ok=False, result={"ok": False, "error": f"unknown remote: {item_remote}"}, error=f"unknown remote: {item_remote}")
          return
        branch = f"{item_remote}/{item_name}"
        local_branch = item_name
        script = (
          f"if git show-ref --verify --quiet {shlex.quote(f'refs/heads/{local_branch}')}; "
          f"then git switch {shlex.quote(local_branch)}; "
          f"else git switch -c {shlex.quote(local_branch)} --track {shlex.quote(branch)}; fi"
        )
      else:
        # Backward compatibility for older clients that only send a branch string.
        remote_prefix = None
        for r in known_remotes:
          if branch.startswith(f"{r}/"):
            remote_prefix = r
            break

        if remote_prefix is not None:
          local_branch = branch[len(remote_prefix) + 1:]
          script = (
            f"if git show-ref --verify --quiet {shlex.quote(f'refs/heads/{local_branch}')}; "
            f"then git switch {shlex.quote(local_branch)}; "
            f"else git switch -c {shlex.quote(local_branch)} --track {shlex.quote(branch)}; fi"
          )
        else:
          script = (
            f"git switch {shlex.quote(branch)} || "
            f"git switch -c {shlex.quote(branch)} --track {shlex.quote(f'origin/{branch}')}"
          )
      rc = await jobs.stream_exec(job, ["bash", "-lc", script], cwd=repo_dir, timeout=180)
      jobs.finish(job, ok=rc == 0, result=jobs.result_from_log(job, rc))
      return

    if action == "git_remote_set":
      url = str(job.get("payload", {}).get("url") or "").strip()
      if not url:
        jobs.finish(job, ok=False, result={"ok": False, "error": "missing url"}, error="missing url")
        return

      jobs.progress(job, message=f"set-url origin {url}", current=1, total=2)
      rc_set = await jobs.stream_exec(job, ["git", "remote", "set-url", "origin", url], cwd=repo_dir, timeout=30)
      if rc_set != 0:
        jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc_set))
        return

      jobs.progress(job, message="fetch origin", current=2, total=2)
      rc_fetch = await jobs.stream_exec(job, ["git", "fetch", "--progress", "origin"], cwd=repo_dir, timeout=180)
      jobs.finish(job, ok=rc_fetch == 0, result=jobs.result_from_log(job, rc_fetch))
      return

    if action == "git_branch_list":
      jobs.progress(job, message="fetch --all --prune", current=1, total=2)
      rc_fetch = await jobs.stream_exec(job, ["git", "fetch", "--all", "--prune"], cwd=repo_dir, timeout=180)
      if rc_fetch != 0:
        jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc_fetch))
        return

      jobs.progress(job, message="git refs", current=2, total=2)
      rc_local, local_refs_out = await jobs.capture_exec(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        cwd=repo_dir,
        timeout=30,
      )
      rc_remote, remote_refs_out = await jobs.capture_exec(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes"],
        cwd=repo_dir,
        timeout=30,
      )
      if local_refs_out or remote_refs_out:
        jobs.append(job, "\n$ git refs\n")
        if local_refs_out:
          jobs.append(job, "[local]\n" + local_refs_out + "\n")
        if remote_refs_out:
          jobs.append(job, "[remote]\n" + remote_refs_out + "\n")
      if rc_local != 0 or rc_remote != 0:
        jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc_local if rc_local != 0 else rc_remote))
        return

      rc_current, current_branch = await jobs.capture_exec(
        ["git", "branch", "--show-current"],
        cwd=repo_dir,
        timeout=15,
      )
      if rc_current != 0:
        current_branch = ""
      current_branch = (current_branch or "").strip()

      rc_remotes, remotes_out = await jobs.capture_exec(["git", "remote"], cwd=repo_dir, timeout=15)
      remotes = remotes_out.split() if rc_remotes == 0 else ["origin"]
      rc_remote_urls, remote_urls_out = await jobs.capture_exec(["git", "remote", "-v"], cwd=repo_dir, timeout=15)
      remote_urls = jobs.parse_remote_urls(remote_urls_out) if rc_remote_urls == 0 else {}
      branch_items = jobs.build_branch_items(local_refs_out, remote_refs_out, remotes)
      branches = sorted({str(item.get("ref") or "") for item in branch_items if item.get("ref")})

      result = {
        "ok": True,
        "branches": branches,
        "branch_items": branch_items,
        "current_branch": current_branch,
        "fetch": (job.get("log") or "").strip(),
        "device_type": HARDWARE.get_device_type(),
        "branch_prefix": jobs.get_branch_prefix(),
        "remotes": remotes,
        "remote_urls": remote_urls,
      }
      jobs.finish(job, ok=True, result=result)
      return

    if action == "git_remote_add":
      name = str(body.get("name") or "").strip()
      url = str(body.get("url") or "").strip()
      if not name or not url:
        jobs.finish(job, ok=False, result={"ok": False, "error": "missing name or url"}, error="missing name or url")
        return

      rc_remotes, remotes_out = await jobs.capture_exec(["git", "remote"], cwd=repo_dir, timeout=15)
      remotes = remotes_out.split() if rc_remotes == 0 else []
      remote_exists = name in remotes

      setup_cmd = ["git", "remote", "set-url", name, url] if remote_exists else ["git", "remote", "add", name, url]
      setup_label = "set-url" if remote_exists else "add"
      jobs.progress(job, message=f"git remote {setup_label} {name}", current=1, total=2)
      rc_setup = await jobs.stream_exec(job, setup_cmd, cwd=repo_dir, timeout=30)
      if rc_setup != 0:
        jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc_setup))
        return

      jobs.progress(job, message=f"git fetch --prune {name}", current=2, total=2)
      rc_fetch = await jobs.stream_exec(job, ["git", "fetch", "--prune", "--progress", name], cwd=repo_dir, timeout=180)

      rc_remote_urls, remote_urls_out = await jobs.capture_exec(["git", "remote", "-v"], cwd=repo_dir, timeout=15)
      if rc_remote_urls == 0 and remote_urls_out:
        jobs.append(job, "\n$ git remote -v\n")
        jobs.append(job, remote_urls_out + "\n")
      jobs.finish(job, ok=rc_fetch == 0, result=jobs.result_from_log(job, rc_fetch))
      return

    if action == "git_log":
      count = min(int(body.get("count") or 20), 50)
      jobs.progress(job, message="git log", current=1, total=1)
      rc, out = await jobs.capture_exec(
        ["git", "log", f"--oneline", f"-{count}"],
        cwd=repo_dir,
        timeout=30,
      )
      rc_head, out_head = await jobs.capture_exec(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo_dir,
        timeout=10,
      )
      current_commit = out_head.strip() if rc_head == 0 else ""
      if out:
        jobs.append(job, out)
      commits = []
      for line in (out or "").splitlines():
        line = line.strip()
        if not line:
          continue
        parts = line.split(" ", 1)
        commits.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
      result = {"ok": rc == 0, "commits": commits, "current_commit": current_commit, "out": out}
      jobs.finish(job, ok=rc == 0, result=result)
      return

    if action == "git_reset_repo_fetch":
      url = "https://github.com/ajouatom/openpilot.git"
      # Phase 1: ensure origin points to the correct URL
      jobs.progress(job, message="configuring origin remote", current=1, total=4)

      rc_set, _ = await jobs.capture_exec(
        ["git", "remote", "set-url", "origin", url], cwd=repo_dir, timeout=15
      )
      if rc_set != 0:
        jobs.append(job, "origin not found, adding new remote\n")
        await jobs.capture_exec(["git", "remote", "remove", "origin"], cwd=repo_dir, timeout=10)
        rc_add, out_add = await jobs.capture_exec(
          ["git", "remote", "add", "origin", url], cwd=repo_dir, timeout=15
        )
        if rc_add != 0:
          jobs.append(job, f"failed to add origin: {out_add}\n")
          jobs.finish(job, ok=False, result={"ok": False, "error": f"failed to configure remote: {out_add}"})
          return
      jobs.append(job, f"origin → {url}\n")

      # Phase 2: remove ALL other remotes (so only origin remains)
      jobs.progress(job, message="cleaning other remotes", current=2, total=4)
      rc_remotes, out_remotes = await jobs.capture_exec(
        ["git", "remote"], cwd=repo_dir, timeout=10
      )
      for remote_name in (out_remotes or "").splitlines():
        remote_name = remote_name.strip()
        if remote_name and remote_name != "origin":
          jobs.append(job, f"removing remote: {remote_name}\n")
          await jobs.capture_exec(
            ["git", "remote", "remove", remote_name], cwd=repo_dir, timeout=10
          )

      # Phase 3: fetch from origin only
      jobs.progress(job, message="git fetch origin --prune", current=3, total=4)
      rc_fetch = await jobs.stream_exec(
        job, ["git", "fetch", "origin", "--prune"], cwd=repo_dir, timeout=300
      )
      if rc_fetch != 0:
        jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc_fetch))
        return

      # Phase 4: list remote branches (origin/* only)
      jobs.progress(job, message="listing branches", current=4, total=4)
      rc_br, out_br = await jobs.capture_exec(
        ["git", "branch", "-r"], cwd=repo_dir, timeout=15
      )
      branches = []
      for line in (out_br or "").splitlines():
        line = line.strip()
        if not line or "->" in line:
          continue
        if not line.startswith("origin/"):
          continue
        branches.append(line.split("/", 1)[1])
      branches = sorted(set(branches))
      jobs.append(job, f"found {len(branches)} branches\n")

      result = {"ok": True, "branches": branches, "out": (job.get("log") or "").strip()}
      jobs.finish(job, ok=True, result=result)
      return

    if action == "git_reset_repo_checkout":
      branch = str(body.get("branch") or "").strip()
      if not branch:
        jobs.finish(job, ok=False, result={"ok": False, "error": "missing branch"}, error="missing branch")
        return

      steps = [
        (f"git checkout -B {branch} origin/{branch}", ["git", "checkout", "-B", branch, f"origin/{branch}"]),
        (f"git reset --hard origin/{branch}", ["git", "reset", "--hard", f"origin/{branch}"]),
        ("git clean -xfd", ["git", "clean", "-xfd"]),
      ]
      for i, (msg, cmd) in enumerate(steps):
        jobs.progress(job, message=msg, current=i + 1, total=len(steps))
        rc = await jobs.stream_exec(job, cmd, cwd=repo_dir, timeout=120)
        if rc != 0:
          jobs.finish(job, ok=False, result=jobs.result_from_log(job, rc))
          return

      jobs.finish(job, ok=True, result=jobs.result_from_log(job, 0))
      return

    if action == "delete_all_videos":
      jobs.progress(job, message="delete videos", current=1, total=1)
      deleted = 0
      for path in ["/data/media/0/videos"]:
        if not os.path.isdir(path):
          continue
        for fn in glob.glob(os.path.join(path, "*")):
          try:
            os.remove(fn)
            deleted += 1
            jobs.append(job, f"deleted: {os.path.basename(fn)}")
          except Exception as e:
            jobs.append(job, f"delete error: {e}")
      result = {"ok": True, "out": f"deleted files: {deleted}"}
      jobs.finish(job, ok=True, result=result)
      return

    if action == "delete_all_logs":
      jobs.progress(job, message="delete logs", current=1, total=1)
      deleted = 0
      for path in ["/data/media/0/realdata"]:
        if not os.path.isdir(path):
          continue
        for name in os.listdir(path):
          full_path = os.path.join(path, name)
          try:
            if os.path.isfile(full_path) or os.path.islink(full_path):
              os.remove(full_path)
            elif os.path.isdir(full_path):
              shutil.rmtree(full_path)
            else:
              continue
            deleted += 1
            jobs.append(job, f"deleted: {name}")
          except Exception as e:
            jobs.append(job, f"delete error: {e}")
      result = {"ok": True, "out": f"deleted entries: {deleted}"}
      jobs.finish(job, ok=True, result=result)
      return

    if action == "send_tmux_log":
      jobs.progress(job, message="capture tmux", current=1, total=1)
      rc, out = await asyncio.to_thread(capture_tmux_log_sync)
      if rc != 0:
        jobs.finish(
          job,
          ok=False,
          result={"ok": False, "error": "tmux capture failed", "error_code": "TMUX_CAPTURE_FAIL", "out": out},
          error="tmux capture failed",
          error_code="TMUX_CAPTURE_FAIL",
        )
        return
      result = {"ok": True, "out": "tmux log captured", "file": "/download/tmux.log"}
      jobs.finish(job, ok=True, result=result)
      return

    if action == "server_tmux_log":
      jobs.progress(job, message="send tmux", current=1, total=1)
      params = Params()
      params.put_nonblocking("CarrotException", "tmux_send")
      jobs.finish(job, ok=True, result={"ok": True, "out": "tmux send triggered"})
      return

    if action == "install_required":
      import importlib.util

      packages = [
        {"pip": "flask", "import": "flask"},
        {"pip": "shapely", "import": "shapely"},
        {"pip": "kaitaistruct", "import": "kaitaistruct"},
      ]
      results = []
      installed_any = False

      for idx, item in enumerate(packages, start=1):
        pip_name = item["pip"]
        import_name = item["import"]
        jobs.progress(job, message=f"checking {pip_name}", current=idx - 1, total=len(packages))

        if importlib.util.find_spec(import_name) is not None:
          results.append({"package": pip_name, "status": "already_installed"})
          jobs.append(job, f"{pip_name}: already installed")
          continue

        jobs.progress(job, message=f"installing {pip_name}", current=idx, total=len(packages))
        jobs.append(job, f"$ pip install {pip_name}")
        rc = await jobs.stream_exec(job, ["pip", "install", pip_name], timeout=300)
        results.append({"package": pip_name, "status": "installed" if rc == 0 else "failed", "returncode": rc})
        if rc != 0:
          jobs.finish(
            job,
            ok=False,
            result={
              "ok": False,
              "error": f"pip install failed: {pip_name}",
              "results": results,
              "need_reboot": False,
            },
            error=f"pip install failed: {pip_name}",
          )
          return
        installed_any = True

      result = {
        "ok": True,
        "out": "required packages installed. reboot is required to apply changes." if installed_any else "all required packages are already installed.",
        "results": results,
        "need_reboot": installed_any,
      }
      jobs.finish(job, ok=True, result=result)
      return

    if action == "backup_settings":
      if not HAS_PARAMS or ParamKeyType is None:
        jobs.finish(
          job,
          ok=False,
          result={"ok": False, "error": "Params/ParamKeyType not available"},
          error="Params/ParamKeyType not available",
        )
        return

      jobs.progress(job, message="backup settings", current=1, total=1)
      values = get_all_param_values_for_backup()
      os.makedirs(os.path.dirname(PARAMS_BACKUP_PATH), exist_ok=True)
      with open(PARAMS_BACKUP_PATH, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=False, indent=2)
      result = {"ok": True, "out": f"backup saved ({len(values)} keys)", "file": "/download/params_backup.json"}
      jobs.finish(job, ok=True, result=result)
      return

    if action == "reset_calib":
      jobs.progress(job, message="reset calibration", current=1, total=1)
      for f in ["/data/params/d_tmp/CalibrationParams", "/data/params/d/CalibrationParams"]:
        try:
          os.remove(f)
          jobs.append(job, f"removed {f}")
        except FileNotFoundError:
          pass
        except Exception as e:
          jobs.append(job, f"error removing {f}: {e}")
      jobs.finish(job, ok=True, result={"ok": True, "out": "calibration reset"})
      await asyncio.sleep(1)
      subprocess.Popen(["sudo", "reboot"])
      return

    if action == "reboot":
      jobs.progress(job, message="request reboot", current=1, total=1)
      subprocess.Popen(["sudo", "reboot"])
      jobs.finish(job, ok=True, result={"ok": True, "out": "reboot requested"})
      return

    if action == "rebuild_all":
      jobs.progress(job, message="rebuild all", current=1, total=1)
      cmd = "cd /data/openpilot && scons -c && rm -rf prebuilt && sudo reboot"
      subprocess.Popen(["bash", "-lc", cmd])
      jobs.finish(job, ok=True, result={"ok": True, "out": "rebuild_all requested (clean + remove prebuilt + reboot)"})
      return

    if action == "shell_cmd":
      cmd_str = (body.get("cmd") or "").strip()
      if not cmd_str:
        jobs.finish(job, ok=False, result={"ok": False, "error": "missing cmd"}, error="missing cmd")
        return
      try:
        argv = shlex.split(cmd_str)
      except Exception:
        jobs.finish(job, ok=False, result={"ok": False, "error": "bad cmd format"}, error="bad cmd format")
        return
      if not argv:
        jobs.finish(job, ok=False, result={"ok": False, "error": "empty cmd"}, error="empty cmd")
        return

      alias_map = {
        "pull": ["git", "pull"],
        "status": ["git", "status"],
        "branch": ["git", "branch"],
        "log": ["git", "log"],
      }
      if argv[0] in alias_map:
        argv = alias_map[argv[0]] + argv[1:]

      validation_error = validate_shell_argv(argv)
      if validation_error:
        error, error_code, detail = validation_error
        jobs.finish(
          job,
          ok=False,
          result={
            "ok": False,
            "error": error,
            "error_code": error_code,
            "error_detail": detail,
          },
          error=error,
          error_code=error_code,
          error_detail=detail,
        )
        return

      jobs.progress(job, message=cmd_str, current=1, total=1)
      jobs.append(job, f"$ {cmd_str}")
      try:
        rc = await jobs.stream_exec(job, argv, cwd="/data/openpilot", timeout=10)
      except asyncio.TimeoutError:
        jobs.finish(
          job,
          ok=False,
          result={"ok": False, "error": "timeout", "error_code": "CMD_TIMEOUT"},
          error="timeout",
          error_code="CMD_TIMEOUT",
        )
        return
      result = {
        "ok": rc == 0,
        "out": (job.get("log") or "").strip() or "(no output)",
        "returncode": rc,
      }
      jobs.finish(job, ok=rc == 0, result=result)
      return

    jobs.finish(job, ok=False, result={"ok": False, "error": f"unknown action: {action}"}, error=f"unknown action: {action}")
  except asyncio.TimeoutError:
    if action in ("mapd_download", "mapd_cancel_download"):
      set_mapd_download_active(False)
    jobs.finish(
      job,
      ok=False,
      result={"ok": False, "error": "timeout", "error_code": "CMD_TIMEOUT"},
      error="timeout",
      error_code="CMD_TIMEOUT",
    )
  except Exception as e:
    if action in ("mapd_download", "mapd_cancel_download"):
      set_mapd_download_active(False)
    jobs.append(job, f"\n{traceback.format_exc()}")
    jobs.finish(job, ok=False, result={"ok": False, "error": str(e)}, error=str(e))


async def dispatch_sync(request: web.Request, body: Dict[str, Any]) -> web.Response:
  action = normalize_action(body.get("action"))
  action_error = validate_action(action)
  if action_error:
    error, error_code = action_error
    return web.json_response({"ok": False, "error": error, "error_code": error_code}, status=400)

  def run(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
    return p.returncode, out.strip()

  try:
    REPO_DIR = "/data/openpilot"

    if action == "mapd_status":
      status = await get_mapd_status()
      return web.json_response({"ok": True, "status": status})

    if action == "mapd_cancel_download":
      set_mapd_download_active(True)
      from cereal import messaging
      pm = messaging.PubMaster(["mapdIn"])
      for _ in range(3):
        send_mapd_input(pm, "cancelDownload")
        await asyncio.sleep(0.1)
      set_mapd_download_active(False)
      return web.json_response({"ok": True, "out": "map download cancel requested"})

    if action == "git_pull":
      rc, out = run(["git", "pull"], cwd=REPO_DIR)
      if rc == 0 and did_git_pull_update(out):
        write_git_pull_time()
      return web.json_response({"ok": rc == 0, "rc": rc, "out": out})

    if action == "git_sync":
      rc1, out1 = run(["bash", "-lc", "git branch | grep -v '^\\*' | xargs -r git branch -D"], cwd=REPO_DIR)
      if rc1 != 0:
        return web.json_response({"ok": False, "rc": rc1, "out": out1})

      rc2, out2 = run(["git", "fetch", "--all", "--prune"], cwd=REPO_DIR)
      out = (out1 + "\n\n" + out2).strip()
      return web.json_response({"ok": rc2 == 0, "rc": rc2, "out": out})

    if action == "git_reset":
      mode = (body.get("mode") or "hard").strip()
      target = (body.get("target") or "HEAD").strip()
      if mode not in ("hard", "soft", "mixed"):
        return web.json_response({"ok": False, "error": "bad mode"}, status=400)
      rc, out = run(["git", "reset", f"--{mode}", target], cwd=REPO_DIR)
      return web.json_response({"ok": rc == 0, "rc": rc, "out": out})

    if action == "git_checkout":
      branch = (body.get("branch") or "").strip()
      kind = str(body.get("kind") or "").strip()
      item_name = str(body.get("name") or "").strip()
      item_remote = str(body.get("remote") or "").strip()
      if not branch and not item_name:
        return web.json_response({"ok": False, "error": "missing branch"}, status=400)

      rc_fetch, out_fetch = run(["git", "fetch", "--all", "--prune"], cwd=REPO_DIR)
      if rc_fetch != 0:
        return web.json_response({"ok": False, "rc": rc_fetch, "out": out_fetch})

      rc_remotes, out_remotes = run(["git", "remote"], cwd=REPO_DIR)
      known_remotes = out_remotes.split() if rc_remotes == 0 else ["origin"]
      remote_prefix = None
      for remote in known_remotes:
        if branch.startswith(f"{remote}/"):
          remote_prefix = remote
          break

      try:
        if kind == "local":
          local_branch = item_name or branch
          rc, out = run(["git", "switch", local_branch], cwd=REPO_DIR)
        elif kind == "remote":
          if not item_remote or not item_name:
            return web.json_response({"ok": False, "error": "missing remote branch info"}, status=400)
          if item_remote not in known_remotes:
            return web.json_response({"ok": False, "error": f"unknown remote: {item_remote}"}, status=400)
          branch = f"{item_remote}/{item_name}"
          local_branch = item_name
          rc_check, _ = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{local_branch}"], cwd=REPO_DIR)
          if rc_check == 0:
            rc, out = run(["git", "switch", local_branch], cwd=REPO_DIR)
          else:
            rc, out = run(
              ["git", "switch", "-c", local_branch, "--track", branch],
              cwd=REPO_DIR
            )
        elif remote_prefix is not None:
          local_branch = branch[len(remote_prefix) + 1:]
          rc_check, _ = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{local_branch}"], cwd=REPO_DIR)
          if rc_check == 0:
            rc, out = run(["git", "switch", local_branch], cwd=REPO_DIR)
          else:
            rc, out = run(
              ["git", "switch", "-c", local_branch, "--track", branch],
              cwd=REPO_DIR
            )
        else:
          rc, out = run(["git", "switch", branch], cwd=REPO_DIR)
          if rc != 0:
            rc2, out2 = run(
              ["git", "switch", "-c", branch, "--track", f"origin/{branch}"],
              cwd=REPO_DIR
            )
            rc, out = rc2, out2
        return web.json_response({"ok": rc == 0, "rc": rc, "out": out})
      except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    if action == "git_branch_list":
      rc0, out0 = run(["git", "fetch", "--all", "--prune"], cwd=REPO_DIR)
      if rc0 != 0:
        return web.json_response({"ok": False, "rc": rc0, "out": out0})

      rc_local, out_local = run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        cwd=REPO_DIR
      )
      rc_remote, out_remote = run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes"],
        cwd=REPO_DIR
      )
      if rc_local != 0 or rc_remote != 0:
        merged = (out0 + "\n\n" + out_local + "\n" + out_remote).strip()
        return web.json_response({"ok": False, "rc": rc_local if rc_local != 0 else rc_remote, "out": merged})

      rc_current, out_current = run(["git", "branch", "--show-current"], cwd=REPO_DIR)
      current_branch = out_current.strip() if rc_current == 0 else ""

      rc_remotes, out_remotes = run(["git", "remote"], cwd=REPO_DIR)
      remotes = out_remotes.split() if rc_remotes == 0 else ["origin"]
      rc_remote_urls, out_remote_urls = run(["git", "remote", "-v"], cwd=REPO_DIR)
      remote_urls = jobs.parse_remote_urls(out_remote_urls) if rc_remote_urls == 0 else {}
      branch_items = jobs.build_branch_items(out_local, out_remote, remotes)
      branches = sorted({str(item.get("ref") or "") for item in branch_items if item.get("ref")})

      return web.json_response({
        "ok": True,
        "branches": branches,
        "branch_items": branch_items,
        "current_branch": current_branch,
        "fetch": out0.strip(),
        "device_type": HARDWARE.get_device_type(),
        "branch_prefix": jobs.get_branch_prefix(),
        "remotes": remotes,
        "remote_urls": remote_urls,
      })

    if action == "git_remote_add":
      name = (body.get("name") or "").strip()
      url = (body.get("url") or "").strip()
      if not name or not url:
        return web.json_response({"ok": False, "error": "missing name or url"}, status=400)

      rc_remotes, out_remotes = run(["git", "remote"], cwd=REPO_DIR)
      remotes = out_remotes.split() if rc_remotes == 0 else []
      remote_exists = name in remotes
      setup_cmd = ["git", "remote", "set-url", name, url] if remote_exists else ["git", "remote", "add", name, url]
      rc_setup, out_setup = run(setup_cmd, cwd=REPO_DIR)
      if rc_setup != 0:
        return web.json_response({"ok": False, "rc": rc_setup, "out": out_setup})

      rc_fetch, out_fetch = run(["git", "fetch", "--prune", name], cwd=REPO_DIR)
      rc_remote_urls, out_remote_urls = run(["git", "remote", "-v"], cwd=REPO_DIR)
      out = (out_setup + "\n" + out_fetch + "\n\n> git remote -v\n" + (out_remote_urls if rc_remote_urls == 0 else "")).strip()
      return web.json_response({"ok": rc_fetch == 0, "rc": rc_fetch, "out": out})

    if action == "git_log":
      count = min(int(body.get("count") or 20), 50)
      rc, out = run(["git", "log", "--oneline", f"-{count}"], cwd=REPO_DIR)
      rc_head, out_head = run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_DIR)
      current_commit = out_head.strip() if rc_head == 0 else ""
      commits = []
      for line in (out or "").splitlines():
        line = line.strip()
        if not line:
          continue
        parts = line.split(" ", 1)
        commits.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
      return web.json_response({"ok": rc == 0, "commits": commits, "current_commit": current_commit, "out": out})

    if action == "git_reset_repo_fetch":
      url = "https://github.com/ajouatom/openpilot.git"
      out_all = ""

      rc_set, out_set = run(["git", "remote", "set-url", "origin", url], cwd=REPO_DIR)
      if rc_set != 0:
        run(["git", "remote", "remove", "origin"], cwd=REPO_DIR)
        rc_add, out_add = run(["git", "remote", "add", "origin", url], cwd=REPO_DIR)
        out_all += f"> git remote add origin {url}\n{out_add}\n\n"
        if rc_add != 0:
          return web.json_response({"ok": False, "error": f"failed to configure remote: {out_add}"})
      else:
        out_all += f"> git remote set-url origin {url}\n{out_set}\n\n"

      rc_rem, out_rem = run(["git", "remote"], cwd=REPO_DIR)
      for rname in (out_rem or "").splitlines():
        rname = rname.strip()
        if rname and rname != "origin":
          run(["git", "remote", "remove", rname], cwd=REPO_DIR)
          out_all += f"> removed remote: {rname}\n"

      rc_fetch, out_fetch = run(["git", "fetch", "origin", "--prune"], cwd=REPO_DIR)
      out_all += f"> git fetch origin --prune\n{out_fetch}\n\n"
      if rc_fetch != 0:
        return web.json_response({"ok": False, "rc": rc_fetch, "out": out_all.strip()})

      rc_br, out_br = run(["git", "branch", "-r"], cwd=REPO_DIR)
      branches = []
      for line in (out_br or "").splitlines():
        line = line.strip()
        if not line or "->" in line:
          continue
        if not line.startswith("origin/"):
          continue
        branches.append(line.split("/", 1)[1])
      branches = sorted(set(branches))
      return web.json_response({"ok": True, "branches": branches, "out": out_all.strip()})

    if action == "git_reset_repo_checkout":
      branch = str(body.get("branch") or "").strip()
      if not branch:
        return web.json_response({"ok": False, "error": "missing branch"}, status=400)
      commands = [
        ["git", "checkout", "-B", branch, f"origin/{branch}"],
        ["git", "reset", "--hard", f"origin/{branch}"],
        ["git", "clean", "-xfd"],
      ]
      out_all = ""
      for c in commands:
        rc, out = run(c, cwd=REPO_DIR)
        out_all += f"> {' '.join(c)}\n{out}\n\n"
        if rc != 0:
          return web.json_response({"ok": False, "rc": rc, "out": out_all.strip()})
      return web.json_response({"ok": True, "out": out_all.strip()})

    if action == "delete_all_videos":
      paths = ["/data/media/0/videos"]
      deleted = 0
      for pth in paths:
        if not os.path.isdir(pth):
          continue
        for fn in glob.glob(os.path.join(pth, "*")):
          try:
            os.remove(fn)
            deleted += 1
          except Exception:
            pass
      return web.json_response({"ok": True, "out": f"deleted files: {deleted}"})

    if action == "delete_all_logs":
      paths = ["/data/media/0/realdata"]
      deleted = 0
      for pth in paths:
        if not os.path.isdir(pth):
          continue

        for name in os.listdir(pth):
          full_path = os.path.join(pth, name)
          try:
            if os.path.isfile(full_path) or os.path.islink(full_path):
              os.remove(full_path)
              deleted += 1
            elif os.path.isdir(full_path):
              shutil.rmtree(full_path)
              deleted += 1
          except Exception as e:
            print("delete error:", e)

      return web.json_response({"ok": True, "out": f"deleted entries: {deleted}"})

    if action == "send_tmux_log":
      rc, out = capture_tmux_log_sync()
      if rc != 0:
        return web.json_response({"ok": False, "error": "tmux capture failed", "error_code": "TMUX_CAPTURE_FAIL", "out": out})

      return web.json_response({
        "ok": True,
        "out": "tmux log captured",
        "file": "/download/tmux.log",
      })

    if action == "server_tmux_log":
      params = Params()
      params.put_nonblocking("CarrotException", "tmux_send")
      return web.json_response({"ok": True, "out": "tmux send triggered"})

    if action == "install_required":
      import importlib.util

      packages = [
        {"pip": "flask", "import": "flask"},
        {"pip": "shapely", "import": "shapely"},
        {"pip": "kaitaistruct", "import": "kaitaistruct"},
      ]

      results = []
      installed_any = False

      for item in packages:
        pip_name = item["pip"]
        import_name = item["import"]

        try:
          if importlib.util.find_spec(import_name) is not None:
            results.append({"package": pip_name, "status": "already_installed"})
            continue

          cmd = ["pip", "install", pip_name]
          p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

          results.append({
            "package": pip_name,
            "status": "installed" if p.returncode == 0 else "failed",
            "returncode": p.returncode,
            "stdout": (p.stdout or "")[-2000:],
            "stderr": (p.stderr or "")[-2000:],
          })

          if p.returncode != 0:
            return web.json_response({
              "ok": False,
              "error": f"pip install failed: {pip_name}",
              "results": results,
              "need_reboot": False,
            }, status=500)

          installed_any = True

        except Exception as e:
          return web.json_response({
            "ok": False,
            "error": f"exception while checking/installing {pip_name}: {str(e)}",
            "results": results,
            "need_reboot": False,
          }, status=500)

      if installed_any:
        return web.json_response({
          "ok": True,
          "out": "required packages installed. reboot is required to apply changes.",
          "results": results,
          "need_reboot": True,
        })

      return web.json_response({
        "ok": True,
        "out": "all required packages are already installed.",
        "results": results,
        "need_reboot": False,
      })

    if action == "backup_settings":
      if not HAS_PARAMS or ParamKeyType is None:
        return web.json_response({"ok": False, "error": "Params/ParamKeyType not available"}, status=500)

      try:
        values = get_all_param_values_for_backup()
        os.makedirs(os.path.dirname(PARAMS_BACKUP_PATH), exist_ok=True)
        with open(PARAMS_BACKUP_PATH, "w", encoding="utf-8") as f:
          json.dump(values, f, ensure_ascii=False, indent=2)
        return web.json_response({"ok": True, "out": f"backup saved ({len(values)} keys)", "file": "/download/params_backup.json"})
      except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    if action == "reset_calib":
      out_msg = []
      for f in ["/data/params/d_tmp/CalibrationParams", "/data/params/d/CalibrationParams"]:
        try:
          os.remove(f)
          out_msg.append(f"removed {f}")
        except FileNotFoundError:
          pass
        except Exception as e:
          out_msg.append(f"error removing {f}: {e}")
      subprocess.Popen(["bash", "-lc", "sleep 1 && sudo reboot"])
      return web.json_response({"ok": True, "out": "\n".join(out_msg) or "calibration reset"})

    if action == "reboot":
      subprocess.Popen(["sudo", "reboot"])
      return web.json_response({"ok": True, "out": "reboot requested"})

    if action == "rebuild_all":
      cmd = "cd /data/openpilot && scons -c && rm -rf prebuilt && sudo reboot"
      subprocess.Popen(["bash", "-lc", cmd])
      return web.json_response({"ok": True, "out": "rebuild_all requested (clean + remove prebuilt + reboot)"})

    if action == "shell_cmd":
      cmd_str = (body.get("cmd") or "").strip()
      if not cmd_str:
        return web.json_response({"ok": False, "error": "missing cmd"}, status=400)

      try:
        argv = shlex.split(cmd_str)
      except Exception:
        return web.json_response({"ok": False, "error": "bad cmd format"}, status=400)

      if not argv:
        return web.json_response({"ok": False, "error": "empty cmd"}, status=400)

      alias_map = {
        "pull": ["git", "pull"],
        "status": ["git", "status"],
        "branch": ["git", "branch"],
        "log": ["git", "log"],
      }
      if argv[0] in alias_map:
        argv = alias_map[argv[0]] + argv[1:]

      validation_error = validate_shell_argv(argv)
      if validation_error:
        error, error_code, detail = validation_error
        return web.json_response({"ok": False, "error": error, "error_code": error_code, "error_detail": detail}, status=403)

      try:
        p = subprocess.run(
          argv,
          cwd="/data/openpilot",
          capture_output=True,
          text=True,
          timeout=10,
        )
        out = ""
        if p.stdout:
          out += p.stdout
        if p.stderr:
          out += ("\n" + p.stderr if out else p.stderr)
        out = out.strip() or "(no output)"
        return web.json_response({"ok": True, "out": out, "returncode": p.returncode})
      except subprocess.TimeoutExpired:
        return web.json_response({"ok": False, "error": "timeout"}, status=504)
      except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    return web.json_response({"ok": False, "error": f"unknown action: {action}"}, status=400)

  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=500)
