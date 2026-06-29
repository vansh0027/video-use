"""Zero-dependency ``.env`` loader for the content engine.

The engine reads API keys (PEXELS_API_KEY, PIXABAY_API_KEY, the cloud
image->video backends, ...) from ``os.environ``. Nothing in the toolchain pulls
in ``python-dotenv``, and the helpers are normally launched as bare
``python engine/<x>.py`` without the shell sourcing ``.env`` — so without this
shim the keys in ``~/Developer/video-use/.env`` are silently ignored and the
code falls back to keyless/lower-quality paths (e.g. Openverse instead of
Pexels).

:func:`load_env` walks up from this file to find the repo-root ``.env`` and
copies any keys that are **not already set** in the real environment into
``os.environ`` (the real environment always wins, so a shell export overrides
the file). It is idempotent and never raises — a missing or malformed ``.env``
just leaves the environment untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_env"]

_LOADED = False


def load_env(start: str | None = None) -> None:
    """Populate ``os.environ`` from the nearest ``.env`` (missing keys only).

    Idempotent (runs its file scan once per process) and exception-safe.
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True

    here = Path(start or __file__).resolve()
    for d in (here, *here.parents):
        env_path = d / ".env"
        if env_path.is_file():
            _parse_into_environ(env_path)
            return


def _parse_into_environ(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # Real environment wins; only fill in what isn't already set.
        if key and key not in os.environ:
            os.environ[key] = val
