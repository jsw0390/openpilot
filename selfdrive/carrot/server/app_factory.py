from aiohttp import web

from .config import WEB_DIR
from .core import log_mw, on_startup, on_cleanup
from . import features


def make_app() -> web.Application:
  app = web.Application(middlewares=[log_mw])
  app.on_startup.append(on_startup)
  app.on_cleanup.append(on_cleanup)

  features.register_all(app)

  # foldered static assets — must be registered AFTER all explicit routes
  # so /api/..., /ws/..., /download/... win the match.
  app.router.add_static("/", str(WEB_DIR), show_index=True)
  return app
