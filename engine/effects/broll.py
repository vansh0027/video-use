#!/usr/bin/env python3
"""
engine/effects/broll.py
=======================

B-roll insert planner — the "say-it / show-it" pattern.

When the speaker NAMES a thing ("...our dashboard...", "...the acceptance
letter..."), you cut to a full-frame shot of that thing for a couple of seconds
while **keeping the A-roll audio** (the voice keeps talking underneath). The
viewer hears the point and sees the proof. This is the single highest-leverage
short-form move after tight pacing.

This module is **pure planning + string building**: given the A-roll output
timeline and a list of b-roll inserts (each: when on the A-roll, which asset,
how long), it returns a :class:`BrollPlan` describing the overlay/concat spec
the renderer should execute. It runs no ffmpeg.

Two render strategies (the plan supports both; the caller picks)
----------------------------------------------------------------
* **overlay** (recommended, matches the engine's final compositing pass):
  the b-roll clip is scaled to fill 1080x1920 and ``overlay``-ed on top of the
  A-roll, ``enable``-gated to ``[t0, t0+dur]``. The A-roll **audio is never
  touched** — only the picture is covered for that window. Because it's an
  overlay with an ``enable`` between, the b-roll's own audio is simply not
  mapped. This composes cleanly with the burn-subtitles-LAST rule: overlays go
  on BEFORE captions so a cutaway can never hide a caption (same ordering
  build_short.py already uses for image overlays).

* **splice** (insert as its own segment): if instead you want the b-roll to
  occupy a real slot in the concat list (e.g. it has matching length and you
  want lossless concat), the plan also yields the cut points so the caller can
  extract A[0:t0], the b-roll seg (muted, A-roll audio bridged), and A[t0+dur:].
  Overlay is simpler and preferred; splice exists for the lossless path.

Hard-rule alignment
-------------------
* B-roll **must use ``setpts``** to land on the right frame when overlaid in a
  filter graph (the engine's overlay convention). :func:`broll_overlay_filter`
  emits ``setpts=PTS-STARTPTS`` on the b-roll leg so it starts at its window.
* A-roll audio is preserved verbatim — the say-it/show-it contract. The b-roll
  audio is dropped (not mixed) unless the caller explicitly asks otherwise.
* Subtitles are still burned LAST by the renderer; b-roll overlays are part of
  the pre-caption composite, exactly like image overlays.

Usage
-----
    from engine.effects import broll

    plan = broll.plan_broll(
        aroll_dur=30.0,
        inserts=[
            {"t": 6.0, "dur": 2.5, "asset": "dash.mp4"},
            {"t": 14.0, "dur": 3.0, "asset": "letter.jpg"},
        ],
    )
    for ins in plan.inserts:
        print(ins.overlay_filter(base_label="[v]", broll_input=1))

Run directly to print an example plan + filters:

    python engine/effects/broll.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "BrollInsert",
    "BrollPlan",
    "plan_broll",
    "broll_overlay_filter",
    "TARGET_W",
    "TARGET_H",
]

# Canonical vertical canvas (match transforms.py).
TARGET_W = 1080
TARGET_H = 1920

# How an image (still) b-roll should be looped to cover its window.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _fmt(x: float) -> str:
    return f"{float(x):.6f}".rstrip("0").rstrip(".")


def broll_overlay_filter(
    t0: float,
    dur: float,
    base_label: str = "[0:v]",
    broll_input: int = 1,
    out_label: str = "[vbr]",
    target_w: int = TARGET_W,
    target_h: int = TARGET_H,
    is_image: bool = False,
) -> str:
    """Build the ``-filter_complex`` snippet that overlays one b-roll cutaway.

    The b-roll leg is scaled to **cover** the full frame (scale-to-fill then
    center-crop — never letterboxed), reset to start at the window via
    ``setpts=PTS-STARTPTS``, and overlaid on ``base_label`` only while
    ``enable='between(t,t0,t0+dur)'``. The A-roll audio is untouched (this is a
    video-only graph; the caller maps ``[0:a]`` straight through).

    Args:
        t0: window start on the A-roll OUTPUT timeline (seconds).
        dur: window length (seconds).
        base_label: label of the A-roll video stream to cover (``[0:v]`` or the
            label produced by a previous overlay in the chain).
        broll_input: ffmpeg input index of this b-roll asset.
        out_label: label to emit (chain the next overlay onto this).
        target_w, target_h: frame size to fill.
        is_image: if True, the source is a still — the caller must feed it with
            ``-loop 1 -t <dur>`` so it has frames to overlay.

    Returns:
        A ``-filter_complex`` body fragment ending in ``out_label``.

    Raises:
        ValueError: on non-positive duration.
    """
    if dur <= 0:
        raise ValueError(f"b-roll dur must be > 0, got {dur}")
    t1 = t0 + dur
    # Scale-to-fill (cover) then center-crop to exact frame — no letterbox.
    cover = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},setsar=1"
    )
    # PTS reset so the overlay frames begin at the window, and the enable gate.
    leg = f"[{broll_input}:v]{cover},setpts=PTS-STARTPTS[br{broll_input}]"
    over = (
        f"{base_label}[br{broll_input}]overlay=0:0:"
        f"enable='between(t,{_fmt(t0)},{_fmt(t1)})'{out_label}"
    )
    return f"{leg};{over}"


@dataclass
class BrollInsert:
    """One planned cutaway.

    Attributes:
        t: window start on the A-roll output timeline (seconds).
        dur: window length (seconds).
        asset: path to the b-roll clip or still image.
        is_image: True if ``asset`` is a still (needs ``-loop 1 -t dur``).
        broll_input: ffmpeg input index assigned to this asset by the planner.
    """
    t: float
    dur: float
    asset: str
    is_image: bool = False
    broll_input: int = 1

    @property
    def end(self) -> float:
        return round(self.t + self.dur, 6)

    def input_args(self) -> List[str]:
        """ffmpeg ``-i`` args for this asset (with ``-loop``/``-t`` for stills)."""
        if self.is_image:
            return ["-loop", "1", "-t", _fmt(self.dur), "-i", self.asset]
        return ["-i", self.asset]

    def overlay_filter(
        self,
        base_label: str = "[0:v]",
        out_label: str = "[vbr]",
    ) -> str:
        """The ``-filter_complex`` fragment that overlays this insert."""
        return broll_overlay_filter(
            self.t, self.dur, base_label=base_label,
            broll_input=self.broll_input, out_label=out_label,
            is_image=self.is_image,
        )


@dataclass
class BrollPlan:
    """The full b-roll plan for one A-roll output.

    Attributes:
        aroll_dur: total A-roll length (seconds).
        inserts: ordered, non-overlapping :class:`BrollInsert` list.
        dropped: inserts that were dropped (with a reason) during validation.
    """
    aroll_dur: float
    inserts: List[BrollInsert] = field(default_factory=list)
    dropped: List[Dict[str, Any]] = field(default_factory=list)

    def input_args(self) -> List[str]:
        """All extra ``-i`` args, in insert order (after the A-roll input 0)."""
        args: List[str] = []
        for ins in self.inserts:
            args += ins.input_args()
        return args

    def overlay_chain(self, base_label: str = "[0:v]") -> str:
        """Chain every insert's overlay into one ``-filter_complex`` body.

        Each overlay feeds the next (``[0:v]`` -> ``[vb0]`` -> ``[vb1]`` ...).
        The final label is :attr:`final_label`. Returns ``""`` if no inserts
        (caller then maps ``[0:v]`` directly).
        """
        if not self.inserts:
            return ""
        parts: List[str] = []
        cur = base_label
        for n, ins in enumerate(self.inserts):
            out = f"[vb{n}]"
            parts.append(ins.overlay_filter(base_label=cur, out_label=out))
            cur = out
        return ";".join(parts)

    @property
    def final_label(self) -> str:
        """Video label after all overlays (to ``-map``)."""
        return f"[vb{len(self.inserts)-1}]" if self.inserts else "[0:v]"


def plan_broll(
    aroll_dur: float,
    inserts: Sequence[Dict[str, Any]],
    min_dur: float = 0.6,
) -> BrollPlan:
    """Validate + assign inputs for a list of b-roll inserts (say-it/show-it).

    Inserts are sorted by ``t``, clamped to the A-roll length, dropped if too
    short or if they overlap an already-accepted insert (a cutaway can't cover
    another cutaway). Surviving inserts get sequential ffmpeg input indices
    starting at 1 (input 0 is the A-roll).

    Args:
        aroll_dur: total A-roll output length (seconds).
        inserts: list of ``{"t", "dur", "asset"}`` dicts. ``is_image`` is
            auto-detected from the asset extension unless given explicitly.
        min_dur: minimum cutaway length to keep (seconds).

    Returns:
        A :class:`BrollPlan`.

    Raises:
        ValueError: if ``aroll_dur`` <= 0.
    """
    aroll_dur = float(aroll_dur)
    if aroll_dur <= 0:
        raise ValueError("aroll_dur must be > 0")

    raw = sorted(
        (dict(i) for i in (inserts or []) if i.get("asset")),
        key=lambda i: float(i.get("t", 0.0)),
    )

    plan = BrollPlan(aroll_dur=aroll_dur)
    next_input = 1
    last_end = 0.0
    for i in raw:
        t = max(0.0, float(i.get("t", 0.0)))
        dur = float(i.get("dur", 0.0))
        asset = str(i["asset"])
        # clamp the window to the A-roll
        if t + dur > aroll_dur:
            dur = aroll_dur - t
        reason = None
        if dur < min_dur:
            reason = f"too short after clamp ({dur:.3f}s < {min_dur}s)"
        elif t < last_end:
            reason = f"overlaps previous insert (starts {t:.3f}s < {last_end:.3f}s)"
        if reason:
            plan.dropped.append({"t": t, "asset": asset, "reason": reason})
            continue
        ext = os.path.splitext(asset)[1].lower()
        is_image = bool(i["is_image"]) if "is_image" in i else (ext in _IMAGE_EXTS)
        plan.inserts.append(BrollInsert(
            t=round(t, 6), dur=round(dur, 6), asset=asset,
            is_image=is_image, broll_input=next_input,
        ))
        next_input += 1
        last_end = t + dur
    return plan


# --------------------------------------------------------------------------- #
# CLI / smoke
# --------------------------------------------------------------------------- #
def _demo() -> int:
    print("# engine/effects/broll.py — say-it/show-it plan\n")
    plan = plan_broll(
        aroll_dur=30.0,
        inserts=[
            {"t": 6.0, "dur": 2.5, "asset": "dash.mp4"},
            {"t": 14.0, "dur": 3.0, "asset": "letter.jpg"},   # auto image
            {"t": 15.0, "dur": 2.0, "asset": "overlap.mp4"},  # dropped: overlaps
            {"t": 28.0, "dur": 5.0, "asset": "tail.mp4"},     # clamped to 2.0s
        ],
    )
    print(f"aroll_dur = {plan.aroll_dur}s   accepted = {len(plan.inserts)}   "
          f"dropped = {len(plan.dropped)}\n")
    for ins in plan.inserts:
        print(f"  insert @ {ins.t}s for {ins.dur}s  asset={ins.asset}  "
              f"image={ins.is_image}  input={ins.broll_input}")
        print(f"    -i args : {' '.join(ins.input_args())}")
        print(f"    overlay : {ins.overlay_filter()}\n")
    for d in plan.dropped:
        print(f"  DROPPED @ {d['t']}s {d['asset']}: {d['reason']}")
    print()
    print("full input_args():")
    print(f"  {' '.join(plan.input_args())}\n")
    print("overlay_chain() (final_label = " + plan.final_label + "):")
    print(f"  {plan.overlay_chain()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
