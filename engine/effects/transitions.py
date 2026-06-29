#!/usr/bin/env python3
"""
engine/effects/transitions.py
=============================

Transition filter-string builders for joining two clips (A -> B).

**Hard cuts are the default and should stay so ~90% of the time.** A transition
is a deliberate accent — a section break, a tone shift, a montage beat — not the
glue between every segment. Overusing dissolves/zooms reads as "wedding video".
These builders exist for the 10% where a transition earns its place.

Every function is **pure**: it returns the ``xfade``/filter *expression* plus the
offset math the caller needs, and renders nothing. Transitions operate on TWO
inputs in a ``-filter_complex`` graph, so each builder returns a small
:class:`Transition` describing the graph body, the required ``offset`` (when in
the combined timeline the blend begins), the ``duration`` of the blend, and the
output label.

The ``xfade`` filter contract (important offset math)
-----------------------------------------------------
``xfade=transition=<t>:duration=<d>:offset=<o>`` consumes input A and input B and
produces a single stream whose total length is::

    len(A) + len(B) - d

The blend happens over ``[o, o+d]`` on the OUTPUT timeline. The canonical setup
is ``offset = len(A) - d`` so B starts blending in exactly ``d`` seconds before A
ends (they overlap by ``d``). :func:`xfade_offset` computes this for you.

Both inputs must share resolution, SAR, fps and pixel format — xfade requires it.
In this engine every segment is already normalized to 1080x1920 @ setsar=1
(see transforms.normalize_vertical), so that precondition holds; we still emit a
defensive ``format=yuv420p,fps=...`` on each leg.

Presets
-------
* :func:`dissolve`           — straight cross-dissolve (``xfade=fade``). Tasteful,
                               slow, premium. The only transition for brand-film.
* :func:`fade_through_black` — A -> black -> B (``xfade=fadeblack``). A "scene
                               break" beat; good before an endcard.
* :func:`whip_pan`           — directional motion-blur slide (``xfade=slideleft``
                               / smoothleft etc. + a blur accent). Energetic; the
                               sizzle look. Pairs with a whoosh SFX.
* :func:`zoom_transition`     — punch-zoom blend (``xfade=zoomin``). Aggressive;
                               the fast-cut montage look. Pairs with a riser.

Usage
-----
    from engine.effects import transitions as tr

    t = tr.dissolve(len_a=4.0, duration=0.5)
    # graph wiring (A=input 0, B=input 1):
    #   -filter_complex "[0:v][1:v]<t.expr>[vout]"
    # t.offset == 3.5, t.out_label == "[vout]"

Run directly to print specs and build a REAL 2-clip xfade on synthetic sources:

    python engine/effects/transitions.py            # print specs
    python engine/effects/transitions.py --render <out.mp4>   # ffmpeg + ffprobe
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "Transition",
    "xfade_offset",
    "dissolve",
    "fade_through_black",
    "whip_pan",
    "zoom_transition",
    "DEFAULT_DURATION",
    "WHY_HARD_CUTS",
]

# Sensible blend length. Short enough to feel intentional, long enough to read.
DEFAULT_DURATION: float = 0.4

# A reminder string the integration layer can surface to keep transitions rare.
WHY_HARD_CUTS = (
    "Default to HARD cuts. Use a transition only as a deliberate section "
    "accent (~10% of boundaries): tone shift, montage beat, pre-endcard. "
    "Dissolving every cut reads as amateur."
)

# Canonical fps/pixel format every leg is coerced to before xfade.
_FPS = 30
_PIXFMT = "yuv420p"

# Directions for the whip-pan slide. xfade ships smooth* and plain slide* wipes.
_WHIP_XFADE = {
    "left": "smoothleft",
    "right": "smoothright",
    "up": "smoothup",
    "down": "smoothdown",
}


@dataclass
class Transition:
    """A planned A->B transition.

    Attributes:
        expr: the complete ``-filter_complex`` body. It already references the
            two raw video inputs ``[0:v]`` (clip A) and ``[1:v]`` (clip B) — it
            normalizes each leg, xfades them, and emits :attr:`out_label`. The
            :attr:`graph` property is an alias for ``expr`` (the two-input case).
        offset: when (seconds, OUTPUT timeline) the blend begins. For the
            standard overlap this is ``len_a - duration``.
        duration: blend length in seconds.
        out_label: the produced video label to ``-map`` / chain (``[vout]``).
        kind: the xfade transition token used (for logging / SFX pairing).
        total_out: total output length = ``len_a + len_b - duration``.
        suggested_sfx: a cue name (sfx.py) that pairs with this transition, or
            ``None``. Purely advisory.
    """
    expr: str
    offset: float
    duration: float
    out_label: str
    kind: str
    total_out: float
    suggested_sfx: Optional[str] = None

    @property
    def graph(self) -> str:
        """The full two-input ``-filter_complex`` graph.

        Alias for :attr:`expr`, which already references ``[0:v]`` and ``[1:v]``
        directly (each leg is normalized inside the graph), so no extra input
        wiring is needed. Map :attr:`out_label` from the result.
        """
        return self.expr


def xfade_offset(len_a: float, duration: float) -> float:
    """Return the xfade ``offset`` so B starts blending ``duration`` s before A ends.

    ``offset = len_a - duration``. Raises if the blend cannot fit inside A.
    """
    len_a = float(len_a)
    duration = float(duration)
    if duration <= 0:
        raise ValueError(f"duration must be > 0, got {duration}")
    if duration >= len_a:
        raise ValueError(
            f"duration {duration}s must be shorter than clip A ({len_a}s)"
        )
    return round(len_a - duration, 6)


def _legs() -> str:
    """Defensive per-input normalization so xfade's equal-format precondition holds.

    Emits two labelled streams ``[a]`` and ``[b]`` from ``[0:v]``/``[1:v]``.
    """
    fmt = f"format={_PIXFMT},fps={_FPS},setsar=1"
    return f"[0:v]{fmt}[a];[1:v]{fmt}[b];"


def _build(
    kind: str,
    len_a: float,
    len_b: float,
    duration: float,
    pre: str = "",
    post: str = "",
    sfx: Optional[str] = None,
) -> Transition:
    """Shared assembly for the xfade-based presets.

    ``pre``/``post`` let a preset wrap the xfade with extra filters (e.g. the
    whip-pan's motion blur) on the produced stream.
    """
    off = xfade_offset(len_a, duration)
    d_str = f"{duration:.6f}".rstrip("0").rstrip(".")
    o_str = f"{off:.6f}".rstrip("0").rstrip(".")
    # Normalize both legs, then xfade [a][b], then optional post-blur.
    xf = (
        f"[a][b]xfade=transition={kind}:duration={d_str}:offset={o_str}"
    )
    if post:
        expr = f"{_legs()}{xf}[xf];[xf]{post}[vout]"
    else:
        expr = f"{_legs()}{xf}[vout]"
    if pre:
        # pre is rare; prepend before the legs (currently unused by presets).
        expr = pre + expr
    return Transition(
        expr=expr,
        offset=off,
        duration=duration,
        out_label="[vout]",
        kind=kind,
        total_out=round(len_a + len_b - duration, 6),
        suggested_sfx=sfx,
    )


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #
def dissolve(
    len_a: float,
    len_b: float = 4.0,
    duration: float = DEFAULT_DURATION,
) -> Transition:
    """Straight cross-dissolve (``xfade=fade``). The premium / brand-film accent.

    Slow and tasteful — keep ``duration`` >= 0.4 s for the "film" feel. No SFX
    pairing by default (a dissolve should breathe).
    """
    return _build("fade", len_a, len_b, duration, sfx=None)


def fade_through_black(
    len_a: float,
    len_b: float = 4.0,
    duration: float = 0.6,
) -> Transition:
    """A fades to black, then B fades up (``xfade=fadeblack``).

    A hard "scene break". Slightly longer default duration so the black beat
    registers. Great immediately before an endcard or a chapter change.
    """
    return _build("fadeblack", len_a, len_b, duration, sfx="impact")


def whip_pan(
    len_a: float,
    len_b: float = 4.0,
    duration: float = 0.25,
    direction: str = "left",
    blur: float = 18.0,
) -> Transition:
    """Directional whip-pan: a fast slide wipe + a motion-blur smear on the blend.

    Uses ``xfade=smooth<dir>`` for the directional slide and adds a horizontal
    (or vertical) ``boxblur`` accent on the produced stream so the swipe smears
    like a real whip-pan. Short by default (0.25 s) — it should feel snappy.
    Pairs with a ``whoosh`` SFX.

    Args:
        direction: ``left`` / ``right`` / ``up`` / ``down`` (camera motion).
        blur: box-blur radius for the smear (px). 0 disables the blur accent.

    Raises:
        ValueError: on unknown direction or negative blur.
    """
    if direction not in _WHIP_XFADE:
        raise ValueError(
            f"direction must be one of {sorted(_WHIP_XFADE)}, got {direction!r}"
        )
    if blur < 0:
        raise ValueError(f"blur must be >= 0, got {blur}")
    kind = _WHIP_XFADE[direction]
    # Smear along the axis of travel: horizontal blur for left/right, else vertical.
    if blur > 0:
        if direction in ("left", "right"):
            post = f"boxblur=lr={blur:.1f}:cr=0:ar=0"
        else:
            post = f"boxblur=lr=0:cr=0:ar={blur:.1f}"
    else:
        post = ""
    return _build(kind, len_a, len_b, duration, post=post, sfx="whoosh")


def zoom_transition(
    len_a: float,
    len_b: float = 4.0,
    duration: float = 0.3,
) -> Transition:
    """Punch-zoom blend (``xfade=zoomin``). The aggressive fast-cut/sizzle accent.

    B rushes in with a zoom while A blends out. Short and punchy. Pairs with a
    ``riser`` (lead-in) or ``impact`` (landing) SFX.
    """
    return _build("zoomin", len_a, len_b, duration, sfx="riser")


# --------------------------------------------------------------------------- #
# CLI / smoke
# --------------------------------------------------------------------------- #
def _render_smoke(out_path: str) -> int:
    """Build a real 2-clip xfade on synthetic testsrc2 sources and probe it."""
    exe = shutil.which("ffmpeg") or "ffmpeg"
    len_a, len_b = 2.0, 2.0
    t = dissolve(len_a=len_a, len_b=len_b, duration=0.5)
    # Two synthetic lavfi sources at the canonical size/fps.
    src = (
        f"testsrc2=size=1080x1920:rate={_FPS}:duration={len_a}",
        f"testsrc2=size=1080x1920:rate={_FPS}:duration={len_b}",
    )
    cmd = [
        exe, "-hide_banner", "-y",
        "-f", "lavfi", "-i", src[0],
        "-f", "lavfi", "-i", src[1],
        "-filter_complex", t.graph,
        "-map", t.out_label,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-pix_fmt", _PIXFMT, out_path,
    ]
    print(f"  ffmpeg: {shlex.join(cmd)}\n")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not os.path.exists(out_path):
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        print(f"  RENDER FAILED:\n{tail}")
        return 1
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=width,height,codec_name",
         "-of", "default=nw=1", out_path],
        capture_output=True, text=True, check=False,
    )
    print(f"  OK wrote {out_path} ({os.path.getsize(out_path)} bytes)")
    print(f"  expected total_out={t.total_out}s")
    print("  ffprobe:")
    for line in (probe.stdout or "").strip().splitlines():
        print(f"    {line}")
    return 0


def _demo(render_out: Optional[str]) -> int:
    print("# engine/effects/transitions.py — specs\n")
    print(f"# {WHY_HARD_CUTS}\n")
    builders = [
        ("dissolve", dissolve(len_a=4.0, len_b=4.0)),
        ("fade_through_black", fade_through_black(len_a=4.0, len_b=4.0)),
        ("whip_pan(left)", whip_pan(len_a=4.0, len_b=4.0, direction="left")),
        ("zoom_transition", zoom_transition(len_a=4.0, len_b=4.0)),
    ]
    for label, t in builders:
        print(f"{label}:")
        print(f"  kind={t.kind}  offset={t.offset}s  dur={t.duration}s  "
              f"total_out={t.total_out}s  sfx={t.suggested_sfx}")
        print(f"  graph = {t.graph}\n")

    print(f"xfade_offset(len_a=4.0, duration=0.5) = {xfade_offset(4.0, 0.5)}\n")

    if render_out:
        print(f"# --render: building real xfade -> {render_out}\n")
        return _render_smoke(render_out)
    return 0


if __name__ == "__main__":
    out = None
    if "--render" in sys.argv:
        i = sys.argv.index("--render")
        out = sys.argv[i + 1] if i + 1 < len(sys.argv) else "/tmp/xfade_smoke.mp4"
    raise SystemExit(_demo(out))
