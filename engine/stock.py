"""Fetch a real STOCK photo (or stock video clip) for the Counza content engine.

Where ``image_fetch.py`` pulls *brand marks* from Wikimedia (logos, crests,
seals) and ``image_gen.py`` *synthesises* people-free scenes, this module
fetches **genuine photography** of a subject — a real university quad, a desk,
a city skyline — from whichever stock provider is configured, best-quality
first:

  1. **Pexels**   — needs ``PEXELS_API_KEY``.  Highest quality, curated, fully
     free for commercial use. Picks a large render (``src.large2x``).
  2. **Pixabay**  — needs ``PIXABAY_API_KEY``. Also free/commercial; picks the
     largest available URL (``largeImageURL``/``fullHDURL``/``webformatURL``).
  3. **Openverse** — keyless, no account. Aggregates openly-licensed media
     (mostly Wikimedia/Flickr). Quality is hit-or-miss, so it is the *last
     resort* and is flagged as such in logs and in the returned metadata.

Public API
----------
    fetch_stock(query, out_dir, orientation="landscape", want="photo")
        -> str | None
    fetch_stock_video(query, out_dir, orientation="landscape")
        -> str | None

Both return an **absolute local path** to a downloaded, validated file, or
``None`` if nothing usable could be fetched. Neither ever raises — exactly like
``image_fetch.fetch_image`` and ``image_gen.gen_image``, so callers branch on
``None`` instead of wrapping in try/except.

Design notes
------------
* **Never raise.** Every public path is wrapped; failures return ``None``.
* **Stdlib-first.** Uses ``requests`` if present, else falls back to ``urllib``
  so it works in a bare venv. Always sends a descriptive ``User-Agent``.
* **Validated downloads.** Photos are decoded + verified with PIL before being
  accepted, so an HTML error page returned with HTTP 200 never masquerades as a
  jpg. Videos are accepted on a non-trivial byte count + a video content-type /
  extension check (decoding video needs ffprobe, which is out of scope here).
* **Honest about source.** The chosen provider is printed, and for the lower
  -quality Openverse path the log says so explicitly.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# Load keys from the repo-root .env so PEXELS_API_KEY / PIXABAY_API_KEY are
# honoured even when launched as a bare `python engine/stock.py` (no shell
# sourcing). The real environment still wins. Never fatal if absent.
try:
    from _env import load_env as _load_env
    _load_env()
except Exception:  # pragma: no cover
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Stock APIs / Openverse all want a descriptive UA; some 403 without one.
USER_AGENT = "CounzaEngine/1.0 (https://counza.com; video-use content engine)"

PEXELS_PHOTO_API = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_API = "https://api.pexels.com/videos/search"
PIXABAY_PHOTO_API = "https://pixabay.com/api/"
PIXABAY_VIDEO_API = "https://pixabay.com/api/videos/"
OPENVERSE_IMAGE_API = "https://api.openverse.org/v1/images/"

# Network timeout (seconds) for every HTTP call. Stock renders are static
# files, so a single generous timeout is plenty; connect failures fail fast.
HTTP_TIMEOUT = 25

# How many candidates to ask each provider for (we then pick the best).
PER_PAGE = 5

# Raster extensions we will accept for a photo download.
_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# Container extensions we will accept for a video download.
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")

# Map our orientation vocabulary onto each provider's parameter spelling.
_PEXELS_ORIENT = {"landscape": "landscape", "portrait": "portrait", "square": "square"}
_PIXABAY_ORIENT = {"landscape": "horizontal", "portrait": "vertical", "square": "all"}

# Try requests; gracefully degrade to urllib if it is unavailable.
try:  # pragma: no cover - trivial import guard
    import requests as _requests  # type: ignore
except Exception:  # pragma: no cover
    _requests = None  # type: ignore


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (never raise)
# ---------------------------------------------------------------------------

def _http_get_json(
    url: str,
    params: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """GET ``url`` with query ``params`` and parse a JSON body.

    ``headers`` is merged over the default User-Agent (used to pass the Pexels
    ``Authorization`` key). Returns the parsed dict, or ``None`` on any
    network / HTTP / decode error.
    """
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    try:
        if _requests is not None:
            resp = _requests.get(url, params=params, headers=hdrs, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                return None
            return resp.json()
        # urllib fallback.
        full = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full, headers=hdrs)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as fh:
            if getattr(fh, "status", 200) != 200:
                return None
            raw = fh.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except Exception:
        return None


def _http_download(url: str, dest_path: str) -> Optional[str]:
    """Download binary ``url`` to ``dest_path``.

    Returns ``dest_path`` on success (non-empty file written), else ``None``.
    Never raises.
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
        if _requests is not None:
            resp = _requests.get(
                url, headers={"User-Agent": USER_AGENT},
                timeout=HTTP_TIMEOUT, stream=True,
            )
            if resp.status_code != 200:
                return None
            with open(dest_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r, open(dest_path, "wb") as fh:
                if getattr(r, "status", 200) != 200:
                    return None
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
        if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Naming / validation helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Turn a query into a filesystem-safe stem (e.g. 'university_campus')."""
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    cleaned = re.sub(r"[\s-]+", "_", cleaned)
    return (cleaned or "stock").lower()


def _ext_from_url(url: str, allowed: Tuple[str, ...], default: str) -> str:
    """Return a lowercase extension parsed from a URL path, restricted to
    ``allowed``; ``default`` if the URL has no usable extension."""
    try:
        path = urllib.parse.urlparse(url).path
        _, ext = os.path.splitext(path)
        ext = ext.lower()
        return ext if ext in allowed else default
    except Exception:
        return default


def _validate_photo(path: str) -> bool:
    """Return True iff ``path`` decodes as a real raster image via PIL.

    Guards against an HTML error page saved with HTTP 200. If PIL is somehow
    unavailable we degrade to a non-emptiness check rather than rejecting.
    """
    try:
        from PIL import Image  # local import: pipeline always has pillow
    except Exception:  # pragma: no cover - PIL expected in the venv
        return os.path.isfile(path) and os.path.getsize(path) > 0
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def _download_validated_photo(url: str, out_dir: str, stem: str) -> Optional[str]:
    """Download ``url`` to ``out_dir/stem.<ext>`` and accept it only if it is a
    real image. Removes a bad download so we never leave a corrupt file."""
    if not url:
        return None
    dest = os.path.join(out_dir, f"{stem}{_ext_from_url(url, _PHOTO_EXTS, '.jpg')}")
    got = _http_download(url, dest)
    if not got:
        return None
    if _validate_photo(got):
        return os.path.abspath(got)
    try:
        os.remove(got)
    except Exception:
        pass
    return None


def _looks_like_video(url: str, content_type: str = "") -> bool:
    """Heuristic: does this URL / content-type point at a real video file?"""
    if content_type and content_type.lower().startswith("video/"):
        return True
    return _ext_from_url(url, _VIDEO_EXTS, "") != ""


# ---------------------------------------------------------------------------
# Provider: Pexels (best quality; needs PEXELS_API_KEY)
# ---------------------------------------------------------------------------

def _pexels_photo(query: str, orientation: str) -> Optional[str]:
    """Return the best Pexels photo URL for ``query``, or ``None``.

    Picks the largest practical render: ``src.large2x`` (≈1880px wide) with a
    graceful walk down to ``large`` / ``original`` if a field is absent.
    """
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return None
    params = {
        "query": query,
        "per_page": PER_PAGE,
        "orientation": _PEXELS_ORIENT.get(orientation, "landscape"),
    }
    data = _http_get_json(PEXELS_PHOTO_API, params, headers={"Authorization": key})
    if not data:
        return None
    photos = data.get("photos") if isinstance(data, dict) else None
    if not isinstance(photos, list) or not photos:
        return None
    first = photos[0]
    src = first.get("src") if isinstance(first, dict) else None
    if not isinstance(src, dict):
        return None
    for field in ("large2x", "large", "original", "medium"):
        url = src.get(field)
        if isinstance(url, str) and url:
            return url
    return None


def _pexels_video(query: str, orientation: str) -> Optional[str]:
    """Return the best Pexels video-file URL for ``query``, or ``None``.

    Chooses the highest-resolution HD/SD ``.mp4`` rendition under 1920px wide
    (to keep the download sane) from the first matching clip.
    """
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return None
    params = {
        "query": query,
        "per_page": PER_PAGE,
        "orientation": _PEXELS_ORIENT.get(orientation, "landscape"),
    }
    data = _http_get_json(PEXELS_VIDEO_API, params, headers={"Authorization": key})
    if not data:
        return None
    videos = data.get("videos") if isinstance(data, dict) else None
    if not isinstance(videos, list) or not videos:
        return None
    first = videos[0]
    files = first.get("video_files") if isinstance(first, dict) else None
    if not isinstance(files, list) or not files:
        return None

    best_url: Optional[str] = None
    best_w = -1
    for f in files:
        if not isinstance(f, dict):
            continue
        link = f.get("link")
        if not isinstance(link, str) or not link:
            continue
        ftype = (f.get("file_type") or "").lower()
        if ftype and "mp4" not in ftype:
            continue
        w = f.get("width") or 0
        try:
            w = int(w)
        except (TypeError, ValueError):
            w = 0
        # Prefer the largest rendition that stays at/under 1920px wide.
        if w <= 1920 and w > best_w:
            best_w, best_url = w, link
    if best_url is None:
        # Nothing under the cap matched; just take the first link available.
        for f in files:
            if isinstance(f, dict) and isinstance(f.get("link"), str):
                return f["link"]
    return best_url


# ---------------------------------------------------------------------------
# Provider: Pixabay (free/commercial; needs PIXABAY_API_KEY)
# ---------------------------------------------------------------------------

def _pixabay_photo(query: str, orientation: str) -> Optional[str]:
    """Return the best Pixabay photo URL for ``query``, or ``None``."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return None
    params = {
        "key": key,
        "q": query,
        "image_type": "photo",
        "orientation": _PIXABAY_ORIENT.get(orientation, "horizontal"),
        "per_page": PER_PAGE,
        "safesearch": "true",
    }
    data = _http_get_json(PIXABAY_PHOTO_API, params)
    if not data:
        return None
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list) or not hits:
        return None
    first = hits[0]
    if not isinstance(first, dict):
        return None
    # Largest first. largeImageURL ≈1280px; fullHDURL/imageURL need full API.
    for field in ("largeImageURL", "fullHDURL", "imageURL", "webformatURL"):
        url = first.get(field)
        if isinstance(url, str) and url:
            return url
    return None


def _pixabay_video(query: str, orientation: str) -> Optional[str]:
    """Return the best Pixabay video URL for ``query``, or ``None``.

    Pixabay nests renditions under ``videos`` -> {large,medium,small,tiny} ->
    {url,width,...}; pick the largest with a real URL."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return None
    params = {
        "key": key,
        "q": query,
        "per_page": PER_PAGE,
        "safesearch": "true",
    }
    data = _http_get_json(PIXABAY_VIDEO_API, params)
    if not data:
        return None
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list) or not hits:
        return None
    first = hits[0]
    renditions = first.get("videos") if isinstance(first, dict) else None
    if not isinstance(renditions, dict):
        return None
    for field in ("large", "medium", "small", "tiny"):
        r = renditions.get(field)
        if isinstance(r, dict):
            url = r.get("url")
            if isinstance(url, str) and url:
                return url
    return None


# ---------------------------------------------------------------------------
# Provider: Openverse (keyless, lower quality — last resort)
# ---------------------------------------------------------------------------

def _openverse_score(title: str, query: str) -> float:
    """Reward query-term coverage in an Openverse result title."""
    t = (title or "").lower()
    score = 0.0
    for tok in re.findall(r"\w+", query.lower()):
        if len(tok) > 2 and tok in t:
            score += 1.0
    return score


def _openverse_photo(query: str) -> Optional[str]:
    """Return the most query-relevant Openverse image URL, or ``None``.

    Keyless and commercial-licensed, but quality is inconsistent (it mostly
    surfaces Wikimedia/Flickr), so this is only used when no keyed provider is
    configured. Picks the best title match among the first page of large hits.
    """
    params = {
        "q": query,
        "license_type": "commercial,modification",
        "size": "large",
        "page_size": PER_PAGE,
    }
    data = _http_get_json(OPENVERSE_IMAGE_API, params)
    if not data:
        return None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return None

    best_url: Optional[str] = None
    best_score = float("-inf")
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url")
        if not isinstance(url, str) or not url:
            continue
        sc = _openverse_score(r.get("title") or "", query)
        if sc > best_score:
            best_score, best_url = sc, url
    return best_url


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def fetch_stock(
    query: str,
    out_dir: str,
    orientation: str = "landscape",
    want: str = "photo",
) -> Optional[str]:
    """Fetch a real stock photo for ``query`` from the best available source.

    Tries providers in descending quality order — Pexels, then Pixabay (both
    keyed), then keyless Openverse as a flagged last resort — and returns the
    first that yields a downloadable, PIL-valid image.

    Args:
        query:       Subject to fetch, e.g. "university campus building".
        out_dir:     Directory to download into (created if missing).
        orientation: "landscape" (default), "portrait", or "square".
        want:        Reserved for API symmetry; only "photo" is meaningful here
                     (video has its own ``fetch_stock_video``). Anything else is
                     treated as "photo".

    Returns:
        Absolute path to a downloaded, validated image (jpg/png/webp), or
        ``None`` if nothing usable could be fetched. Never raises.
    """
    try:
        if not query or not isinstance(query, str):
            return None
        if not out_dir or not isinstance(out_dir, str):
            return None

        out_dir = os.path.abspath(out_dir)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            return None

        orientation = (orientation or "landscape").lower()
        stem = _slugify(query)

        # Provider chain: (human label, url-getter, is_lower_quality)
        chain = [
            ("pexels", lambda: _pexels_photo(query, orientation), False),
            ("pixabay", lambda: _pixabay_photo(query, orientation), False),
            ("openverse", lambda: _openverse_photo(query), True),
        ]

        for label, getter, low_quality in chain:
            try:
                url = getter()
            except Exception:
                url = None
            if not url:
                continue
            got = _download_validated_photo(url, out_dir, f"{stem}_{label}")
            if got:
                if low_quality:
                    print(
                        f"[stock] fetched via {label} (keyless, LOWER quality — "
                        f"set PEXELS_API_KEY/PIXABAY_API_KEY for better photos): {got}"
                    )
                else:
                    print(f"[stock] fetched via {label}: {got}")
                return got

        print(
            f"[stock] no stock photo found for {query!r} "
            f"(tried pexels, pixabay, openverse)"
        )
        return None
    except Exception:
        # Absolute backstop: the contract is "never raise".
        return None


def fetch_stock_video(
    query: str,
    out_dir: str,
    orientation: str = "landscape",
) -> Optional[str]:
    """Fetch a real stock video clip for ``query`` for b-roll.

    Only the keyed providers (Pexels, then Pixabay) supply video; if neither
    ``PEXELS_API_KEY`` nor ``PIXABAY_API_KEY`` is set this returns ``None``
    (Openverse has no usable keyless video search). The download is accepted on
    a video content-type / extension check — decoding the container to verify
    frames needs ffprobe, which is out of scope for this fetcher.

    Args:
        query:       Subject to fetch, e.g. "students walking on campus".
        out_dir:     Directory to download into (created if missing).
        orientation: "landscape" (default), "portrait", or "square".

    Returns:
        Absolute path to a downloaded video file, or ``None``. Never raises.
    """
    try:
        if not query or not isinstance(query, str):
            return None
        if not out_dir or not isinstance(out_dir, str):
            return None

        out_dir = os.path.abspath(out_dir)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            return None

        orientation = (orientation or "landscape").lower()
        stem = _slugify(query)

        chain = [
            ("pexels", lambda: _pexels_video(query, orientation)),
            ("pixabay", lambda: _pixabay_video(query, orientation)),
        ]

        for label, getter in chain:
            try:
                url = getter()
            except Exception:
                url = None
            if not url:
                continue
            dest = os.path.join(out_dir, f"{stem}_{label}{_ext_from_url(url, _VIDEO_EXTS, '.mp4')}")
            got = _http_download(url, dest)
            if not got:
                continue
            # Sanity-check: non-trivial size and a video-ish URL/extension.
            if os.path.getsize(got) > 8192 and _looks_like_video(url):
                print(f"[stock] fetched video via {label}: {got}")
                return os.path.abspath(got)
            try:
                os.remove(got)
            except Exception:
                pass

        print(
            f"[stock] no stock video for {query!r} "
            f"(needs PEXELS_API_KEY or PIXABAY_API_KEY)"
        )
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------

def _describe(path: Optional[str]) -> str:
    """Human string with PIL dimensions for a downloaded photo."""
    if not path:
        return "  -> FAILED (None)"
    try:
        from PIL import Image
        with Image.open(path) as im:
            size, mode, fmt = im.size, im.mode, im.format
        return (
            f"  -> {path}\n"
            f"     dims={size} mode={mode} format={fmt} bytes={os.path.getsize(path)}"
        )
    except Exception as exc:  # pragma: no cover
        return f"  -> {path} (could not read with PIL: {exc})"


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/counza_stock_test"
    query = sys.argv[2] if len(sys.argv) > 2 else "university campus building"

    print(f"stock self-test  (out_dir={out})")
    print(f"requests available: {_requests is not None}")
    print(f"PEXELS_API_KEY set:  {bool(os.environ.get('PEXELS_API_KEY'))}")
    print(f"PIXABAY_API_KEY set: {bool(os.environ.get('PIXABAY_API_KEY'))}")
    print(f"\n[photo] {query!r}")
    print(_describe(fetch_stock(query, out, orientation="landscape")))
    print(f"\n[video] {query!r}")
    vid = fetch_stock_video(query, out, orientation="landscape")
    print(f"  -> {vid}" if vid else "  -> None (no video provider key, expected)")
