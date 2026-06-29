"""Reusable ffmpeg video-filter builders for the content engine.

Every function in this module is **pure**: it returns an ffmpeg ``-vf`` filter
*string* and renders nothing. Compose the pieces with :func:`compose` and hand
the result to ``ffmpeg -vf`` (or to the engine's renderer) yourself. Keeping the
transforms side-effect-free means they are trivially unit-testable and can be
stitched into a larger filter graph without spawning a process.

Two transforms are provided:

  * :func:`normalize_vertical` — turn any source resolution into a centered
    1080x1920 (9:16) frame. Wider-than-9:16 sources are center-cropped then
    scaled; taller/narrower sources are scaled to fit the width and padded.

  * :func:`punch_in` — a subtle, *reliable* static push-in (scale-up + center
    crop). :func:`punch_in_animated` offers an optional ``zoompan`` ramp, but
    the static version is the default because it is robust across ffmpeg
    builds and never drifts or judders.

Design notes
------------
* All filters end with ``setsar=1`` where a resolution is produced so the
  pixel aspect ratio can never surprise a downstream concat/overlay.
* ``normalize_vertical`` decides crop-vs-pad from the *source* dimensions at
  build time (we know them from ffprobe), so the emitted filter contains plain
  integers — no ffmpeg expression evaluation, nothing to misparse.
* Even-number safety: scale targets are forced even (``ceil`` to a multiple of
  2) because most encoders reject odd dimensions on subsampled pixel formats.

Usage:
    python engine/transforms.py        # prints example filters for 1920x1080

Importable:
    from transforms import normalize_vertical, punch_in, compose
"""

from __future__ import annotations

from typing import Iterable

# Canonical vertical canvas for short-form (TikTok / Reels / Shorts).
TARGET_W = 1080
TARGET_H = 1920


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _even(n: float) -> int:
    """Round up to the nearest even integer (encoder-safe dimension)."""
    i = int(n)
    if i < n:  # we rounded down via int() truncation; bump up
        i += 1
    return i if i % 2 == 0 else i + 1


def compose(filters: Iterable[str]) -> str:
    """Join filter strings with commas, dropping empties/whitespace-only ones.

    Accepts any iterable of strings (a list is typical). ``None`` entries and
    blank strings are skipped so callers can build a chain with conditional
    pieces without littering the result with stray commas::

        compose([normalize_vertical(1920, 1080), grade or "", punch_in()])
    """
    parts = []
    for f in filters:
        if f is None:
            continue
        f = f.strip().strip(",").strip()
        if f:
            parts.append(f)
    return ",".join(parts)


# --------------------------------------------------------------------------- #
# normalize_vertical
# --------------------------------------------------------------------------- #
def normalize_vertical(
    src_w: int,
    src_h: int,
    target_w: int = TARGET_W,
    target_h: int = TARGET_H,
) -> str:
    """Build a filter that fits any source into a centered ``target_w``x``target_h`` frame.

    The target is 9:16 by default (1080x1920). The branch is chosen from the
    *source* aspect ratio:

    * **Source wider than target** (``src_w/src_h > target_w/target_h``):
      center-crop to the target aspect, then scale to the exact target size.
      Nothing is letterboxed — we fill the frame and lose the left/right edges.
      (MVP smart-center: crop is taken from the middle; no face tracking.)

    * **Source equal or taller/narrower**: scale to fit the target *width*,
      then pad top/bottom (or left/right, if it ends up wider) to reach the
      exact target size, centering the image. Nothing is cropped.

    Returns a single comma-joined filter string ending in ``setsar=1``. The
    emitted geometry is computed here in Python (we know the real source
    dimensions), so the filter contains concrete even integers.

    Raises:
        ValueError: if any dimension is not a positive integer.
    """
    for name, v in (("src_w", src_w), ("src_h", src_h),
                    ("target_w", target_w), ("target_h", target_h)):
        if not isinstance(v, int) or v <= 0:
            raise ValueError(f"{name} must be a positive int, got {v!r}")

    src_ar = src_w / src_h
    tgt_ar = target_w / target_h

    if src_ar > tgt_ar:
        # Source is too wide -> crop the sides to target aspect, then scale.
        # Crop height = full source height; crop width = src_h * tgt_ar.
        crop_w = _even(src_h * tgt_ar)
        crop_w = min(crop_w, src_w)  # never crop beyond the source
        crop_h = src_h
        return (
            f"crop={crop_w}:{crop_h}:(iw-{crop_w})/2:0,"
            f"scale={target_w}:{target_h},"
            f"setsar=1"
        )

    # Source is equal or too tall/narrow -> fit *inside* the frame (contain),
    # then pad the remaining axis. We must scale by whichever dimension would
    # otherwise overflow: scaling to the full width can produce a height taller
    # than the target (e.g. a 9:21 source), which `pad` rejects. So pick the
    # smaller scale factor — the classic letterbox/pillarbox "fit" — and pad
    # the leftover border, centered.
    scale_w = target_w / src_w
    scale_h = target_h / src_h
    if scale_w <= scale_h:
        # width is the binding constraint -> fill width, pad top/bottom
        scaled_w = target_w
        scaled_h = _even(src_h * scale_w)
        scaled_h = min(scaled_h, target_h)
    else:
        # height is the binding constraint -> fill height, pad left/right
        scaled_h = target_h
        scaled_w = _even(src_w * scale_h)
        scaled_w = min(scaled_w, target_w)
    return (
        f"scale={scaled_w}:{scaled_h},"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1"
    )


# --------------------------------------------------------------------------- #
# punch_in (static, reliable) + animated variant
# --------------------------------------------------------------------------- #
def punch_in(
    zoom: float = 1.12,
    target_w: int = TARGET_W,
    target_h: int = TARGET_H,
    fps: int = 30,  # accepted for API symmetry; unused in the static path
) -> str:
    """Static, rock-solid subtle push-in: scale the frame up, then center-crop back.

    A constant magnification reads as a gentle "punch in" on a cut without any
    motion math — there is no per-frame expression, so it cannot drift, judder,
    or depend on ffmpeg's ``zoompan`` quirks. This is the default because it
    *always* works.

    The input is assumed to already be ``target_w``x``target_h`` (e.g. the
    output of :func:`normalize_vertical`). We scale by ``zoom`` and crop the
    center back to the exact target size, so the output dimensions are
    unchanged — safe to concat with un-punched segments.

    Args:
        zoom: magnification factor (>1.0). 1.12 ≈ a 12% push-in.
        target_w, target_h: output size to crop back to.
        fps: ignored here; present so callers can swap in
            :func:`punch_in_animated` without changing the call site.

    Returns:
        A filter string: ``scale=...,crop=...,setsar=1``.

    Raises:
        ValueError: if ``zoom`` < 1.0 or dimensions are non-positive.
    """
    if zoom < 1.0:
        raise ValueError(f"zoom must be >= 1.0, got {zoom}")
    for name, v in (("target_w", target_w), ("target_h", target_h)):
        if not isinstance(v, int) or v <= 0:
            raise ValueError(f"{name} must be a positive int, got {v!r}")

    up_w = _even(target_w * zoom)
    up_h = _even(target_h * zoom)
    return (
        f"scale={up_w}:{up_h},"
        f"crop={target_w}:{target_h}:(iw-{target_w})/2:(ih-{target_h})/2,"
        f"setsar=1"
    )


def punch_in_animated(
    zoom: float = 1.12,
    duration_s: float = 3.0,
    fps: int = 30,
    target_w: int = TARGET_W,
    target_h: int = TARGET_H,
) -> str:
    """Optional animated push-in via ``zoompan`` (ramps 1.0 -> ``zoom``).

    Prefer :func:`punch_in` for reliability. ``zoompan`` is powerful but its
    integer-rounding of the crop window can cause a faint jitter on some
    builds; use this only when you specifically want the motion of the zoom to
    be *visible* over the segment rather than a fixed magnification.

    The zoom level increases linearly from 1.0 to ``zoom`` across
    ``duration_s`` seconds (``total = duration_s * fps`` frames), keeping the
    crop window centered. Output is locked to ``target_w``x``target_h``.

    Args:
        zoom: final magnification factor (>1.0).
        duration_s: length of the segment the ramp should span (seconds).
        fps: frames per second of the segment.
        target_w, target_h: output size.

    Returns:
        A ``zoompan=...,setsar=1`` filter string.

    Raises:
        ValueError: on invalid zoom / duration / fps / dimensions.
    """
    if zoom < 1.0:
        raise ValueError(f"zoom must be >= 1.0, got {zoom}")
    if duration_s <= 0:
        raise ValueError(f"duration_s must be > 0, got {duration_s}")
    if fps <= 0:
        raise ValueError(f"fps must be > 0, got {fps}")
    for name, v in (("target_w", target_w), ("target_h", target_h)):
        if not isinstance(v, int) or v <= 0:
            raise ValueError(f"{name} must be a positive int, got {v!r}")

    total = max(1, int(round(duration_s * fps)))
    # Linear ramp: z grows by (zoom-1)/total per frame, clamped at `zoom`.
    # 'on' is the output frame index inside zoompan.
    step = (zoom - 1.0) / total
    z_expr = f"min(1+{step:.8f}*on,{zoom})"
    return (
        f"zoompan=z='{z_expr}'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:fps={fps}:s={target_w}x{target_h},"
        f"setsar=1"
    )


# --------------------------------------------------------------------------- #
# demo
# --------------------------------------------------------------------------- #
def _demo() -> None:
    print("# transforms.py example filters (source = 1920x1080)\n")

    print("normalize_vertical(1920, 1080)  # wider than 9:16 -> center-crop + scale")
    print(f"  {normalize_vertical(1920, 1080)}\n")

    print("normalize_vertical(1080, 1920)  # already 9:16 -> scale (no pad/crop loss)")
    print(f"  {normalize_vertical(1080, 1920)}\n")

    print("normalize_vertical(1080, 1350)  # 4:5 portrait (wider aspect than 9:16) -> center-crop + scale")
    print(f"  {normalize_vertical(1080, 1350)}\n")

    print("normalize_vertical(720, 1280)   # smaller 9:16 -> scale up (no crop/pad loss)")
    print(f"  {normalize_vertical(720, 1280)}\n")

    print("normalize_vertical(540, 1280)   # narrower/taller than 9:16 -> fit width + pillarbox")
    print(f"  {normalize_vertical(540, 1280)}\n")

    print("punch_in()                      # default static 12% push-in")
    print(f"  {punch_in()}\n")

    print("punch_in(zoom=1.2)              # stronger static push-in")
    print(f"  {punch_in(zoom=1.2)}\n")

    print("punch_in_animated(zoom=1.12, duration_s=3, fps=30)  # optional zoompan ramp")
    print(f"  {punch_in_animated()}\n")

    print("compose([normalize_vertical(1920,1080), punch_in()])  # chained")
    print(f"  {compose([normalize_vertical(1920, 1080), punch_in()])}")


if __name__ == "__main__":
    _demo()
