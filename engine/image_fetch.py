"""Fetch a real image or logo from Wikimedia for the Counza content engine.

This module pulls genuine, reusable imagery (school crests/seals, company
logos, landmark photos) from Wikimedia so overlays can show the *actual*
thing being talked about instead of an AI hallucination of it.

It talks to two public Wikimedia APIs, no key required:

  1. ``en.wikipedia.org`` *pageimages* — returns the lead/infobox image for a
     page title. For most institutions and companies that is exactly the
     logo, crest or seal.
  2. ``commons.wikimedia.org`` *imageinfo* — given a ``File:`` title, renders
     a raster ``thumburl`` at a requested width. This is how we turn the SVG
     crests Wikipedia loves to serve into a flat PNG the ffmpeg overlay
     pipeline can composite.
  3. ``commons.wikimedia.org`` *search* (``srnamespace=6``) — a fallback that
     searches the ``File:`` namespace directly for "<query> logo" / "seal",
     used when the page lead image is missing or unsuitable.

Design goals:
  * **Never raise.** Every public path is wrapped; failures return ``None``.
  * **Stdlib-first.** Uses ``requests`` if present, else falls back to
    ``urllib`` so it works in a bare venv.
  * **Returns a local raster.** SVGs are always rasterised to PNG via the
    imageinfo thumb endpoint, because the downstream ffmpeg ``overlay`` path
    cannot read SVG.

Public API:
    fetch_image(query, out_dir, want="logo") -> str | None

Example:
    >>> fetch_image("Harvard University", "/tmp/imgs", want="logo")
    '/tmp/imgs/Harvard_University.png'
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Wikimedia asks every automated client to send a descriptive User-Agent.
# Requests without one are increasingly rejected with HTTP 403.
USER_AGENT = "CounzaEngine/1.0 (https://counza.com; video-use content engine)"

EN_WIKI_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Default raster width when turning an SVG (or any File:) into a PNG thumb.
# Wikimedia may round up to the next available rendering width; that is fine.
DEFAULT_THUMB_WIDTH = 600

# Network timeout (seconds) for every HTTP call.
HTTP_TIMEOUT = 20

# Raster image extensions we are happy to download as-is. Anything else
# (notably .svg) gets routed through the imageinfo thumb rasteriser.
_RASTER_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# When scoring File: search hits we reward names that look like the canonical
# brand mark and penalise derivative/era/variant cruft.
_GOOD_HINTS = ("logo", "seal", "crest", "coat of arms", "wordmark", "emblem", "shield")
_BAD_HINTS = (
    "press", "athletic", "sport", "football", "basketball", "hockey",
    "alumni", "club", "society", "department", "division", "school of",
    "museum", "library", "hospital", "history", "old", "former", "1",
    "building", "campus", "map", "flag",
)

# Try to use requests; gracefully degrade to urllib if it is not installed.
try:  # pragma: no cover - trivial import guard
    import requests as _requests  # type: ignore
except Exception:  # pragma: no cover
    _requests = None  # type: ignore


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (never raise)
# ---------------------------------------------------------------------------

def _http_get_json(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """GET ``url`` with query ``params`` and parse a JSON body.

    Returns the parsed dict, or ``None`` on any network / decode error.
    """
    try:
        full = f"{url}?{urllib.parse.urlencode(params)}"
        if _requests is not None:
            resp = _requests.get(
                url, params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            return resp.json()
        # urllib fallback.
        req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
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
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
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
        # A 0-byte file is a failure even if the request "succeeded".
        if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Turn a query into a filesystem-safe stem (e.g. 'Harvard_University')."""
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    cleaned = re.sub(r"[\s-]+", "_", cleaned)
    return cleaned or "image"


def _ext_from_url(url: str, default: str = ".png") -> str:
    """Return a lowercase file extension parsed from a URL path."""
    path = urllib.parse.urlparse(url).path
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    return ext if ext in _RASTER_EXTS else default


def _is_svg(url_or_title: str) -> bool:
    return url_or_title.lower().rstrip().endswith(".svg")


# ---------------------------------------------------------------------------
# Wikimedia API calls
# ---------------------------------------------------------------------------

def _pageimage_original(query: str) -> Optional[str]:
    """Return the *original* lead/infobox image URL for a Wikipedia page title.

    This is usually the logo / crest / seal for an institution or company.
    Returns the source URL (which may be an SVG), or ``None``.
    """
    data = _http_get_json(
        EN_WIKI_API,
        {
            "action": "query",
            "format": "json",
            "prop": "pageimages",
            "piprop": "original",
            "titles": query,
            "redirects": 1,
        },
    )
    if not data:
        return None
    try:
        pages = data["query"]["pages"]
    except (KeyError, TypeError):
        return None
    for page in pages.values():
        original = page.get("original") if isinstance(page, dict) else None
        if isinstance(original, dict):
            src = original.get("source")
            if isinstance(src, str) and src:
                return src
    return None


def _rasterize_file(file_title: str, width: int = DEFAULT_THUMB_WIDTH) -> Optional[str]:
    """Resolve a ``File:`` title to a raster PNG ``thumburl`` via imageinfo.

    Works for SVG (renders a PNG) and for oversized rasters (downscales).
    ``file_title`` may be given with or without the ``File:`` prefix.
    Returns a direct image URL, or ``None``.
    """
    if not file_title:
        return None
    if not file_title.lower().startswith("file:"):
        file_title = f"File:{file_title}"

    data = _http_get_json(
        COMMONS_API,
        {
            "action": "query",
            "format": "json",
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "iiurlwidth": int(width),
            "titles": file_title,
            "redirects": 1,
        },
    )
    if not data:
        return None
    try:
        pages = data["query"]["pages"]
    except (KeyError, TypeError):
        return None
    for page in pages.values():
        info = page.get("imageinfo") if isinstance(page, dict) else None
        if isinstance(info, list) and info:
            entry = info[0]
            # Prefer the rasterised thumb (always a PNG/JPG); fall back to the
            # raw url only if it is itself already a raster.
            thumb = entry.get("thumburl")
            if isinstance(thumb, str) and thumb:
                return thumb
            url = entry.get("url")
            if isinstance(url, str) and url and not _is_svg(url):
                return url
    return None


def _title_to_file_title(src_url: str) -> Optional[str]:
    """Derive a ``File:`` title from an upload.wikimedia.org source URL.

    e.g. '.../commons/c/cc/Harvard_University_coat_of_arms.svg'
         -> 'File:Harvard_University_coat_of_arms.svg'
    """
    try:
        name = os.path.basename(urllib.parse.urlparse(src_url).path)
        name = urllib.parse.unquote(name)
        if not name:
            return None
        return f"File:{name}"
    except Exception:
        return None


def _score_file_hit(title: str, query: str) -> float:
    """Heuristic score for a File: search result; higher is better.

    Rewards canonical-logo wording and an svg/png extension; penalises
    derivative/variant naming so we prefer the institution's primary mark.
    """
    t = title.lower()
    score = 0.0

    # Reward query-term coverage.
    for tok in re.findall(r"\w+", query.lower()):
        if len(tok) > 2 and tok in t:
            score += 1.0

    # Reward canonical-mark vocabulary.
    for hint in _GOOD_HINTS:
        if hint in t:
            score += 2.0

    # Penalise derivative/era/variant cruft.
    for bad in _BAD_HINTS:
        if bad in t:
            score -= 1.5

    # Prefer vector source (cleanest raster after rendering), then png.
    if t.endswith(".svg"):
        score += 1.5
    elif t.endswith(".png"):
        score += 1.0
    elif t.endswith((".jpg", ".jpeg")):
        score += 0.25

    # Mildly prefer shorter, less qualified names.
    score -= 0.02 * len(title)
    return score


def _search_file_namespace(query: str, want: str) -> List[str]:
    """Search the Commons ``File:`` namespace for logo/seal candidates.

    Returns an ordered list of ``File:`` titles, best first. Empty on failure.
    """
    suffixes = ["logo", "seal", "crest", "coat of arms"] if want == "logo" else [""]
    candidates: Dict[str, float] = {}

    for suffix in suffixes:
        srsearch = f"{query} {suffix}".strip()
        data = _http_get_json(
            COMMONS_API,
            {
                "action": "query",
                "format": "json",
                "list": "search",
                "srnamespace": 6,
                "srlimit": 12,
                "srsearch": srsearch,
            },
        )
        if not data:
            continue
        try:
            hits = data["query"]["search"]
        except (KeyError, TypeError):
            continue
        for hit in hits:
            title = hit.get("title") if isinstance(hit, dict) else None
            if not title:
                continue
            # Only consider real image files.
            if not title.lower().endswith(_RASTER_EXTS + (".svg",)):
                continue
            sc = _score_file_hit(title, query)
            # Keep the best score seen for a given title.
            if title not in candidates or sc > candidates[title]:
                candidates[title] = sc

    return [t for t, _ in sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_image(query: str, out_dir: str, want: str = "logo") -> Optional[str]:
    """Fetch a real image/logo for ``query`` from Wikimedia.

    Strategy:
      1. Look up the Wikipedia page lead image (``pageimages`` original). For
         institutions and companies this is usually the logo/crest/seal.
         - If it is an SVG, or ``want == "logo"`` (we always want a clean flat
           raster for logos), resolve the ``File:`` title and rasterise to PNG
           via the imageinfo thumb endpoint.
         - If it is already a raster and ``want != "logo"``, download directly.
      2. If step 1 yields nothing usable, search the Commons ``File:``
         namespace for "<query> logo" / "seal" / "crest" and rasterise the
         best-scoring candidate.

    Args:
        query:   Subject to fetch, e.g. "Harvard University", "Stripe".
        out_dir: Directory to download into (created if missing).
        want:    "logo" (default) to prefer a clean flat brand mark, or
                 "photo" to accept the lead image as-is when it is a raster.

    Returns:
        Absolute path to a downloaded raster image (PNG/JPG), or ``None`` if
        nothing could be fetched. Never raises.
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

        stem = _slugify(query)
        want_logo = (want or "logo").lower() == "logo"

        # --- Strategy 1: Wikipedia page lead image -------------------------
        src = _pageimage_original(query)
        if src:
            if _is_svg(src) or want_logo:
                # Route through the rasteriser. Derive the File: title from
                # the source URL so we render a clean PNG at our width.
                file_title = _title_to_file_title(src)
                raster_url = _rasterize_file(file_title, DEFAULT_THUMB_WIDTH) if file_title else None
                # If the lead image was already a raster, the imageinfo thumb
                # is a downscaled PNG/JPG; if rasterising failed but the
                # source is itself a raster, fall back to the raw source.
                if not raster_url and not _is_svg(src):
                    raster_url = src
                if raster_url:
                    dest = os.path.join(out_dir, f"{stem}{_ext_from_url(raster_url)}")
                    got = _http_download(raster_url, dest)
                    if got:
                        return got
            else:
                # want != logo and the lead image is already a raster: take it.
                dest = os.path.join(out_dir, f"{stem}{_ext_from_url(src)}")
                got = _http_download(src, dest)
                if got:
                    return got

        # --- Strategy 2: Commons File: namespace search --------------------
        for file_title in _search_file_namespace(query, "logo" if want_logo else "photo"):
            raster_url = _rasterize_file(file_title, DEFAULT_THUMB_WIDTH)
            if not raster_url:
                continue
            dest = os.path.join(out_dir, f"{stem}{_ext_from_url(raster_url)}")
            got = _http_download(raster_url, dest)
            if got:
                return got

        return None
    except Exception:
        # Absolute backstop: the contract is "never raise".
        return None


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------

def _describe(path: Optional[str]) -> str:
    """Return a human string with PIL dimensions for a downloaded file."""
    if not path:
        return "  -> FAILED (None)"
    try:
        from PIL import Image  # local import: pipeline always has pillow
        with Image.open(path) as im:
            size, mode, fmt = im.size, im.mode, im.format
        return f"  -> {path}\n     dims={size} mode={mode} format={fmt} bytes={os.path.getsize(path)}"
    except Exception as exc:  # pragma: no cover
        return f"  -> {path} (could not read with PIL: {exc})"


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/counza_image_fetch_test"
    queries = sys.argv[2:] or ["Harvard University", "Yale University"]

    print(f"image_fetch self-test  (out_dir={out})")
    print(f"requests available: {_requests is not None}")
    for q in queries:
        print(f"\n[{q}]")
        result = fetch_image(q, out, want="logo")
        print(_describe(result))
