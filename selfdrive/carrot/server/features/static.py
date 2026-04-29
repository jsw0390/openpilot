import os

from aiohttp import web

from ..config import JS_DIR, WEB_DIR


async def handle_index(request: web.Request) -> web.Response:
  return web.FileResponse(os.path.join(WEB_DIR, "index.html"))


# Legacy direct-file routes kept for backward compatibility.
async def handle_appjs(request: web.Request) -> web.Response:
  return web.FileResponse(os.path.join(JS_DIR, "app_core.js"))


def register(app: web.Application) -> None:
  app.router.add_get("/", handle_index)
  app.router.add_get("/app.js", handle_appjs)
