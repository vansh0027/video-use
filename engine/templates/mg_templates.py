#!/usr/bin/env python3
"""
engine/templates/mg_templates.py
================================

Parametrized, **brand-driven** motion-graphic cards for the content engine.

These package the recurring cards a launch / product / reel edit keeps
rebuilding by hand. Every card:

  * is driven by a *brand-kit dict* (what ``brandkit.load_brandkit("counza")``
    returns — i.e. it has a ``["colors"]`` mapping of name -> "#rrggbb");
  * eases **in -> holds -> out** exactly like ``motiongfx.hook_card`` (ease-out
    cubic rise + fade in, hold, quick fade out);
  * is rendered as a PIL PNG frame sequence and encoded with ffmpeg, reusing
    ``motiongfx``'s font discovery, colour helpers, easing, and ``_run``;
  * defaults to a 1080x1920 opaque libx264 / yuv420p / 30fps mp4 (no audio),
    the same contract as ``hook_card`` / ``kinetic_endcard`` — so the output
    drops straight into ``build_short.final_pass`` as an opaque full-frame
    overlay or into ``product_video`` as a beat.

Optionally a card can be rendered for **overlay** use instead of full-frame:
pass ``overlay=True`` to get a transparent render — an RGBA ``.webm``
(VP9 + yuva420p alpha) when the path ends in ``.webm``, else an animated card
on the brand background. (Full alpha needs the ``.webm`` extension; ffmpeg's
mp4/h264 path here is opaque, matching the rest of the engine.)

Public API
----------
    stat_card(number, label, out_path, brand, *, duration=2.6, sublabel=None,
              bg="bone", accent_number=True, fps=30, overlay=False) -> str
    quote_card(text, out_path, brand, *, duration=3.0, attribution=None,
               bg="bone", highlight=None, fps=30, overlay=False) -> str
    feature_bullets(bullets, out_path, brand, *, duration=None, title=None,
                    bg="navy", per_bullet=0.9, fps=30, overlay=False) -> str
    cta_endcard(text, out_path, brand, *, keyword=None, duration=2.8,
                bg="navy", fps=30, overlay=False) -> str
    logo_reveal(wordmark, out_path, brand, *, tagline=None, duration=2.4,
                bg="bone", fps=30, overlay=False) -> str

Each returns the absolute path written.

Hard-rule alignment
-------------------
These render *graphics only*; they never burn subtitles (subtitles are LAST and
the renderer's job — Hard Rule 1). A full-frame card used as a clip is opaque
and covers its own window; an ``overlay=True`` webm carries alpha for
compositing. Timing is internal to the card, so when used as an *overlay* the
caller still applies ``setpts`` (Hard Rule 4) at the splice — these cards always
start their animation at frame 0, which is exactly what ``setpts`` resets to.

Stdlib + PIL only (plus the project's ffmpeg). Imports — never modifies —
``engine.motiongfx``.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# Import the sibling engine modules. This file lives in engine/templates/, so
# the engine/ dir (its parent) is the package-less import root for motiongfx /
# brandkit, mirroring how build_short.py puts engine/ on sys.path.
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINE_DIR = os.path.dirname(_THIS_DIR)
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

import motiongfx as mg  # noqa: E402  (font discovery, colour helpers, easing, _run)

# Re-exported so callers can build cards without a second import.
CANVAS_W = mg.CANVAS_W
CANVAS_H = mg.CANVAS_H
DEFAULT_FPS = mg.DEFAULT_FPS
FFMPEG = mg.FFMPEG
FFPROBE = mg.FFPROBE
MotionGfxError = mg.MotionGfxError


# --------------------------------------------------------------------------- #
# Background / palette resolution (shared by every card)
# --------------------------------------------------------------------------- #
def _resolve_bg(brand: Dict[str, Any], bg: str) -> Tuple[Tuple[int, int, int], bool]:
    """Map a ``bg`` keyword to (rgb, is_dark).

    "bone"/"light"  -> bone field (dark text)
    "navy"/"dark"   -> navy_deep field (light text)
    "black"         -> pure black (light text)
    any brand colour name -> that colour (luminance decides text polarity)
    """
    key = str(bg or "bone").strip().lower()
    if key in ("bone", "light", "paper", "white"):
        return mg._brand_rgb(brand, "bone"), False
    if key in ("navy", "navy_deep", "dark"):
        return mg._brand_rgb(brand, "navy_deep"), True
    if key == "black":
        return (0, 0, 0), True
    rgb = mg._brand_rgb(brand, key)
    lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    return rgb, lum < 128


def _text_colors(
    brand: Dict[str, Any], is_dark: bool
) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Return (primary_text, muted_text) appropriate for a dark/light field."""
    if is_dark:
        primary = mg._brand_rgb(brand, "bone")
        muted = mg._blend(primary, mg._brand_rgb(brand, "navy"), 0.45)
    else:
        primary = mg._brand_rgb(brand, "navy")
        muted = mg._blend(primary, mg._brand_rgb(brand, "bone"), 0.45)
    return primary, muted


def _accent(brand: Dict[str, Any]) -> Tuple[int, int, int]:
    return mg._brand_rgb(brand, "orange")


# --------------------------------------------------------------------------- #
# Layout helpers (sans-wrapping, mirrors motiongfx's serif wrapper)
# --------------------------------------------------------------------------- #
def _sans_wrap(
    text: str, font: ImageFont.FreeTypeFont, max_w: float
) -> List[str]:
    """Greedy word-wrap a plain string with ``font`` to fit ``max_w`` px."""
    words = str(text).split()
    if not words:
        return [""]
    space = font.getlength(" ")
    lines: List[str] = []
    cur: List[str] = []
    cur_w = 0.0
    for w in words:
        ww = font.getlength(w)
        add = ww if not cur else space + ww
        if cur and cur_w + add > max_w:
            lines.append(" ".join(cur))
            cur, cur_w = [w], ww
        else:
            cur.append(w)
            cur_w += add
    if cur:
        lines.append(" ".join(cur))
    return lines


def _fit_sans_block(
    text: str, weight: str, max_w: float, max_h: float, *, hi: int = 110, lo: int = 40
) -> Tuple[ImageFont.FreeTypeFont, List[str], int]:
    """Largest sans size whose wrapped block of ``text`` fits (max_w, max_h)."""
    font = mg._sans_font(lo, weight=weight)
    lines = _sans_wrap(text, font, max_w)
    line_h = 1
    for size in range(hi, lo - 1, -6):
        font = mg._sans_font(size, weight=weight)
        lines = _sans_wrap(text, font, max_w)
        ascent, descent = font.getmetrics()
        line_h = int(round((ascent + descent) * 1.14))
        widest = max((font.getlength(ln) for ln in lines), default=0.0)
        if widest <= max_w and line_h * len(lines) <= max_h:
            break
    return font, lines, line_h


def _fit_serif_block(
    text: str, max_w: float, max_h: float, *, hi: int = 116, lo: int = 48
) -> Tuple[ImageFont.FreeTypeFont, List[str], int]:
    """Largest serif size whose wrapped block of ``text`` fits (max_w, max_h)."""
    font = mg._serif_font(lo)
    lines = _sans_wrap(text, font, max_w)  # word-wrap is font-agnostic
    line_h = 1
    for size in range(hi, lo - 1, -6):
        font = mg._serif_font(size)
        lines = _sans_wrap(text, font, max_w)
        ascent, descent = font.getmetrics()
        line_h = int(round((ascent + descent) * 1.16))
        widest = max((font.getlength(ln) for ln in lines), default=0.0)
        if widest <= max_w and line_h * len(lines) <= max_h:
            break
    return font, lines, line_h


def _draw_centered_lines(
    draw: ImageDraw.ImageDraw,
    lines: Sequence[str],
    font: ImageFont.FreeTypeFont,
    line_h: int,
    top_y: float,
    color: Tuple[int, int, int],
    alpha: int = 255,
) -> None:
    """Centre each line on the canvas, stacked from ``top_y``."""
    for k, ln in enumerate(lines):
        w = font.getlength(ln)
        x = (CANVAS_W - w) / 2.0
        cy = top_y + (k + 0.5) * line_h
        draw.text((x, cy), ln, font=font, fill=color + (alpha,), anchor="lm")


# --------------------------------------------------------------------------- #
# Animation envelope (shared in/hold/out, mirrors hook_card)
# --------------------------------------------------------------------------- #
def _envelope(tt: float, duration: float, reveal: float, fade_out: float,
              rise_px: float = 40.0) -> Tuple[float, float]:
    """Return (alpha, dy) for time ``tt`` under the in/hold/out envelope."""
    if reveal > 0 and tt < reveal:
        e = mg.ease_out_cubic(tt / reveal)
        return e, (1.0 - e) * rise_px
    if fade_out > 0 and tt > duration - fade_out:
        return mg._clamp01((duration - tt) / fade_out), 0.0
    return 1.0, 0.0


# --------------------------------------------------------------------------- #
# Frame-sequence -> video encoder (mirrors motiongfx's ffmpeg invocation)
# --------------------------------------------------------------------------- #
def _encode_frames(
    render_frame,
    n_frames: int,
    out_path: str,
    fps: int,
    *,
    overlay: bool,
    bg_rgba: Tuple[int, int, int, int],
    prefix: str,
) -> str:
    """Render ``n_frames`` RGBA frames via ``render_frame(i, tt)`` and encode.

    ``render_frame(i, tt)`` returns an RGBA ``Image`` the size of the canvas.

    * Full-frame (``overlay=False``): each frame is flattened onto ``bg_rgba``
      and encoded as opaque h264 / yuv420p — identical contract to hook_card.
    * Overlay (``overlay=True``) with a ``.webm`` out path: frames keep their
      alpha and are encoded as VP9 / yuva420p (real transparency).
    * Overlay with a non-webm path: we still flatten onto the brand bg (h264
      cannot carry alpha here) — callers wanting true alpha must use ``.webm``.
    """
    out_abs = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    want_alpha = overlay and out_abs.lower().endswith(".webm")

    tmp = tempfile.mkdtemp(prefix=prefix)
    try:
        for i in range(n_frames):
            tt = i / fps
            frame = render_frame(i, tt)
            if want_alpha:
                frame.save(os.path.join(tmp, f"frame_{i:05d}.png"))
            else:
                canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg_rgba)
                canvas.alpha_composite(frame)
                canvas.convert("RGB").save(os.path.join(tmp, f"frame_{i:05d}.png"))

        if want_alpha:
            cmd = [
                FFMPEG, "-y",
                "-framerate", str(fps),
                "-i", os.path.join(tmp, "frame_%05d.png"),
                "-an",
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-r", str(fps),
                "-b:v", "0", "-crf", "30",
                out_abs,
            ]
        else:
            cmd = [
                FFMPEG, "-y",
                "-framerate", str(fps),
                "-i", os.path.join(tmp, "frame_%05d.png"),
                "-an",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
                "-crf", "18", "-movflags", "+faststart",
                out_abs,
            ]
        mg._run(cmd, what=f"encode {prefix.rstrip('_')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_abs


def _frame_count(duration: float, fps: int) -> int:
    return max(1, int(round(duration * fps)))


def _check_common(name: str, duration: float, fps: int) -> int:
    if duration <= 0:
        raise ValueError(f"{name}: duration must be > 0, got {duration!r}")
    fps = int(fps)
    if fps <= 0:
        raise ValueError(f"{name}: fps must be > 0, got {fps!r}")
    return fps


# --------------------------------------------------------------------------- #
# Card: stat_card  (big number + label reveal)
# --------------------------------------------------------------------------- #
def stat_card(
    number: str,
    label: str,
    out_path: str,
    brand: Dict[str, Any],
    *,
    duration: float = 2.6,
    sublabel: Optional[str] = None,
    bg: str = "bone",
    accent_number: bool = True,
    fps: int = DEFAULT_FPS,
    overlay: bool = False,
) -> str:
    """Big-number stat card: a huge figure reveals, then its label settles in.

    Args:
        number:        The hero figure, rendered verbatim (e.g. "92%", "$10K",
                       "3.2x"). Drawn large and centered.
        label:         Caption under the number (e.g. "of students improved").
        out_path:      Where to write the file (.mp4 default; .webm => alpha).
        brand:         Brand-kit dict.
        duration:      Total clip length (s).
        sublabel:      Optional smaller third line under the label.
        bg:            "bone" (default) | "navy" | "black" | brand colour name.
        accent_number: Draw the number in the brand accent (orange). If False it
                       uses the primary text colour.
        fps:           Frame rate.
        overlay:       True + .webm => transparent overlay render.

    Returns:
        Absolute path to the written file.
    """
    if not str(number).strip():
        raise ValueError("stat_card: number must be a non-empty string")
    fps = _check_common("stat_card", duration, fps)

    bg_rgb, is_dark = _resolve_bg(brand, bg)
    bg_rgba = bg_rgb + (255,)
    primary, muted = _text_colors(brand, is_dark)
    accent = _accent(brand)
    num_color = accent if accent_number else primary

    margin_x = 90
    max_w = CANVAS_W - 2 * margin_x

    number = str(number).strip()
    num_font = mg._fit_oneline(number, "bold", max_w, lo=120, hi=420)
    n_asc, n_desc = num_font.getmetrics()
    num_h = n_asc + n_desc

    label_font, label_lines, label_lh = _fit_sans_block(
        str(label or ""), "bold", max_w, 360, hi=84, lo=40
    )
    label_block_h = label_lh * len(label_lines) if str(label or "").strip() else 0

    sub_font = mg._sans_font(40, weight="regular")
    sub_lines = _sans_wrap(str(sublabel), sub_font, max_w) if (sublabel and str(sublabel).strip()) else []
    s_asc, s_desc = sub_font.getmetrics()
    sub_lh = int(round((s_asc + s_desc) * 1.18))
    sub_block_h = sub_lh * len(sub_lines)

    # Vertical stack centered on the frame: number, gap, label, gap, sublabel.
    gap1 = 56
    gap2 = 34
    total_h = num_h + (gap1 + label_block_h if label_block_h else 0) + \
        (gap2 + sub_block_h if sub_block_h else 0)
    top = (CANVAS_H - total_h) / 2.0
    num_cy = top + num_h / 2.0
    label_top = top + num_h + gap1
    sub_top = label_top + label_block_h + gap2

    # Accent rule under the whole stack for a finished, branded look.
    rule_y = int(top + total_h + 70)
    rule_w = 120

    reveal = min(0.55, duration * 0.40)
    fade_out = min(0.45, duration * 0.30)
    label_reveal_start = reveal * 0.55  # label trails the number slightly
    n_frames = _frame_count(duration, fps)

    num_x = (CANVAS_W - num_font.getlength(number)) / 2.0

    def render(i: int, tt: float) -> Image.Image:
        frame = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)

        a_num, dy_num = _envelope(tt, duration, reveal, fade_out, rise_px=48.0)
        if a_num > 0:
            draw.text((num_x, num_cy + dy_num), number, font=num_font,
                      fill=num_color + (int(round(255 * a_num)),), anchor="lm")

        # Label trails: shift its local clock back by label_reveal_start.
        a_lab, dy_lab = _envelope(
            max(0.0, tt - label_reveal_start), duration - label_reveal_start,
            max(0.01, reveal - label_reveal_start), fade_out, rise_px=30.0,
        )
        if label_block_h and a_lab > 0:
            _draw_centered_lines(draw, label_lines, label_font, label_lh,
                                 label_top + dy_lab, muted, int(round(255 * a_lab)))

        if sub_block_h and a_lab > 0:
            _draw_centered_lines(draw, sub_lines, sub_font, sub_lh,
                                 sub_top + dy_lab, muted, int(round(200 * a_lab)))

        # Accent rule fades in with the label.
        if a_lab > 0:
            ra = int(round(255 * a_lab))
            draw.rounded_rectangle(
                [(CANVAS_W - rule_w) // 2, rule_y, (CANVAS_W + rule_w) // 2, rule_y + 8],
                radius=4, fill=accent + (ra,),
            )
        return frame

    return _encode_frames(render, n_frames, out_path, fps,
                          overlay=overlay, bg_rgba=bg_rgba, prefix="stat_card_")


# --------------------------------------------------------------------------- #
# Card: quote_card  (centered statement, clean-premium caption look)
# --------------------------------------------------------------------------- #
def quote_card(
    text: str,
    out_path: str,
    brand: Dict[str, Any],
    *,
    duration: float = 3.0,
    attribution: Optional[str] = None,
    bg: str = "bone",
    highlight: Optional[str] = None,
    fps: int = DEFAULT_FPS,
    overlay: bool = False,
) -> str:
    """Centered statement card — the clean, premium "documentary quote" look.

    A serif statement word-wraps and reveals with an ease-out rise + fade, an
    accent quote-tick sits above it, and an optional attribution settles in
    below. A ``highlight`` word/phrase is drawn in the brand accent.

    Args:
        text:        The statement copy (word-wrapped, centered).
        out_path:    Where to write the file (.mp4 default; .webm => alpha).
        brand:       Brand-kit dict.
        duration:    Total clip length (s).
        attribution: Optional "— Name, Role" line under the statement.
        bg:          "bone" (default) | "navy" | "black" | brand colour name.
        highlight:   Optional word/short phrase drawn in the brand accent.
        fps:         Frame rate.
        overlay:     True + .webm => transparent overlay render.

    Returns:
        Absolute path to the written file.
    """
    if not str(text).strip():
        raise ValueError("quote_card: text must be a non-empty string")
    fps = _check_common("quote_card", duration, fps)

    bg_rgb, is_dark = _resolve_bg(brand, bg)
    bg_rgba = bg_rgb + (255,)
    primary, muted = _text_colors(brand, is_dark)
    accent = _accent(brand)

    margin_x = 110
    max_w = CANVAS_W - 2 * margin_x
    font, lines, line_h = _fit_serif_block(str(text), max_w, 1100, hi=112, lo=52)
    block_h = line_h * len(lines)

    # Highlight matching (per-word, like motiongfx._tokenize but for plain lines).
    hl_norm = {mg._norm_token(h) for h in str(highlight).split()} if highlight else set()
    hl_norm.discard("")

    attribution = str(attribution).strip() if attribution else ""
    attr_font = mg._sans_font(40, weight="regular")
    a_asc, a_desc = attr_font.getmetrics()
    attr_h = (a_asc + a_desc) if attribution else 0

    tick_h = 70  # accent quote tick height above the statement
    gap_tick = 50
    gap_attr = 56
    total_h = tick_h + gap_tick + block_h + (gap_attr + attr_h if attr_h else 0)
    top = (CANVAS_H - total_h) / 2.0
    tick_y = top
    text_top = top + tick_h + gap_tick
    attr_cy = text_top + block_h + gap_attr + attr_h / 2.0

    space_w = font.getlength(" ")

    reveal = min(0.6, duration * 0.40)
    fade_out = min(0.5, duration * 0.30)
    attr_start = reveal * 0.7
    n_frames = _frame_count(duration, fps)

    def _draw_hl_line(draw, line, cy, alpha):
        x = (CANVAS_W - font.getlength(line)) / 2.0
        for word in line.split():
            is_hl = mg._norm_token(word) in hl_norm if hl_norm else False
            col = (accent if is_hl else primary) + (alpha,)
            draw.text((x, cy), word, font=font, fill=col, anchor="lm")
            x += font.getlength(word) + space_w

    def render(i: int, tt: float) -> Image.Image:
        frame = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)

        a_main, dy = _envelope(tt, duration, reveal, fade_out, rise_px=42.0)
        if a_main > 0:
            ma = int(round(255 * a_main))
            # accent tick (two short bars) above the statement
            tw = 18
            tx = (CANVAS_W - (tw * 2 + 16)) // 2
            for k in range(2):
                bx = tx + k * (tw + 16)
                draw.rounded_rectangle(
                    [bx, tick_y + dy, bx + tw, tick_y + tick_h + dy],
                    radius=8, fill=accent + (ma,),
                )
            for k, ln in enumerate(lines):
                cy = text_top + dy + (k + 0.5) * line_h
                _draw_hl_line(draw, ln, cy, ma)

        if attr_h:
            a_attr, dy_a = _envelope(
                max(0.0, tt - attr_start), duration - attr_start,
                max(0.01, reveal - attr_start), fade_out, rise_px=24.0,
            )
            if a_attr > 0:
                aw = attr_font.getlength(attribution)
                draw.text(((CANVAS_W - aw) / 2.0, attr_cy + dy_a), attribution,
                          font=attr_font, fill=muted + (int(round(255 * a_attr)),),
                          anchor="lm")
        return frame

    return _encode_frames(render, n_frames, out_path, fps,
                          overlay=overlay, bg_rgba=bg_rgba, prefix="quote_card_")


# --------------------------------------------------------------------------- #
# Card: feature_bullets  (sequential bullet reveal — never parallel)
# --------------------------------------------------------------------------- #
def feature_bullets(
    bullets: Sequence[str],
    out_path: str,
    brand: Dict[str, Any],
    *,
    duration: Optional[float] = None,
    title: Optional[str] = None,
    bg: str = "navy",
    per_bullet: float = 0.9,
    fps: int = DEFAULT_FPS,
    overlay: bool = False,
) -> str:
    """Feature list whose bullets reveal **one at a time**, top to bottom.

    Independent list items are revealed sequentially (each slides up + fades in
    after the previous), never all at once — the readable, on-brand way to show
    a feature list. An accent tick marks each bullet; an optional title sits on
    top and reveals first.

    Args:
        bullets:    The list items (1+). Each wraps within the content width.
        out_path:   Where to write the file (.mp4 default; .webm => alpha).
        brand:      Brand-kit dict.
        duration:   Total clip length (s). If None, computed from the bullet
                    count: ``0.5 + per_bullet*n + 1.0`` (lead-in + reveals +
                    hold/out).
        title:      Optional heading above the list.
        bg:         "navy" (default) | "bone" | "black" | brand colour name.
        per_bullet: Seconds between consecutive bullet reveals.
        fps:        Frame rate.
        overlay:    True + .webm => transparent overlay render.

    Returns:
        Absolute path to the written file.
    """
    items = [str(b).strip() for b in (bullets or []) if str(b).strip()]
    if not items:
        raise ValueError("feature_bullets: need at least one non-empty bullet")
    if per_bullet <= 0:
        raise ValueError(f"feature_bullets: per_bullet must be > 0, got {per_bullet!r}")
    n = len(items)
    if duration is None:
        duration = 0.5 + per_bullet * n + 1.0
    fps = _check_common("feature_bullets", duration, fps)

    bg_rgb, is_dark = _resolve_bg(brand, bg)
    bg_rgba = bg_rgb + (255,)
    primary, muted = _text_colors(brand, is_dark)
    accent = _accent(brand)

    margin_x = 110
    tick_w = 14
    tick_gap = 38
    text_x = margin_x + tick_w + tick_gap
    max_text_w = CANVAS_W - text_x - margin_x

    bullet_font = mg._sans_font(60, weight="bold")
    b_asc, b_desc = bullet_font.getmetrics()
    line_h = int(round((b_asc + b_desc) * 1.12))

    # Pre-wrap each bullet; remember its line list + total height.
    wrapped: List[List[str]] = [_sans_wrap(it, bullet_font, max_text_w) for it in items]
    block_heights = [line_h * len(w) for w in wrapped]
    inter_gap = 46

    title_font = mg._sans_font(46, weight="bold")
    t_asc, t_desc = title_font.getmetrics()
    title_h = (t_asc + t_desc) if (title and str(title).strip()) else 0
    title_gap = 70 if title_h else 0

    list_h = sum(block_heights) + inter_gap * (n - 1)
    total_h = title_h + title_gap + list_h
    top = (CANVAS_H - total_h) / 2.0

    # y for each bullet's first line.
    bullet_tops: List[float] = []
    y = top + title_h + title_gap
    for bh in block_heights:
        bullet_tops.append(y)
        y += bh + inter_gap

    lead_in = 0.5
    fade_out = min(0.45, duration * 0.22)
    rise_per = 28.0
    reveal_each = min(0.45, per_bullet * 0.9)
    n_frames = _frame_count(duration, fps)

    title_text = str(title).strip() if title_h else ""
    title_x = margin_x

    def render(i: int, tt: float) -> Image.Image:
        frame = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)

        # global out-fade so the whole card eases away at the end
        g_out = 1.0
        if fade_out > 0 and tt > duration - fade_out:
            g_out = mg._clamp01((duration - tt) / fade_out)

        if title_h:
            a_t = mg.ease_out_cubic(mg._clamp01(tt / max(0.01, lead_in))) * g_out
            if a_t > 0:
                draw.text((title_x, top + title_h / 2.0), title_text,
                          font=title_font, fill=accent + (int(round(255 * a_t)),),
                          anchor="lm")

        for bi, lines in enumerate(wrapped):
            start_t = lead_in + bi * per_bullet
            local = tt - start_t
            if local <= 0:
                continue
            e = mg.ease_out_cubic(mg._clamp01(local / reveal_each))
            a = e * g_out
            dy = (1.0 - e) * rise_per
            ia = int(round(255 * a))
            bt = bullet_tops[bi]
            # accent tick aligned to the bullet block
            draw.rounded_rectangle(
                [margin_x, bt + dy + 6, margin_x + tick_w, bt + block_heights[bi] + dy - 6],
                radius=tick_w // 2, fill=accent + (ia,),
            )
            for li, ln in enumerate(lines):
                cy = bt + dy + (li + 0.5) * line_h
                draw.text((text_x, cy), ln, font=bullet_font,
                          fill=primary + (ia,), anchor="lm")
        return frame

    return _encode_frames(render, n_frames, out_path, fps,
                          overlay=overlay, bg_rgba=bg_rgba, prefix="feature_bullets_")


# --------------------------------------------------------------------------- #
# Card: cta_endcard  (comment-trigger CTA on a brand field)
# --------------------------------------------------------------------------- #
def cta_endcard(
    text: str,
    out_path: str,
    brand: Dict[str, Any],
    *,
    keyword: Optional[str] = None,
    duration: float = 2.8,
    bg: str = "navy",
    fps: int = DEFAULT_FPS,
    overlay: bool = False,
) -> str:
    """Comment-trigger CTA end-card on a brand field.

    A large accent KEYWORD reveals first (the comment trigger, e.g. "PROFILE"),
    then the CTA line settles below it, under a small wordmark. Defaults pull
    the keyword and copy from ``brand["cta"]`` when not passed.

    Args:
        text:     The CTA line (e.g. "Comment 'PROFILE' for a free review"). If
                  empty, falls back to ``brand["cta"]["text"]``.
        out_path: Where to write the file (.mp4 default; .webm => alpha).
        brand:    Brand-kit dict.
        keyword:  The comment trigger drawn large in the accent. Falls back to
                  ``brand["cta"]["keyword"]``.
        duration: Total clip length (s).
        bg:       "navy" (default) | "bone" | "black" | brand colour name.
        fps:      Frame rate.
        overlay:  True + .webm => transparent overlay render.

    Returns:
        Absolute path to the written file.
    """
    cta = (brand or {}).get("cta") or {}
    keyword = (keyword if keyword is not None else cta.get("keyword") or "").strip()
    text = (text if text else cta.get("text") or "").strip()
    if not text and not keyword:
        raise ValueError("cta_endcard: need a CTA text or keyword (none in brand['cta'] either)")
    fps = _check_common("cta_endcard", duration, fps)

    bg_rgb, is_dark = _resolve_bg(brand, bg)
    bg_rgba = bg_rgb + (255,)
    primary, muted = _text_colors(brand, is_dark)
    accent = _accent(brand)

    margin_x = 90
    max_w = CANVAS_W - 2 * margin_x

    kw_text = keyword.upper() if keyword else ""
    kw_font = mg._fit_oneline(kw_text, "bold", max_w, lo=80, hi=240) if kw_text else None
    kw_h = (sum(kw_font.getmetrics()) if kw_font else 0)

    body_font, body_lines, body_lh = _fit_sans_block(text, "bold", max_w, 480, hi=66, lo=36) \
        if text else (mg._sans_font(48, "bold"), [], 0)
    body_block_h = body_lh * len(body_lines)

    mark_font = mg._sans_font(44, weight="bold")
    mark = str((brand or {}).get("name", "")).strip().title() or "Counza"

    gap1 = 60
    total_h = kw_h + (gap1 + body_block_h if body_block_h else 0)
    top = (CANVAS_H - total_h) / 2.0
    kw_cy = top + kw_h / 2.0
    body_top = top + kw_h + gap1

    reveal = min(0.55, duration * 0.40)
    fade_out = min(0.45, duration * 0.30)
    body_start = reveal * 0.6
    n_frames = _frame_count(duration, fps)

    kw_x = (CANVAS_W - kw_font.getlength(kw_text)) / 2.0 if kw_font else 0.0

    def render(i: int, tt: float) -> Image.Image:
        frame = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)

        g_out = 1.0
        if fade_out > 0 and tt > duration - fade_out:
            g_out = mg._clamp01((duration - tt) / fade_out)

        # wordmark (small, top) with an accent dot
        a_mark = mg.ease_out_cubic(mg._clamp01(tt / max(0.01, reveal * 0.6))) * g_out
        if a_mark > 0:
            ma = int(round(255 * a_mark))
            mw = mark_font.getlength(mark)
            dot_r = 7
            x = (CANVAS_W - (mw + 18 + dot_r * 2)) / 2.0
            draw.text((x, 230), mark, font=mark_font, fill=primary + (ma,), anchor="lm")
            dx = x + mw + 18
            draw.ellipse([dx, 230 - dot_r, dx + dot_r * 2, 230 + dot_r], fill=accent + (ma,))

        if kw_font and kw_text:
            a_kw, dy = _envelope(tt, duration, reveal, fade_out, rise_px=44.0)
            if a_kw > 0:
                draw.text((kw_x, kw_cy + dy), kw_text, font=kw_font,
                          fill=accent + (int(round(255 * a_kw)),), anchor="lm")

        if body_block_h:
            a_b, dy_b = _envelope(
                max(0.0, tt - body_start), duration - body_start,
                max(0.01, reveal - body_start), fade_out, rise_px=26.0,
            )
            if a_b > 0:
                _draw_centered_lines(draw, body_lines, body_font, body_lh,
                                     body_top + dy_b, primary, int(round(255 * a_b)))
        return frame

    return _encode_frames(render, n_frames, out_path, fps,
                          overlay=overlay, bg_rgba=bg_rgba, prefix="cta_endcard_")


# --------------------------------------------------------------------------- #
# Card: logo_reveal  (wordmark + tagline reveal)
# --------------------------------------------------------------------------- #
def logo_reveal(
    wordmark: str,
    out_path: str,
    brand: Dict[str, Any],
    *,
    tagline: Optional[str] = None,
    duration: float = 2.4,
    bg: str = "bone",
    fps: int = DEFAULT_FPS,
    overlay: bool = False,
) -> str:
    """Brand logo reveal: wordmark + accent dot draw on, tagline settles below.

    The wordmark scales up slightly from 92% with an ease-out, an accent
    underline wipes in beneath it, then an optional tagline fades in. The clean
    open/close bumper for a launch or product clip.

    Args:
        wordmark: The brand wordmark text (e.g. "Counza"). Defaults to
                  ``brand["name"]`` titled if empty.
        out_path: Where to write the file (.mp4 default; .webm => alpha).
        brand:    Brand-kit dict.
        tagline:  Optional line under the wordmark.
        duration: Total clip length (s).
        bg:       "bone" (default) | "navy" | "black" | brand colour name.
        fps:      Frame rate.
        overlay:  True + .webm => transparent overlay render.

    Returns:
        Absolute path to the written file.
    """
    wordmark = (str(wordmark).strip()
                or str((brand or {}).get("name", "")).strip().title() or "Counza")
    fps = _check_common("logo_reveal", duration, fps)

    bg_rgb, is_dark = _resolve_bg(brand, bg)
    bg_rgba = bg_rgb + (255,)
    primary, muted = _text_colors(brand, is_dark)
    accent = _accent(brand)

    margin_x = 90
    max_w = CANVAS_W - 2 * margin_x
    word_font = mg._fit_oneline(wordmark, "bold", int(max_w * 0.9), lo=90, hi=200)
    w_asc, w_desc = word_font.getmetrics()
    word_h = w_asc + w_desc
    word_w = word_font.getlength(wordmark)

    tagline = str(tagline).strip() if tagline else ""
    tag_font = mg._sans_font(44, weight="regular")
    t_asc, t_desc = tag_font.getmetrics()
    tag_h = (t_asc + t_desc) if tagline else 0

    underline_h = 10
    gap_ul = 40
    gap_tag = 48
    total_h = word_h + gap_ul + underline_h + (gap_tag + tag_h if tag_h else 0)
    top = (CANVAS_H - total_h) / 2.0
    word_cy = top + word_h / 2.0
    ul_y = top + word_h + gap_ul
    tag_cy = ul_y + underline_h + gap_tag + tag_h / 2.0

    reveal = min(0.5, duration * 0.40)
    fade_out = min(0.4, duration * 0.30)
    ul_start = reveal * 0.5
    tag_start = reveal * 0.9
    n_frames = _frame_count(duration, fps)

    # scale wordmark from 0.92 -> 1.0 around its centre for a subtle "set" feel
    cx = CANVAS_W / 2.0

    def render(i: int, tt: float) -> Image.Image:
        frame = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)

        a_w, dy = _envelope(tt, duration, reveal, fade_out, rise_px=20.0)
        if a_w > 0:
            e = mg.ease_out_cubic(mg._clamp01(tt / max(0.01, reveal))) if tt < reveal else 1.0
            scale = 0.92 + 0.08 * e
            fs = max(20, int(round(word_font.size * scale)))
            sf = mg._sans_font(fs, weight="bold")
            sw = sf.getlength(wordmark)
            draw.text((cx - sw / 2.0, word_cy + dy), wordmark, font=sf,
                      fill=primary + (int(round(255 * a_w)),), anchor="lm")

        # accent underline wipes from centre outwards
        a_ul, _ = _envelope(max(0.0, tt - ul_start), duration - ul_start,
                            max(0.01, reveal - ul_start), fade_out)
        if a_ul > 0:
            half = (word_w / 2.0) * mg.ease_out_cubic(a_ul)
            draw.rounded_rectangle(
                [cx - half, ul_y, cx + half, ul_y + underline_h],
                radius=underline_h // 2, fill=accent + (int(round(255 * a_ul)),),
            )

        if tag_h:
            a_t, dy_t = _envelope(max(0.0, tt - tag_start), duration - tag_start,
                                 max(0.01, reveal - tag_start), fade_out, rise_px=18.0)
            if a_t > 0:
                tw = tag_font.getlength(tagline)
                draw.text((cx - tw / 2.0, tag_cy + dy_t), tagline, font=tag_font,
                          fill=muted + (int(round(255 * a_t)),), anchor="lm")
        return frame

    return _encode_frames(render, n_frames, out_path, fps,
                          overlay=overlay, bg_rgba=bg_rgba, prefix="logo_reveal_")


# --------------------------------------------------------------------------- #
# Registry (so an assembler / EDL can dispatch a card by name)
# --------------------------------------------------------------------------- #
CARD_BUILDERS = {
    "stat_card": stat_card,
    "quote_card": quote_card,
    "feature_bullets": feature_bullets,
    "cta_endcard": cta_endcard,
    "logo_reveal": logo_reveal,
}


# --------------------------------------------------------------------------- #
# Self-test / CLI
# --------------------------------------------------------------------------- #
def _load_kit():
    try:
        import brandkit  # type: ignore
        return brandkit.load_brandkit("counza")
    except Exception:
        return {"colors": dict(mg._FALLBACK_COLORS), "name": "counza",
                "cta": {"keyword": "PROFILE", "text": "Comment 'PROFILE' for a free review"}}


def _selftest() -> int:
    kit = _load_kit()
    out_dir = os.environ.get("MG_TEST_DIR", "/tmp/mg_templates_test")
    os.makedirs(out_dir, exist_ok=True)

    def _probe(path):
        proc = mg._run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name:format=duration",
             "-of", "default=noprint_wrappers=1", path],
            what=f"ffprobe {os.path.basename(path)}",
        )
        info = dict(l.split("=", 1) for l in proc.stdout.strip().splitlines() if "=" in l)
        return int(info.get("width", 0)), int(info.get("height", 0)), \
            float(info.get("duration", 0.0)), info.get("codec_name", "?")

    ok = True
    jobs = [
        ("stat_card", lambda p: stat_card("92%", "of our users hit their goal", p, kit, duration=2.4)),
        ("quote_card", lambda p: quote_card(
            "We replaced a $10,000 counselor with one app.", p, kit,
            attribution="— Vansh, Founder", highlight="$10,000", duration=2.8)),
        ("feature_bullets", lambda p: feature_bullets(
            ["Profile analysis in minutes", "Real essay feedback", "No $300/hr fees"],
            p, kit, title="Why Counza", per_bullet=0.7)),
        ("cta_endcard", lambda p: cta_endcard("", p, kit, duration=2.6)),
        ("logo_reveal", lambda p: logo_reveal("Counza", p, kit, tagline="college, decoded", duration=2.2)),
    ]
    for name, fn in jobs:
        path = os.path.join(out_dir, f"{name}.mp4")
        fn(path)
        w, h, dur, codec = _probe(path)
        good = (w, h) == (CANVAS_W, CANVAS_H) and codec == "h264" and dur > 0
        ok = ok and good
        print(f"{name}: {path}")
        print(f"  {w}x{h}  {dur:.3f}s  codec={codec}  [{'PASS' if good else 'FAIL'}]")

    print(f"\nOVERALL: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
