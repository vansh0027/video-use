#!/usr/bin/env python3
"""
engine/effects/speed_ramp.py
============================

Speed-change filter builders: constant-rate speed segments and dead-air
compression. Every function is **pure** — it returns ``-vf`` / ``-af`` filter
strings (or a small plan) and renders nothing.

The two operations
------------------
* :func:`speed_segment` — change a clip's playback rate by a constant factor.
  Video uses ``setpts=PTS/rate``; audio uses an :func:`atempo_chain` so the
  pitch is **preserved** (atempo time-stretches without the chipmunk effect).
  ``rate=2.0`` = 2x faster; ``rate=0.5`` = half speed (slow-mo).

* :func:`dead_air_speedup` — compress low-energy stretches (pauses, "ums",
  reach-for-water moments) by speeding *just those spans* up. Returns a
  per-span plan plus the ``setpts``/``atempo`` strings, so the caller can either
  pre-cut the spans and apply :func:`speed_segment` to each, or build a single
  ``filter_complex`` that trims, speeds, and concats them. This is the
  "jump-cut without the cut" look — momentum without losing continuity.

Why atempo, not asetrate
------------------------
``asetrate`` would resample and shift pitch (voice goes high/low). ``atempo``
stretches time at constant pitch but is limited to the range ``[0.5, 2.0]`` per
instance, so larger factors are achieved by **chaining** multiple ``atempo``
stages whose product equals the target (e.g. 4x = ``atempo=2.0,atempo=2.0``).
:func:`atempo_chain` builds the minimal valid chain for any positive factor.

Hard-rule alignment
-------------------
* Audio still gets its 30 ms boundary fades and final loudnorm downstream — this
  module only changes *rate*, not levels.
* When a sped span is concatenated back, the caller must re-derive segment
  durations from the NEW (post-speed) length: ``new_dur = old_dur / rate``.
  :func:`scaled_duration` does this so caption/SFX timestamps stay in sync.

Usage
-----
    from engine.effects import speed_ramp as sr

    vf, af = sr.speed_segment(1.5)          # 1.5x, pitch-correct
    # ffmpeg ... -vf <vf> -af <af> ...

    plan = sr.dead_air_speedup(
        low_energy_spans=[(2.1, 3.4), (8.0, 9.2)], rate=2.5, clip_dur=12.0)
    # plan["keep"] / plan["speed"] spans + filtergraph helpers

Run directly to print specs:

    python engine/effects/speed_ramp.py
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Sequence, Tuple

__all__ = [
    "atempo_chain",
    "speed_segment",
    "scaled_duration",
    "dead_air_speedup",
    "ATEMPO_MIN",
    "ATEMPO_MAX",
]

# atempo's per-instance valid factor range.
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0


def _fmt(x: float) -> str:
    return f"{float(x):.6f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------- #
# atempo chaining (pitch-preserving audio rate)
# --------------------------------------------------------------------------- #
def atempo_chain(rate: float) -> str:
    """Return a comma-joined ``atempo`` chain whose product equals ``rate``.

    A single ``atempo`` only accepts ``[0.5, 2.0]``. For factors outside that
    range we chain stages: e.g. ``rate=4`` -> ``atempo=2,atempo=2``; ``rate=0.25``
    -> ``atempo=0.5,atempo=0.5``. The pitch is preserved at every stage.

    Args:
        rate: positive playback-rate factor (>1 faster, <1 slower).

    Returns:
        A filter string like ``"atempo=2,atempo=1.5"`` (here for rate=3).

    Raises:
        ValueError: if ``rate`` <= 0.
    """
    rate = float(rate)
    if rate <= 0:
        raise ValueError(f"rate must be > 0, got {rate}")
    if rate == 1.0:
        return "atempo=1"

    stages: List[float] = []
    remaining = rate
    # Peel off max-magnitude stages until what's left is in-range.
    while remaining > ATEMPO_MAX:
        stages.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN:
        stages.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN
    stages.append(remaining)
    return ",".join(f"atempo={_fmt(s)}" for s in stages)


# --------------------------------------------------------------------------- #
# constant-rate speed segment
# --------------------------------------------------------------------------- #
def speed_segment(rate: float) -> Tuple[str, str]:
    """Return ``(vf, af)`` to play a clip at a constant ``rate`` with kept pitch.

    * video: ``setpts=PTS/rate`` (faster rate -> smaller PTS -> quicker playback).
    * audio: :func:`atempo_chain` (constant pitch).

    Args:
        rate: playback factor. ``2.0`` = 2x speed; ``0.5`` = slow-motion.

    Returns:
        ``(video_filter, audio_filter)`` strings for ``-vf`` / ``-af``.

    Raises:
        ValueError: if ``rate`` <= 0.
    """
    rate = float(rate)
    if rate <= 0:
        raise ValueError(f"rate must be > 0, got {rate}")
    vf = f"setpts={_fmt(1.0 / rate)}*PTS"
    af = atempo_chain(rate)
    return vf, af


def scaled_duration(old_dur: float, rate: float) -> float:
    """Return the new duration of a span played at ``rate`` (``old_dur / rate``)."""
    rate = float(rate)
    if rate <= 0:
        raise ValueError(f"rate must be > 0, got {rate}")
    return round(float(old_dur) / rate, 6)


# --------------------------------------------------------------------------- #
# dead-air speed-up
# --------------------------------------------------------------------------- #
def _merge_spans(
    spans: Sequence[Tuple[float, float]],
    clip_dur: float,
    min_span: float,
) -> List[Tuple[float, float]]:
    """Clamp, drop sub-``min_span`` spans, sort, and merge overlaps."""
    cleaned: List[Tuple[float, float]] = []
    for a, b in spans or []:
        a = max(0.0, float(a))
        b = min(float(clip_dur), float(b))
        if b - a >= min_span:
            cleaned.append((a, b))
    cleaned.sort()
    merged: List[Tuple[float, float]] = []
    for a, b in cleaned:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def dead_air_speedup(
    low_energy_spans: Sequence[Tuple[float, float]],
    rate: float = 2.5,
    clip_dur: float = 0.0,
    min_span_s: float = 0.4,
) -> Dict[str, Any]:
    """Plan a "jump-cut without the cut": speed up low-energy spans, keep the rest 1x.

    Given the spans of a clip flagged as low-energy (pauses, filler, dead air)
    and the clip's total duration, this returns a plan that partitions the clip
    into alternating **keep** (1x) and **speed** (``rate``x) spans covering
    ``[0, clip_dur]`` with no gaps, plus the filter strings for the sped spans
    and the new total duration.

    The caller renders this one of two ways:

      1. **Per-span extract + concat** (lossless-friendly, matches the engine's
         per-segment pattern): for each ``keep`` span copy at 1x; for each
         ``speed`` span apply ``speed_segment(rate)``; concat in order. This is
         the recommended path — it composes with the existing concat demuxer and
         keeps every boundary independently faded.

      2. **Single filter_complex**: ``trim``+``setpts``/``atempo`` each span and
         ``concat`` — handy for a one-shot render but harder to fade per-segment.

    Args:
        low_energy_spans: ``[(start, end), ...]`` seconds in the SOURCE clip.
        rate: how fast to play the dead-air spans (>1). 2.5 is a good default —
            fast enough to feel like momentum, slow enough not to glitch.
        clip_dur: total source-clip duration (seconds). Required to compute the
            trailing keep span and the new total length.
        min_span_s: ignore flagged spans shorter than this (not worth a ramp).

    Returns:
        ``dict`` with:
          * ``"rate"``        — the applied rate.
          * ``"speed"``       — merged ``[(a, b), ...]`` spans to speed up.
          * ``"keep"``        — complementary ``[(a, b), ...]`` 1x spans.
          * ``"vf"`` / ``"af"`` — the ``speed_segment(rate)`` filter strings to
                                  apply to each *speed* span.
          * ``"new_total_s"`` — output duration after compression.
          * ``"saved_s"``     — seconds removed vs. the original.

    Raises:
        ValueError: if ``clip_dur`` <= 0 or ``rate`` <= 0.
    """
    rate = float(rate)
    clip_dur = float(clip_dur)
    if clip_dur <= 0:
        raise ValueError("clip_dur must be > 0 (pass the source clip length)")
    if rate <= 0:
        raise ValueError(f"rate must be > 0, got {rate}")

    speed = _merge_spans(low_energy_spans, clip_dur, min_span_s)

    # Build the complementary keep spans across [0, clip_dur].
    keep: List[Tuple[float, float]] = []
    cursor = 0.0
    for a, b in speed:
        if a > cursor:
            keep.append((round(cursor, 6), round(a, 6)))
        cursor = b
    if cursor < clip_dur:
        keep.append((round(cursor, 6), round(clip_dur, 6)))

    vf, af = speed_segment(rate)

    sped_src = sum(b - a for a, b in speed)
    sped_out = scaled_duration(sped_src, rate) if sped_src else 0.0
    kept = sum(b - a for a, b in keep)
    new_total = round(kept + sped_out, 6)

    return {
        "rate": rate,
        "speed": speed,
        "keep": keep,
        "vf": vf,
        "af": af,
        "new_total_s": new_total,
        "saved_s": round(clip_dur - new_total, 6),
    }


# --------------------------------------------------------------------------- #
# CLI / smoke
# --------------------------------------------------------------------------- #
def _demo() -> int:
    print("# engine/effects/speed_ramp.py — specs\n")

    for r in (2.0, 1.5, 0.5, 4.0, 0.25, 3.0):
        print(f"atempo_chain({r}) = {atempo_chain(r)}")
    print()

    for r in (1.5, 2.0, 0.5):
        vf, af = speed_segment(r)
        print(f"speed_segment({r}):")
        print(f"  -vf {vf}")
        print(f"  -af {af}")
        print(f"  scaled_duration(4.0, {r}) = {scaled_duration(4.0, r)}s\n")

    plan = dead_air_speedup(
        low_energy_spans=[(2.1, 3.4), (3.3, 3.9), (8.0, 9.2)],
        rate=2.5, clip_dur=12.0,
    )
    print("dead_air_speedup(spans=[(2.1,3.4),(3.3,3.9),(8.0,9.2)], rate=2.5, clip=12.0):")
    print(f"  speed  = {plan['speed']}   (overlap (2.1,3.4)+(3.3,3.9) merged)")
    print(f"  keep   = {plan['keep']}")
    print(f"  vf     = {plan['vf']}")
    print(f"  af     = {plan['af']}")
    print(f"  new_total_s = {plan['new_total_s']}  saved_s = {plan['saved_s']}\n")

    # sanity: keep + speed spans tile [0, clip_dur] with no gaps/overlap
    spans = sorted(plan["keep"] + plan["speed"])
    cursor, ok = 0.0, True
    for a, b in spans:
        if abs(a - cursor) > 1e-6:
            ok = False
        cursor = b
    print(f"  partition covers [0,12.0] contiguously: {ok and abs(cursor-12.0)<1e-6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
