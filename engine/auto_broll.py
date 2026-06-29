"""Auto-B-roll: pull real footage/imagery for a reel's concept moments.

Given the reel's words on the OUTPUT timeline, this segments the clip into
visual beats (via :mod:`storyboard`), then resolves a *small* number of them to
real assets and returns ``images`` overlay specs that ``build_short`` already
knows how to composite (``source:"file"`` inserts with ``at`` / ``duration`` /
``placement``).

Resolution prefers **real** imagery over generated, matching the project's
visual-usage rules — for college/admissions content, real campuses, crests and
student photos read as more credible than AI scenes:

  * ``logos``        -> real school crest via :func:`image_fetch.fetch_image`.
  * ``scene``/``stock`` -> real stock photo via :func:`stock.fetch_stock`
    (Pexels/Pixabay — live once a key is in .env), falling back to a generated
    people-free scene via :func:`image_gen.gen_image`.

Kept deliberately LIGHT (default 3 inserts, short) so talking-head reels stay in
the ~10-25% visual range and the founder's face still carries the trust. It adds
no on-screen TEXT, so it is safe on spoken_only face reels (B-roll is visual,
not a caption/label/card).

Everything is best-effort: any beat that can't be resolved is skipped, never
fatal. Returns ``[]`` when nothing usable is found or the optional deps/keys are
absent (the reel then renders exactly as before).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# All siblings are optional — degrade to "no auto b-roll" rather than break a
# render if a module is missing on this tree.
try:
    import storyboard as _storyboard
except Exception:  # pragma: no cover
    _storyboard = None
try:
    import stock as _stock
except Exception:  # pragma: no cover
    _stock = None
try:
    import image_fetch as _image_fetch
except Exception:  # pragma: no cover
    _image_fetch = None
try:
    import image_gen as _image_gen
except Exception:  # pragma: no cover
    _image_gen = None
try:
    import video_gen as _video_gen
except Exception:  # pragma: no cover
    _video_gen = None

__all__ = ["auto_broll_specs"]

# Vertical canvas (matches transforms / the rest of the engine).
_W, _H = 1080, 1920


def _select_beats(beats: List[Dict[str, Any]], max_inserts: int) -> List[Dict[str, Any]]:
    """Pick up to ``max_inserts`` visual beats, spread across the clip.

    Drops ``none`` beats, prefers the longer beats (more room for a cutaway),
    then re-sorts the chosen few back into time order.
    """
    visual = [b for b in beats if b.get("kind") and b["kind"] != "none"
              and str(b.get("query") or "").strip()]
    if not visual or max_inserts <= 0:
        return []
    # Prefer beats with more duration (a cutaway needs a beat to sit in), then
    # de-duplicate by query so we never show the same image twice in one reel.
    visual.sort(key=lambda b: float(b["end"]) - float(b["start"]), reverse=True)
    chosen: List[Dict[str, Any]] = []
    seen_queries: set = set()
    for b in visual:
        q = str(b.get("query") or "").strip().lower()
        if q in seen_queries:
            continue
        seen_queries.add(q)
        chosen.append(b)
        if len(chosen) >= max_inserts:
            break
    chosen.sort(key=lambda b: float(b["start"]))
    return chosen


def _resolve_asset(beat: Dict[str, Any], out_dir: str) -> Optional[str]:
    """Resolve one beat to a local image path, preferring REAL imagery."""
    kind = str(beat.get("kind") or "")
    query = str(beat.get("query") or "").strip()
    if not query:
        return None

    # Real school crest for a logos beat.
    if kind == "logos" and _image_fetch is not None:
        try:
            p = _image_fetch.fetch_image(query, out_dir, want="logo")
            if p and os.path.isfile(p):
                return p
        except Exception:
            pass

    # Real stock photo first (credibility), generated scene as fallback.
    if _stock is not None:
        try:
            p = _stock.fetch_stock(query, out_dir, orientation="portrait", want="photo")
            if p and os.path.isfile(p):
                return p
        except Exception:
            pass
    if _image_gen is not None:
        try:
            prompt = query if "no people" in query.lower() else f"{query}, no people"
            p = _image_gen.gen_image(prompt, out_dir, width=_W, height=_H)
            if p and os.path.isfile(p):
                return p
        except Exception:
            pass
    return None


def auto_broll_specs(
    words: List[Dict[str, Any]],
    clip_dur: float,
    brand: Dict[str, Any],
    *,
    max_inserts: int = 3,
    insert_dur: float = 2.2,
    out_dir: str = ".",
    placement: str = "full",
    motion: bool = False,
) -> List[Dict[str, Any]]:
    """Return overlay specs (``source:"file"``) for concept moments.

    Args:
        words:       reel words on the OUTPUT timeline ({text,start,end}).
        clip_dur:    total reel duration (seconds).
        brand:       brand-kit dict.
        max_inserts: cap on the number of B-roll cutaways (kept small).
        insert_dur:  how long each cutaway sits on screen (clamped to its beat).
        out_dir:     where resolved assets are written.
        placement:   overlay placement for the inserts ("full" by default).
        motion:      when True, ABSTRACT concept beats (storyboard ``scene``)
                     become a free MOVING clip via ``video_gen`` ``flux_morph``
                     (evolving generated B-roll, no paid key); the spec is then
                     marked ``"video": True``. Concrete concepts (real campuses /
                     crests / stock) stay as stills — morphing those looks wrong.

    Never raises. Returns ``[]`` when storyboard is unavailable, nothing is
    visual-worthy, or no asset resolves.
    """
    if _storyboard is None:
        return []
    # storyboard reads each word's "text"; the rest of build_short uses "word".
    # Normalize so either transcript shape feeds the concept rules.
    norm_words = []
    for w in (words or []):
        if not isinstance(w, dict):
            continue
        txt = w.get("text", w.get("word", ""))
        norm_words.append({"text": txt, "start": w.get("start"), "end": w.get("end")})
    try:
        beats = _storyboard.storyboard(norm_words, float(clip_dur), brand or {})
    except Exception:
        return []

    specs: List[Dict[str, Any]] = []
    os.makedirs(out_dir, exist_ok=True)
    for beat in _select_beats(beats, max_inserts):
        b_start = float(beat["start"])
        b_len = max(0.0, float(beat["end"]) - b_start)
        # Sit the cutaway near the start of its beat, never longer than the beat.
        dur = max(0.8, min(float(insert_dur), b_len if b_len > 0 else float(insert_dur)))

        is_video = False
        path = None
        # Abstract concept + motion -> a free moving generated clip.
        if motion and beat.get("kind") == "scene" and _video_gen is not None:
            try:
                clip = _video_gen.gen_video(
                    prompt=str(beat.get("query") or ""), out_dir=out_dir,
                    duration=round(dur, 3), width=_W, height=_H,
                    seed=int(round(b_start * 10)) % 100000, backend="flux_morph",
                )
                if clip and os.path.isfile(clip):
                    path, is_video = clip, True
            except Exception:
                path = None
        if not path:
            path = _resolve_asset(beat, out_dir)   # still fallback
        if not path:
            continue

        specs.append({
            "source": "file",
            "file": path,
            "video": is_video,
            "at": round(b_start, 3),
            "duration": round(dur, 3),
            "placement": ("full" if is_video else
                          ("under_caption" if beat.get("kind") == "logos" else placement)),
            "label": beat.get("label") or beat.get("query"),
            "_auto_broll": True,
        })
    return specs


if __name__ == "__main__":  # tiny smoke test (offline-safe: prints, fetches if keys)
    import json
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    demo_words = [
        {"text": "applying", "start": 0.2, "end": 0.8},
        {"text": "to", "start": 0.8, "end": 0.9},
        {"text": "college", "start": 0.9, "end": 1.5},
        {"text": "is", "start": 1.6, "end": 1.8},
        {"text": "broken", "start": 1.8, "end": 2.4},
        {"text": "so", "start": 4.0, "end": 4.2},
        {"text": "we", "start": 4.2, "end": 4.4},
        {"text": "are", "start": 4.4, "end": 4.6},
        {"text": "building", "start": 4.6, "end": 5.2},
        {"text": "a", "start": 5.2, "end": 5.3},
        {"text": "product", "start": 5.3, "end": 6.0},
    ]
    out = os.environ.get("OUT", "/tmp/auto_broll_demo")
    specs = auto_broll_specs(demo_words, 8.0, {}, max_inserts=2, out_dir=out)
    print(json.dumps(specs, indent=2))
    print(f"[auto_broll] {len(specs)} insert(s) resolved into {out}")
