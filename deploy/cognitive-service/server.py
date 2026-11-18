"""Entrypoint shim launching the pure-ASGI cognitive runtime (``app.main()``)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_APP_PATH = Path(__file__).resolve().with_name("app.py")
try:
    import app as _cog_app

    if Path(getattr(_cog_app, "__file__", "")).resolve() != _APP_PATH:
        raise ImportError("imported non-sibling app module")
except ImportError:
    _spec = importlib.util.spec_from_file_location("nce_cognitive_service_app", _APP_PATH)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Unable to load cognitive service app from {_APP_PATH}") from None
    _cog_app = importlib.util.module_from_spec(_spec)
    sys.modules.setdefault("nce_cognitive_service_app", _cog_app)
    _spec.loader.exec_module(_cog_app)

app = _cog_app.app
main = _cog_app.main

__all__ = ["app", "main"]

if __name__ == "__main__":
    sys.exit(main())
