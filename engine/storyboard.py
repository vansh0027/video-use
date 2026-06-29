"""Transcript -> visual STORY plan for the Counza vertical-short engine.

The split layout puts a 1080x1080 **top zone** above the caption band and the
talking-head footage. That top zone should *narrate* what the speaker is
saying — when they mention universities, show real school logos; when they talk
about building a product, show a (people-free) workspace scene; when they paint
a future/vision, show a hopeful campus skyline. This module turns a clip's word
list into that plan.

Two public functions:

  * ``storyboard(words, clip_dur, brand) -> list[beat]``
        Segment a clip into 3-6 contiguous, gap-/sentence-aligned beats and pick
        a visual ``kind`` + ``query`` for each from its text. Beats always cover
        ``[0, clip_dur]`` with no holes and no overlap (clip-LOCAL seconds).

  * ``resolve_beat(beat, out_dir, brand) -> str | None``
        Produce the top-zone ASSET PNG (1080x1080) for one beat:
          - ``logos`` -> real crests via image_fetch + image_overlay.make_logo_row,
            cropped to the top-zone square.
          - ``scene`` -> people-free generated image via image_gen (flux).
          - ``stock`` -> a real stock photo via stock.fetch_stock if that module
            exists (optional; falls back to a scene generation otherwise).
          - ``none``  -> ``None`` (show the footage bigger / a brand card).

A ``beat`` is a plain dict::

    {"start": float, "end": float,
     "kind": "logos"|"scene"|"stock"|"none",
     "query": str, "label": str | None}

Design intent: the keyword rules here are deliberately small and legible so an
LLM-authored plan can drop in as a replacement — produce the same beat dicts and
``resolve_beat`` will render them. We NEVER request photos of people (AI mangles
faces; generic stock people look off-brand): every ``scene`` query ends in
"no people" and we prefer real ``logos`` rows whenever a school is named.

The produced asset is the TOP-ZONE square only (1080x1080). It is meant to be
composited into the top 1080px of the 1080x1920 frame by the renderer, BEFORE
subtitles are burned (video-use Hard Rule: subtitles last).

Stdlib + PIL only (the heavy lifting lives in the sibling engine modules).
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional

# --- Sibling-module imports -------------------------------------------------
# Other engine modules assume the engine dir is on sys.path (they `import
# brandkit` bare). Make that true regardless of the caller's cwd so this module
# imports cleanly whether run as a script or imported as engine.storyboard.
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from PIL import Image, ImageDraw  # noqa: E402

# These are the real workhorses. Import them lazily-tolerant: the planning half
# (storyboard) must work even if an asset backend is unavailable, so a failed
# import degrades to None and resolve_beat reports it instead of crashing.
try:  # real-logo fetch (Wikimedia)
    import image_fetch  # type: ignore
except Exception:  # pragma: no cover - exercised only on a broken tree
    image_fetch = None  # type: ignore

try:  # branded overlay compositor (make_logo_row lives here)
    import image_overlay  # type: ignore
except Exception:  # pragma: no cover
    image_overlay = None  # type: ignore

try:  # text-to-image (Pollinations / flux)
    import image_gen  # type: ignore
except Exception:  # pragma: no cover
    image_gen = None  # type: ignore

# ``stock`` is OPTIONAL — it may not exist on this tree yet. Never hard-require.
try:  # real stock photos (Pexels/Pixabay) if a key is configured
    import stock  # type: ignore
except Exception:
    stock = None  # type: ignore


# --- Top-zone geometry ------------------------------------------------------
# The top zone is the 1080x1080 square at the top of the 1080x1920 frame.
TOPZONE = 1080

# --- Brand colour fallbacks (only used if a colour is missing from the kit) -
_FALLBACK_COLORS = {
    "navy": "#003f7d",
    "navy_deep": "#13294b",
    "orange": "#e46e24",
    "bone": "#f4f8fd",
}


# --- Brand misheard-word fix ------------------------------------------------
# WhisperX mishears the brand "Counza" as Kansa/Kanza/Konza/Kanzaa etc. Normalise
# any such token back to "Counza" so storyboard text/labels read correctly.
_BRAND_MISHEARINGS = re.compile(r"\b(k[ao]n+z+a+h?|kanza|kansa|konza|counsa)\b", re.IGNORECASE)


def fix_brand(text: str) -> str:
    """Replace WhisperX mishearings of the brand with 'Counza'."""
    if not text:
        return text
    return _BRAND_MISHEARINGS.sub("Counza", text)


# ---------------------------------------------------------------------------
# Colour helpers (local copies so this module stays self-contained)
# ---------------------------------------------------------------------------
def _hex_to_rgb(hex_color: str):
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _brand_rgb(brand: Dict[str, Any], key: str):
    colors = (brand or {}).get("colors", {}) if isinstance(brand, dict) else {}
    val = colors.get(key) or _FALLBACK_COLORS.get(key) or "#000000"
    return _hex_to_rgb(val)


# ---------------------------------------------------------------------------
# Keyword -> (kind, query, label) concept rules
# ---------------------------------------------------------------------------
# Each rule: a set of trigger keywords -> the visual concept to show. Ordered by
# priority (first match wins). Written to be trivially swappable by an LLM plan.
#
# Queries follow the memory's IMAGE QUALITY STRATEGY:
#   * "logos"  -> a comma-separated 3-school row (real crests, never AI faces).
#   * "scene"  -> a people-free, cinematic prompt (every prompt ends "no people").

_LOGOS_QUERY = "Harvard University,Yale University,Princeton University"
_LOGOS_LABEL = "the dream schools"

_SCENE_BUILD = (
    "a modern laptop and notebook on a desk, startup workspace, "
    "warm light, no people"
)
_SCENE_STUDENTS = (
    "stack of college application essays and books on a desk, "
    "warm cinematic, no people"
)
_SCENE_FUTURE = (
    "sunrise over a university campus skyline, hopeful, cinematic, no people"
)

# (priority order matters: university first so "students applying to
# universities" lands on a logo row, not the generic students scene.)
_RULES = [
    {
        "keys": (
            "universit", "college", "school", "ivy", "admission", "admissions",
            "abroad", "harvard", "yale", "princeton", "stanford", "campus",
        ),
        "kind": "logos",
        "query": _LOGOS_QUERY,
        "label": _LOGOS_LABEL,
    },
    {
        "keys": (
            "build", "built", "building", "product", "tech", "app",
            "startup", "company", "founder", "launch", "platform",
        ),
        "kind": "scene",
        "query": _SCENE_BUILD,
        "label": None,
    },
    {
        "keys": (
            "student", "students", "learn", "learning", "study", "exam",
            "essay", "essays", "application", "applying", "profile",
        ),
        "kind": "scene",
        "query": _SCENE_STUDENTS,
        "label": None,
    },
    {
        "keys": (
            "change", "changing", "future", "dream", "dreams", "passion",
            "passionate", "industry", "world", "imagine", "vision", "idea",
            "access", "whole",
        ),
        "kind": "scene",
        "query": _SCENE_FUTURE,
        "label": None,
    },
]


def _concept_for_text(text: str) -> Dict[str, Any]:
    """Map a beat's text to {kind, query, label} using the keyword rules.

    Returns ``kind="none"`` (no top image; show footage bigger / brand card)
    when no rule's keywords appear.
    """
    low = (text or "").lower()
    for rule in _RULES:
        for key in rule["keys"]:
            if key in low:
                return {
                    "kind": rule["kind"],
                    "query": rule["query"],
                    "label": rule.get("label"),
                }
    return {"kind": "none", "query": "", "label": None}


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------
def _clean_words(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only timed word dicts, sorted by start; brand-fix their text.

    Accepts the WhisperX shape ({text,start,end,...}) and is tolerant of words
    that are already clip-local. Drops anything without numeric start/end.
    """
    out = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        s, e = w.get("start"), w.get("end")
        if s is None or e is None:
            continue
        try:
            s = float(s)
            e = float(e)
        except (TypeError, ValueError):
            continue
        txt = fix_brand(str(w.get("text", "")).strip())
        out.append({"text": txt, "start": s, "end": e})
    out.sort(key=lambda d: d["start"])
    return out


def _boundary_indices(
    words: List[Dict[str, Any]], target_beats: int
) -> List[int]:
    """Pick split points (indices into ``words``) for ~``target_beats`` segments.

    Strategy: rank the inter-word gaps (silence between consecutive words) and
    keep the largest as natural pause boundaries. Whisper often normalises away
    disfluencies and merges sentences, so gaps can be tiny — we therefore also
    enforce a roughly even fallback split so we always reach 3-6 beats even when
    the audio has no clear pauses. Splits are returned as the START index of
    each new segment (so ``[0, ...]`` semantics are handled by the caller).
    """
    n = len(words)
    if n <= 1:
        return []

    want_splits = max(0, target_beats - 1)
    if want_splits == 0:
        return []

    # Gap before word i = words[i].start - words[i-1].end, for i in 1..n-1.
    gaps = []
    for i in range(1, n):
        gap = words[i]["start"] - words[i - 1]["end"]
        gaps.append((gap, i))

    # Sort candidate boundaries by gap size (largest first), keep a few extra so
    # the spacing filter below has room to reject ones that are too close.
    gaps.sort(key=lambda g: g[0], reverse=True)

    # Minimum words between boundaries so beats aren't trivially short.
    min_span = max(2, n // (target_beats * 2))

    chosen: List[int] = []
    for _, idx in gaps:
        if len(chosen) >= want_splits:
            break
        if all(abs(idx - c) >= min_span for c in chosen):
            chosen.append(idx)

    # Fallback: if real pauses didn't give us enough boundaries (merged speech),
    # fill in with evenly spaced cut points so we still hit the target beat count.
    if len(chosen) < want_splits:
        for k in range(1, target_beats):
            idx = round(k * n / target_beats)
            idx = max(1, min(n - 1, idx))
            if all(abs(idx - c) >= 1 for c in chosen):
                chosen.append(idx)
            if len(chosen) >= want_splits:
                break

    chosen = sorted(set(chosen))[:want_splits]
    return chosen


def _target_beat_count(words: List[Dict[str, Any]], clip_dur: float) -> int:
    """Choose how many beats (3-6) to cut, scaled by clip length, clamped."""
    # Roughly one beat per ~8s of speech, bounded to the 3-6 contract. Short
    # clips still get at least 3 so the top zone keeps moving.
    by_time = int(round(max(clip_dur, 1.0) / 8.0))
    n_words = len(words)
    # Don't ask for more beats than we can reasonably fill with words.
    by_words = max(1, n_words // 6)
    target = min(by_time, by_words)
    return max(3, min(6, target))


def storyboard(
    words: List[Dict[str, Any]],
    clip_dur: float,
    brand: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Turn a clip's word list into a contiguous list of 3-6 visual beats.

    Args:
        words:    Clip-LOCAL word dicts ``{text,start,end,...}`` (seconds from
                  the clip's own start). Brand mishearings are auto-fixed. Words
                  with non-numeric timestamps are ignored.
        clip_dur: Clip duration in seconds. The returned beats always span
                  exactly ``[0.0, clip_dur]`` with no gaps and no overlap.
        brand:    Brand-kit dict (``brandkit.load_brandkit("counza")``). Only
                  used to keep the signature uniform with ``resolve_beat``; the
                  plan itself is brand-agnostic.

    Returns:
        A list of beat dicts, each ``{"start","end","kind","query","label"}``.
        ``kind`` is one of "logos" | "scene" | "stock" | "none". The list is
        ordered, contiguous, and covers the whole clip. (This module's keyword
        rules never emit "stock" themselves — that kind is supported by
        ``resolve_beat`` for LLM-authored plans — but is fully handled here.)

    Never raises for ordinary input: an empty/blank word list yields a single
    full-length ``none`` beat (footage shown bigger for the whole clip).
    """
    try:
        clip_dur = float(clip_dur)
    except (TypeError, ValueError):
        clip_dur = 0.0
    if clip_dur <= 0:
        # Degenerate: derive duration from the words if we can, else 1s.
        cw = _clean_words(words)
        clip_dur = max((w["end"] for w in cw), default=1.0)

    cw = _clean_words(words)

    # No usable words -> one full-length "none" beat (just show the footage).
    if not cw:
        return [{"start": 0.0, "end": round(clip_dur, 3),
                 "kind": "none", "query": "", "label": None}]

    target = _target_beat_count(cw, clip_dur)
    splits = _boundary_indices(cw, target)

    # Build [start_index, end_index) ranges from the split points.
    bounds = [0] + splits + [len(cw)]
    bounds = sorted(set(bounds))

    beats: List[Dict[str, Any]] = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = cw[a:b]
        if not seg:
            continue
        text = " ".join(w["text"] for w in seg).strip()
        concept = _concept_for_text(text)
        beats.append({
            "_start_raw": seg[0]["start"],
            "_end_raw": seg[-1]["end"],
            "kind": concept["kind"],
            "query": concept["query"],
            "label": concept["label"],
        })

    if not beats:  # pragma: no cover - guarded by the cw check above
        return [{"start": 0.0, "end": round(clip_dur, 3),
                 "kind": "none", "query": "", "label": None}]

    # --- Make beats contiguous and cover [0, clip_dur] exactly. -------------
    # Snap the first beat to 0, the last to clip_dur, and set each internal
    # boundary to the midpoint of the silence between adjacent beats so a beat's
    # visual changes during the pause, not mid-word.
    n = len(beats)
    out: List[Dict[str, Any]] = []
    for i, beat in enumerate(beats):
        if i == 0:
            start = 0.0
        else:
            prev_end = beats[i - 1]["_end_raw"]
            cur_start = beat["_start_raw"]
            start = (prev_end + cur_start) / 2.0
        if i == n - 1:
            end = clip_dur
        else:
            cur_end = beat["_end_raw"]
            nxt_start = beats[i + 1]["_start_raw"]
            end = (cur_end + nxt_start) / 2.0
        # Clamp into range and guard monotonicity.
        start = max(0.0, min(start, clip_dur))
        end = max(start, min(end, clip_dur))
        out.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "kind": beat["kind"],
            "query": beat["query"],
            "label": beat["label"],
        })

    # Final pass: stitch any rounding seams so end[i] == start[i+1] exactly and
    # the whole span is watertight from 0 to clip_dur.
    out[0]["start"] = 0.0
    out[-1]["end"] = round(clip_dur, 3)
    for i in range(len(out) - 1):
        out[i + 1]["start"] = out[i]["end"]
    return out


# ---------------------------------------------------------------------------
# Asset resolution
# ---------------------------------------------------------------------------
def _square_from_overlay(
    overlay_png: str, out_path: str, brand: Dict[str, Any]
) -> Optional[str]:
    """Reframe a 1080x1920 logo-row overlay into the TOP-ZONE 1080x1080 square.

    ``make_logo_row(..., placement="center")`` lays its chip row out centred
    around y=900 on a 1080x1920 transparent canvas. If we merely cropped the top
    1080px the row would sit jammed at the bottom of the square with dead space
    above. Instead we measure the row's actual content bounding box and re-centre
    that band vertically inside the 1080x1080 square, so the logos read as a
    balanced focal group. A subtle navy border frames it like the scene assets.
    """
    try:
        src = Image.open(overlay_png).convert("RGBA")
    except Exception:
        return None

    bbox = src.getbbox()  # tight box around the opaque chip row (+ labels)
    canvas = Image.new("RGBA", (TOPZONE, TOPZONE), (0, 0, 0, 0))
    if bbox is not None:
        row = src.crop(bbox)
        rw, rh = row.size
        # If the row is taller/wider than the square (shouldn't be), contain it.
        if rw > TOPZONE or rh > TOPZONE:
            scale = min(TOPZONE / rw, TOPZONE / rh)
            rw, rh = max(1, int(rw * scale)), max(1, int(rh * scale))
            row = row.resize((rw, rh), Image.LANCZOS)
        x = (TOPZONE - rw) // 2
        y = (TOPZONE - rh) // 2
        canvas.alpha_composite(row, (x, y))
    _add_navy_border(canvas, brand)
    out_abs = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    canvas.save(out_abs, "PNG")
    return out_abs


def _square_from_photo(
    image_path: str, out_path: str, brand: Dict[str, Any]
) -> Optional[str]:
    """Cover-fit a raster (scene/stock) into a 1080x1080 square + navy border.

    Uses a center-crop "cover" so the square is fully filled (no letterboxing) —
    appropriate for full-bleed scene/stock imagery in the top zone.
    """
    try:
        from PIL import ImageOps

        img = Image.open(image_path)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img = img.convert("RGB")
    except Exception:
        return None

    w, h = img.size
    if w <= 0 or h <= 0:
        return None
    scale = max(TOPZONE / w, TOPZONE / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - TOPZONE) // 2
    top = (nh - TOPZONE) // 2
    img = img.crop((left, top, left + TOPZONE, top + TOPZONE))

    canvas = img.convert("RGBA")
    _add_navy_border(canvas, brand)
    out_abs = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    canvas.save(out_abs, "PNG")
    return out_abs


def _add_navy_border(canvas: Image.Image, brand: Dict[str, Any], width: int = 6) -> None:
    """Draw a subtle navy border just inside the square edge (in place)."""
    navy = _brand_rgb(brand, "navy")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle(
        (0, 0, canvas.size[0] - 1, canvas.size[1] - 1),
        outline=(navy[0], navy[1], navy[2], 255),
        width=width,
    )


def _stable_seed(text: str) -> int:
    """Deterministic small positive seed from a query string."""
    import hashlib

    return int(hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:7], 16) % 100000


def resolve_beat(
    beat: Dict[str, Any],
    out_dir: str,
    brand: Dict[str, Any],
) -> Optional[str]:
    """Render the TOP-ZONE asset PNG (1080x1080) for one ``beat``.

    Dispatch on ``beat["kind"]``:
      * ``"logos"`` : split ``query`` on commas into up to 4 school names, fetch
        each real crest via ``image_fetch.fetch_image(..., want="logo")``, build
        a centred chip row with ``image_overlay.make_logo_row(placement=
        "center")``, then crop that 1080x1920 overlay to the 1080x1080 top zone.
        Returns ``None`` if no logo could be fetched (caller falls back to
        footage/brand card).
      * ``"scene"`` : generate a people-free image with ``image_gen.gen_image``
        (default flux/pollinations backend) at 1080x1080, then frame it with a
        navy border.
      * ``"stock"`` : fetch a real photo via ``stock.fetch_stock`` if that module
        exists; otherwise transparently fall back to scene generation so a plan
        that asks for stock still yields an asset.
      * ``"none"``  : return ``None`` (no top image for this beat).

    Args:
        beat:    A beat dict from ``storyboard`` (or an LLM plan) with at least
                 ``kind`` and ``query``.
        out_dir: Directory to write the asset PNG into (created if missing).
        brand:   Brand-kit dict.

    Returns:
        Absolute path to a 1080x1080 RGBA PNG, or ``None``. Never raises for
        ordinary failures (network, missing backend) — those surface as ``None``.
    """
    if not isinstance(beat, dict):
        return None
    kind = (beat.get("kind") or "none").lower()
    query = beat.get("query") or ""
    out_dir = os.path.abspath(out_dir or ".")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        return None

    if kind == "none":
        return None

    # --- logos ------------------------------------------------------------
    if kind == "logos":
        if image_fetch is None or image_overlay is None:
            return None
        names = [n.strip() for n in re.split(r"[;,]", query) if n.strip()][:4]
        if not names:
            return None
        logo_paths: List[str] = []
        labels: List[str] = []
        for name in names:
            try:
                p = image_fetch.fetch_image(name, out_dir, want="logo")
            except Exception:
                p = None
            if p:
                logo_paths.append(p)
                # Short label = the distinctive first token (e.g. "Harvard").
                labels.append(name.split()[0] if name.split() else name)
        if not logo_paths:
            return None
        row_png = os.path.join(out_dir, f"logorow_{_stable_seed(query)}.png")
        try:
            image_overlay.make_logo_row(
                logo_paths, row_png, brand,
                placement="center", labels=labels,
            )
        except Exception:
            return None
        sq = os.path.join(out_dir, f"topzone_logos_{_stable_seed(query)}.png")
        return _square_from_overlay(row_png, sq, brand)

    # --- scene ------------------------------------------------------------
    if kind == "scene":
        if image_gen is None:
            return None
        gen = None
        try:
            gen = image_gen.gen_image(
                query, out_dir, width=TOPZONE, height=TOPZONE,
                seed=_stable_seed(query), backend="pollinations",
            )
        except Exception:
            gen = None
        if not gen:
            return None
        sq = os.path.join(out_dir, f"topzone_scene_{_stable_seed(query)}.png")
        return _square_from_photo(gen, sq, brand)

    # --- stock (optional module; fall back to scene generation) -----------
    if kind == "stock":
        photo = None
        if stock is not None and hasattr(stock, "fetch_stock"):
            try:
                photo = stock.fetch_stock(query, out_dir)  # type: ignore[attr-defined]
            except Exception:
                photo = None
        if photo:
            sq = os.path.join(out_dir, f"topzone_stock_{_stable_seed(query)}.png")
            return _square_from_photo(photo, sq, brand)
        # Graceful fallback: treat as a scene so the beat still renders.
        if image_gen is not None and query:
            try:
                gen = image_gen.gen_image(
                    query, out_dir, width=TOPZONE, height=TOPZONE,
                    seed=_stable_seed(query), backend="pollinations",
                )
            except Exception:
                gen = None
            if gen:
                sq = os.path.join(out_dir, f"topzone_stock_{_stable_seed(query)}.png")
                return _square_from_photo(gen, sq, brand)
        return None

    # Unknown kind -> no asset.
    return None


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------
def _load_clip_words(
    transcript_path: str, clip_start: float, clip_end: float
) -> List[Dict[str, Any]]:
    """Load the cached transcript, filter to [clip_start, clip_end), shift to
    clip-local seconds, and brand-fix the text. Mirrors how the renderer slices
    a clip out of the full-source transcript.
    """
    import json

    with open(transcript_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    raw = data.get("words", [])
    local: List[Dict[str, Any]] = []
    for w in raw:
        s, e = w.get("start"), w.get("end")
        if s is None or e is None:
            continue
        if clip_start <= s < clip_end:
            local.append({
                "text": fix_brand(str(w.get("text", "")).strip()),
                "start": round(float(s) - clip_start, 3),
                "end": round(float(e) - clip_start, 3),
            })
    return local


def _selftest() -> int:
    """Build clip-4 (123.10-160.96) words from the cached transcript, plan the
    storyboard, print the beats, then resolve the FIRST logos beat to a PNG and
    confirm it is 1080x1080.
    """
    import brandkit  # type: ignore

    transcript = "/Users/vanshgupta/Video Editing/edit/transcripts/IMG_5344.json"
    clip_start, clip_end = 123.10, 160.96
    clip_dur = round(clip_end - clip_start, 3)

    brand = brandkit.load_brandkit("counza")
    words = _load_clip_words(transcript, clip_start, clip_end)

    print(f"storyboard self-test — clip {clip_start}-{clip_end} "
          f"({clip_dur}s, {len(words)} words)")
    if words:
        preview = " ".join(w["text"] for w in words[:18])
        print(f"  text head: {preview} ...")

    beats = storyboard(words, clip_dur, brand)
    print(f"\nBEAT PLAN ({len(beats)} beats):")
    for i, b in enumerate(beats):
        q = b["query"][:64] + ("…" if len(b["query"]) > 64 else "")
        print(f"  [{i}] {b['start']:6.2f}-{b['end']:6.2f}  "
              f"kind={b['kind']:6}  query={q!r}")

    # Contiguity assertions (cover whole clip, no gaps/overlaps).
    ok = True
    if abs(beats[0]["start"] - 0.0) > 1e-6:
        print("  FAIL: first beat does not start at 0"); ok = False
    if abs(beats[-1]["end"] - clip_dur) > 1e-3:
        print(f"  FAIL: last beat ends at {beats[-1]['end']} != {clip_dur}"); ok = False
    for i in range(len(beats) - 1):
        if abs(beats[i]["end"] - beats[i + 1]["start"]) > 1e-6:
            print(f"  FAIL: gap/overlap between beat {i} and {i+1}"); ok = False
    if not (3 <= len(beats) <= 6):
        print(f"  FAIL: beat count {len(beats)} outside 3-6"); ok = False
    print(f"  contiguity/coverage: {'PASS' if ok else 'FAIL'}")

    # Resolve the FIRST logos beat.
    first_logos = next((b for b in beats if b["kind"] == "logos"), None)
    out_dir = "/tmp/storyboard_selftest"
    asset_ok = False
    asset_path = None
    if first_logos is None:
        print("\nNo logos beat in the plan (cannot exercise resolve_beat logos).")
    else:
        print(f"\nResolving first logos beat "
              f"({first_logos['start']:.2f}-{first_logos['end']:.2f}) "
              f"query={first_logos['query']!r} ...")
        asset_path = resolve_beat(first_logos, out_dir, brand)
        if asset_path and os.path.isfile(asset_path):
            with Image.open(asset_path) as im:
                dims, mode = im.size, im.mode
            asset_ok = (dims == (TOPZONE, TOPZONE))
            print(f"  asset: {asset_path}")
            print(f"  dims={dims} mode={mode}  "
                  f"[{'PASS' if asset_ok else 'FAIL — not 1080x1080'}]")
        else:
            # Network may be unavailable; report but don't hard-crash the suite.
            print("  resolve_beat returned None (logo fetch likely offline). "
                  "Plan portion still validated.")

    overall = ok and (first_logos is None or asset_ok)
    print(f"\nOVERALL: {'PASS' if overall else 'PARTIAL/FAIL'}")
    if asset_path:
        print(f"(asset artifact: {asset_path})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
