"""Branded motion-graphics generators for the Counza content engine.

Two PIL-based generators for the short-form pipeline:

  * ``hook_card(text, out_path, brand, ...)`` -> an **animated full-frame mp4**
    (1080x1920, opaque, libx264 / yuv420p / 30fps, no audio). A clean editorial
    "hook" title card: a big Georgia-Bold serif headline (word-wrapped, centered,
    with an optional highlight word in Counza orange) on a bone or navy_deep
    field, under a small "Counza" wordmark. The headline reveals with an
    ease-out-cubic fade + slight rise, holds, then fades near the end. Rendered
    as a PIL PNG frame sequence and encoded with ffmpeg.

  * ``lower_third_png(name, credential, out_path, brand)`` -> a **static RGBA
    PNG** of a branded lower-third graphic only (tight bounding box, transparent
    elsewhere — NOT a full 1080 canvas), so the renderer can position / slide it.
    A navy_deep rounded bar with a left orange accent tick, the name in white
    Helvetica Bold, and the credential below in a lighter, smaller tint.

Honor the video-use Hard Rules: graphic overlays (this lower-third, and the
hook card used as a full-frame clip) composite onto the base video *before*
subtitles. Subtitles are burned last, by the renderer — never here.

``brand`` is a brand-kit dict (what ``brandkit.load_brandkit("counza")``
returns) — i.e. it has a ``["colors"]`` mapping of name -> "#rrggbb".

Fonts are NOT installed via fontconfig on this machine, so we point at concrete
files: the SERIF headline uses Georgia Bold (Newsreader stand-in); the SANS
wordmark / lower-third text uses Helvetica (Space Grotesk stand-in), falling
back to Arial. Each loader degrades gracefully to PIL's bitmap font so the
module never hard-fails on a box without those faces.

Stdlib + PIL only.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

# --- Canvas / timing defaults ----------------------------------------------
CANVAS_W = 1080
CANVAS_H = 1920
DEFAULT_FPS = 30

# External tools. Honour overrides so the same code runs on machines where
# ffmpeg is not the slim Homebrew build (mirrors build_short.py).
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

# --- Brand fallbacks (used only if a colour is missing from the kit) --------
_FALLBACK_COLORS = {
    "navy": "#003f7d",
    "navy_deep": "#13294b",
    "orange": "#e46e24",
    "bone": "#f4f8fd",
    "blue_light": "#e6f0f9",
}

# --- Font discovery ---------------------------------------------------------
# SERIF headline: Georgia Bold (Newsreader stand-in). First existing wins.
_SERIF_CANDIDATES = (
    os.path.expanduser("~/Library/Fonts/Newsreader-Bold.ttf"),
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/NewYork.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
)

# SANS wordmark / lower-third: Helvetica (Space Grotesk stand-in). Helvetica
# ships as a .ttc collection here; the faces we want live at known indices.
_HELVETICA_TTC = "/System/Library/Fonts/Helvetica.ttc"
_HELV_INDEX = {"regular": 0, "bold": 1, "light": 4}
# Standalone Arial faces as the graceful fallback for each weight.
_ARIAL_BY_WEIGHT = {
    "regular": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "light": "/System/Library/Fonts/Supplemental/Arial.ttf",
}
_SANS_LAST_RESORT = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _serif_font(size: int) -> ImageFont.FreeTypeFont:
    """Return the Georgia-Bold serif (or graceful fallback) at ``size`` px."""
    for path in _SERIF_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _sans_font(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
    """Return a Helvetica face at ``size`` px for weight regular/bold/light.

    Prefers true Helvetica faces from the system .ttc (by index), then the
    matching standalone Arial weight, then any sans we can find.
    """
    weight = weight if weight in _HELV_INDEX else "bold"
    if os.path.isfile(_HELVETICA_TTC):
        try:
            return ImageFont.truetype(_HELVETICA_TTC, size, index=_HELV_INDEX[weight])
        except OSError:
            pass
    arial = _ARIAL_BY_WEIGHT.get(weight)
    if arial and os.path.isfile(arial):
        try:
            return ImageFont.truetype(arial, size)
        except OSError:
            pass
    for path in _SANS_LAST_RESORT:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


# --- Colour helpers ---------------------------------------------------------
def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """'#rrggbb' / 'rrggbb' / '#rgb' -> (r, g, b)."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"invalid hex colour: {hex_color!r}")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _brand_rgb(brand: Dict[str, Any], key: str) -> Tuple[int, int, int]:
    """Look up a brand colour by name, with a safe built-in fallback."""
    colors = (brand or {}).get("colors", {}) if isinstance(brand, dict) else {}
    val = colors.get(key) or _FALLBACK_COLORS.get(key) or "#000000"
    return _hex_to_rgb(val)


def _blend(
    a: Tuple[int, int, int], b: Tuple[int, int, int], t: float
) -> Tuple[int, int, int]:
    """Linearly blend colour ``a`` toward ``b`` by ``t`` in [0, 1]."""
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


# --- Easing -----------------------------------------------------------------
def ease_out_cubic(t: float) -> float:
    """Ease-out cubic: fast start, gentle settle. ``1 - (1 - t) ** 3``."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 1.0 - (1.0 - t) ** 3


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


# --- Errors -----------------------------------------------------------------
class MotionGfxError(RuntimeError):
    """Raised with a human-readable message for any unrecoverable failure."""


# --- Subprocess helper (self-contained; mirrors build_short.py style) -------
def _run(cmd: Sequence[str], *, what: str) -> subprocess.CompletedProcess:
    """Run a command, raising MotionGfxError with cmd + stderr tail on failure."""
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MotionGfxError(
            f"{what}: executable not found ({cmd[0]!r}). Is it installed and on PATH?"
        ) from exc
    if proc.returncode != 0:
        tail = "\n".join(
            "    " + ln for ln in (proc.stderr or proc.stdout or "").rstrip().splitlines()[-20:]
        ) or "    (no output)"
        raise MotionGfxError(
            f"{what} failed (exit {proc.returncode}).\n"
            f"  cmd: {' '.join(shlex.quote(c) for c in cmd)}\n"
            f"  stderr (last lines):\n{tail}"
        )
    return proc


# --------------------------------------------------------------------------- #
# Text layout helpers (used by the hook card)
# --------------------------------------------------------------------------- #
_STRIP_CHARS = " \t\r\n.,!?;:\"'`’“”()[]{}-—–…"


def _norm_token(s: str) -> str:
    """Lower-case a word and strip surrounding punctuation, for highlight match."""
    return s.strip(_STRIP_CHARS).lower()


def _tokenize(text: str, highlight: Optional[str]) -> List[Tuple[str, bool]]:
    """Split ``text`` into (word, is_highlight) tuples.

    A word is flagged when its normalised form matches any normalised word in
    ``highlight`` (so a single word or a short phrase both work).
    """
    hl = {_norm_token(h) for h in str(highlight).split()} if highlight else set()
    hl.discard("")
    return [(w, (_norm_token(w) in hl) if hl else False) for w in str(text).split()]


def _line_width(
    line: Sequence[Tuple[str, bool]], font: ImageFont.FreeTypeFont, space_w: float
) -> float:
    if not line:
        return 0.0
    return sum(font.getlength(w) for w, _ in line) + space_w * (len(line) - 1)


def _wrap(
    tokens: Sequence[Tuple[str, bool]],
    font: ImageFont.FreeTypeFont,
    max_w: float,
    space_w: float,
) -> List[List[Tuple[str, bool]]]:
    """Greedy word-wrap the (word, hl) tokens into lines that fit ``max_w``."""
    lines: List[List[Tuple[str, bool]]] = []
    cur: List[Tuple[str, bool]] = []
    cur_w = 0.0
    for word, hl in tokens:
        ww = font.getlength(word)
        add = ww if not cur else space_w + ww
        if cur and cur_w + add > max_w:
            lines.append(cur)
            cur, cur_w = [(word, hl)], ww
        else:
            cur.append((word, hl))
            cur_w += add
    if cur:
        lines.append(cur)
    return lines


def _fit_headline(
    tokens: Sequence[Tuple[str, bool]], max_w: float, max_h: float
) -> Tuple[ImageFont.FreeTypeFont, List[List[Tuple[str, bool]]], int, float]:
    """Pick the largest serif size whose wrapped block fits (max_w, max_h).

    Returns (font, lines, line_height_px, space_width). Falls back to the
    smallest tried size if nothing fits (extreme inputs).
    """
    font = None
    lines: List[List[Tuple[str, bool]]] = []
    line_h = 0
    space_w = 0.0
    for size in range(120, 55, -8):
        font = _serif_font(size)
        space_w = font.getlength(" ")
        lines = _wrap(tokens, font, max_w, space_w)
        ascent, descent = font.getmetrics()
        line_h = int(round((ascent + descent) * 1.12))
        widest = max((_line_width(ln, font, space_w) for ln in lines), default=0.0)
        block_h = line_h * len(lines)
        if widest <= max_w and block_h <= max_h:
            break
    return font, lines, line_h, space_w  # type: ignore[return-value]


def _render_headline_layer(
    lines: Sequence[Sequence[Tuple[str, bool]]],
    font: ImageFont.FreeTypeFont,
    line_h: int,
    space_w: float,
    top_y: float,
    color_main: Tuple[int, int, int],
    color_hl: Tuple[int, int, int],
) -> Image.Image:
    """Render the centered, per-word-coloured headline onto a transparent layer."""
    layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for k, line in enumerate(lines):
        x = (CANVAS_W - _line_width(line, font, space_w)) / 2.0
        cy = top_y + (k + 0.5) * line_h
        for word, hl in line:
            fill = (color_hl if hl else color_main) + (255,)
            draw.text((x, cy), word, font=font, fill=fill, anchor="lm")
            x += font.getlength(word) + space_w
    return layer


def _render_wordmark_layer(
    text: str,
    font: ImageFont.FreeTypeFont,
    center_y: int,
    color: Tuple[int, int, int],
    dot_color: Tuple[int, int, int],
) -> Image.Image:
    """Render the small centered wordmark (text + a trailing orange dot)."""
    layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    dot_r = max(5, int(round(font.size * 0.12)))
    dot_gap = int(round(font.size * 0.30))
    text_w = font.getlength(text)
    total_w = text_w + dot_gap + dot_r * 2
    x = (CANVAS_W - total_w) / 2.0
    draw.text((x, center_y), text, font=font, fill=color + (255,), anchor="lm")
    dx = x + text_w + dot_gap
    draw.ellipse(
        [dx, center_y - dot_r, dx + dot_r * 2, center_y + dot_r],
        fill=dot_color + (255,),
    )
    return layer


def _faded_shifted(
    layer: Image.Image, alpha: float, dy: float
) -> Optional[Image.Image]:
    """Return a copy of ``layer`` at global ``alpha`` (0..1), shifted down ``dy`` px."""
    if alpha <= 0.0:
        return None
    a = layer.getchannel("A")
    if alpha < 1.0:
        a = a.point(lambda v: int(v * alpha))
    out = layer.copy()
    out.putalpha(a)
    if dy:
        shifted = Image.new("RGBA", out.size, (0, 0, 0, 0))
        shifted.paste(out, (0, int(round(dy))))
        out = shifted
    return out


# --------------------------------------------------------------------------- #
# Public API: hook_card
# --------------------------------------------------------------------------- #
def hook_card(
    text: str,
    out_path: str,
    brand: Dict[str, Any],
    duration: float = 2.5,
    highlight: Optional[str] = None,
    bg: str = "bone",
    fps: int = DEFAULT_FPS,
) -> str:
    """Render an animated full-frame hook title card to an mp4.

    Args:
        text:      Headline copy (word-wrapped + centered).
        out_path:  Where to write the .mp4. Parent dirs are created.
        brand:     Brand-kit dict (from brandkit.load_brandkit).
        duration:  Total clip length in seconds.
        highlight: Optional word (or short phrase) drawn in Counza orange.
        bg:        "bone" (default) -> bone field, navy text; "navy" -> navy_deep
                   field, bone text. Highlight stays orange either way.
        fps:       Frame rate (default 30).

    Returns:
        Absolute path to the written mp4 (1080x1920, opaque, libx264/yuv420p,
        no audio).

    Raises:
        ValueError:      on empty text / non-positive duration or fps.
        MotionGfxError:  if ffmpeg is missing or the encode fails.
    """
    if not text or not str(text).strip():
        raise ValueError("hook_card: text must be a non-empty string")
    if duration <= 0:
        raise ValueError(f"hook_card: duration must be > 0, got {duration!r}")
    fps = int(fps)
    if fps <= 0:
        raise ValueError(f"hook_card: fps must be > 0, got {fps!r}")

    navy_mode = str(bg).strip().lower() in ("navy", "navy_deep", "dark")

    navy = _brand_rgb(brand, "navy")
    navy_deep = _brand_rgb(brand, "navy_deep")
    orange = _brand_rgb(brand, "orange")
    bone = _brand_rgb(brand, "bone")

    if navy_mode:
        bg_rgb = navy_deep
        text_main = bone
        mark_color = bone
    else:
        bg_rgb = bone
        text_main = navy
        mark_color = navy
    bg_rgba = bg_rgb + (255,)

    # --- Layout (computed once; only alpha + y-offset animate) --------------
    margin_x = 110
    max_w = CANVAS_W - 2 * margin_x          # ~860 usable width
    max_h = 1200                             # headline block height budget
    head_center_y = 1000                     # block centre (lots of bottom air)

    tokens = _tokenize(text, highlight)
    font, lines, line_h, space_w = _fit_headline(tokens, max_w, max_h)
    block_h = line_h * max(1, len(lines))
    top_y = head_center_y - block_h / 2.0

    head_layer = _render_headline_layer(
        lines, font, line_h, space_w, top_y, text_main, orange
    )

    mark_font = _sans_font(46, weight="bold")
    mark_layer = _render_wordmark_layer(
        "Counza", mark_font, center_y=165, color=mark_color, dot_color=orange
    )

    # --- Animation envelope -------------------------------------------------
    reveal = min(0.5, duration * 0.35)       # headline rise + fade-in
    fade_out = min(0.4, duration * 0.30)     # quick fade near the end
    mark_reveal = min(0.3, max(0.01, reveal))
    rise_px = 42.0
    n_frames = max(1, int(round(duration * fps)))

    tmp = tempfile.mkdtemp(prefix="hook_card_")
    try:
        for i in range(n_frames):
            tt = i / fps

            # Headline: ease-out rise+fade in, hold, linear fade out.
            if reveal > 0 and tt < reveal:
                e = ease_out_cubic(tt / reveal)
                a_head, dy = e, (1.0 - e) * rise_px
            elif fade_out > 0 and tt > duration - fade_out:
                a_head, dy = _clamp01((duration - tt) / fade_out), 0.0
            else:
                a_head, dy = 1.0, 0.0

            # Wordmark: faster fade in, shares the fade out.
            if mark_reveal > 0 and tt < mark_reveal:
                a_mark = ease_out_cubic(tt / mark_reveal)
            elif fade_out > 0 and tt > duration - fade_out:
                a_mark = _clamp01((duration - tt) / fade_out)
            else:
                a_mark = 1.0

            canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg_rgba)
            fm = _faded_shifted(mark_layer, a_mark, 0.0)
            if fm is not None:
                canvas.alpha_composite(fm)
            fh = _faded_shifted(head_layer, a_head, dy)
            if fh is not None:
                canvas.alpha_composite(fh)

            canvas.convert("RGB").save(os.path.join(tmp, f"frame_{i:05d}.png"))

        out_abs = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
        cmd = [
            FFMPEG, "-y",
            "-framerate", str(fps),
            "-i", os.path.join(tmp, "frame_%05d.png"),
            "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-crf", "18", "-movflags", "+faststart",
            out_abs,
        ]
        _run(cmd, what="encode hook card")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return out_abs


# --------------------------------------------------------------------------- #
# Public API: lower_third_png
# --------------------------------------------------------------------------- #
def lower_third_png(
    name: str,
    credential: str,
    out_path: str,
    brand: Dict[str, Any],
) -> str:
    """Render a branded lower-third graphic to a tight, transparent RGBA PNG.

    The output is the bar only (rounded navy_deep panel + left orange accent
    tick + name + credential), sized to its content with transparent corners —
    NOT a full 1080x1920 canvas — so the renderer can position / slide it.

    Args:
        name:       Primary line (white Helvetica Bold).
        credential: Secondary line (lighter, smaller).
        out_path:   Where to write the PNG. Parent dirs are created.
        brand:      Brand-kit dict (from brandkit.load_brandkit).

    Returns:
        Absolute path to the written RGBA PNG.
    """
    name = "" if name is None else str(name)
    credential = "" if credential is None else str(credential)

    navy_deep = _brand_rgb(brand, "navy_deep")
    orange = _brand_rgb(brand, "orange")
    white = (255, 255, 255)
    # Credential: a soft, clearly-lighter steel tint (white blended toward navy).
    cred_rgb = _blend(white, _brand_rgb(brand, "navy"), 0.30)

    name_font = _sans_font(52, weight="bold")
    cred_font = _sans_font(34, weight="regular")

    # Vertical metrics from the actual faces so lines never clip.
    n_asc, n_desc = name_font.getmetrics()
    c_asc, c_desc = cred_font.getmetrics()
    name_h = n_asc + n_desc
    cred_h = c_asc + c_desc

    pad_x = 46           # interior left/right padding (panel edge -> content)
    pad_y = 34           # interior top/bottom padding
    line_gap = 12        # space between name and credential
    accent_w = 12        # orange tick width
    accent_gap = 30      # gap between tick and text
    accent_inset = 26    # tick distance from the left panel edge
    radius = 26
    min_w = 560

    text_x = accent_inset + accent_w + accent_gap
    has_cred = bool(credential.strip())

    content_w = name_font.getlength(name)
    if has_cred:
        content_w = max(content_w, cred_font.getlength(credential))

    bar_w = int(round(max(min_w, text_x + content_w + pad_x)))
    if has_cred:
        bar_h = int(round(pad_y + name_h + line_gap + cred_h + pad_y))
    else:
        bar_h = int(round(pad_y + name_h + pad_y))

    canvas = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Rounded navy_deep panel (corners stay transparent -> tight bbox).
    draw.rounded_rectangle(
        (0, 0, bar_w - 1, bar_h - 1), radius=radius, fill=navy_deep + (255,)
    )

    # Left orange accent tick (rounded), inset vertically a touch.
    tick_top = pad_y - 4
    tick_bot = bar_h - (pad_y - 4)
    draw.rounded_rectangle(
        (accent_inset, tick_top, accent_inset + accent_w, tick_bot),
        radius=accent_w // 2,
        fill=orange + (255,),
    )

    # Text. Centre each line vertically on its own band.
    if has_cred:
        name_cy = pad_y + name_h / 2.0
        cred_cy = pad_y + name_h + line_gap + cred_h / 2.0
        draw.text((text_x, name_cy), name, font=name_font, fill=white + (255,), anchor="lm")
        draw.text(
            (text_x, cred_cy), credential, font=cred_font, fill=cred_rgb + (255,), anchor="lm"
        )
    else:
        draw.text(
            (text_x, bar_h / 2.0), name, font=name_font, fill=white + (255,), anchor="lm"
        )

    out_abs = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    canvas.save(out_abs, "PNG")
    return out_abs


# --------------------------------------------------------------------------- #
# Public API: kinetic_endcard
# --------------------------------------------------------------------------- #
def _fit_oneline(text: str, weight: str, max_w: float,
                 lo: int = 40, hi: int = 200) -> ImageFont.FreeTypeFont:
    """Largest sans face whose single line of ``text`` fits ``max_w`` px."""
    best = _sans_font(lo, weight=weight)
    for size in range(hi, lo - 1, -4):
        f = _sans_font(size, weight=weight)
        if f.getlength(text) <= max_w:
            return f
        best = f
    return best


def kinetic_endcard(
    text: str,
    out_path: str,
    brand: Dict[str, Any],
    duration: float = 2.5,
    bg: str = "black",
    solid: str = "white",
    fps: int = DEFAULT_FPS,
) -> str:
    """Render a kinetic-typography CTA end-card to a full-frame mp4.

    The card stacks the SAME line of text (e.g. ``app.counza.com``) to fill the
    frame: every row is rendered hollow (outline only) except one "solid" row
    that is fully filled. The solid row sweeps down the stack as it animates in,
    the classic repeated-URL outro look. Output matches ``hook_card``: 1080x1920,
    opaque, libx264 / yuv420p / no audio.

    Args:
        text:     The line to repeat (a URL / handle / CTA). Rendered verbatim.
        out_path: Where to write the .mp4. Parent dirs are created.
        brand:    Brand-kit dict (for colours).
        duration: Clip length in seconds.
        bg:       "black" (default) | "navy" (navy_deep field).
        solid:    Fill colour of the highlighted row: "white" (default),
                  "orange"/"accent" for the brand accent, or any brand colour name.
        fps:      Frame rate (default 30).

    Returns:
        Absolute path to the written mp4.

    Raises:
        ValueError:     on empty text / non-positive duration or fps.
        MotionGfxError: if ffmpeg is missing or the encode fails.
    """
    if not text or not str(text).strip():
        raise ValueError("kinetic_endcard: text must be a non-empty string")
    if duration <= 0:
        raise ValueError(f"kinetic_endcard: duration must be > 0, got {duration!r}")
    fps = int(fps)
    if fps <= 0:
        raise ValueError(f"kinetic_endcard: fps must be > 0, got {fps!r}")

    text = str(text).strip()
    navy_mode = str(bg).strip().lower() in ("navy", "navy_deep", "dark")
    bg_rgb = _brand_rgb(brand, "navy_deep") if navy_mode else (0, 0, 0)
    bg_rgba = bg_rgb + (255,)

    line_rgb = _brand_rgb(brand, "bone") if navy_mode else (255, 255, 255)
    solid_key = str(solid).strip().lower()
    if solid_key in ("orange", "accent", "cta", "highlight"):
        solid_rgb = _brand_rgb(brand, "orange")
    elif solid_key in ("white", "", "bone"):
        solid_rgb = line_rgb
    else:
        solid_rgb = _brand_rgb(brand, solid_key)

    # --- Layout: size the line to ~90% width, then stack to fill height -------
    margin_x = 40
    max_w = CANVAS_W - 2 * margin_x
    font = _fit_oneline(text, "bold", max_w)
    ascent, descent = font.getmetrics()
    line_h = int(round((ascent + descent) * 1.06))
    text_w = font.getlength(text)
    x = (CANVAS_W - text_w) / 2.0
    stroke_w = max(2, int(round(font.size * 0.045)))

    n_rows = CANVAS_H // line_h + 2
    block_h = n_rows * line_h
    top_y = (CANVAS_H - block_h) / 2.0 + (line_h - (ascent + descent)) / 2.0

    # Outline-only rows fade with distance from the solid row for depth.
    base_outline_a = 200 if not navy_mode else 230

    reveal = min(0.45, duration * 0.35)
    n_frames = max(1, int(round(duration * fps)))
    # Solid row sweeps from the top into a settle position just above centre.
    settle_row = max(0, n_rows // 2 - 1)

    tmp = tempfile.mkdtemp(prefix="kinetic_end_")
    try:
        for i in range(n_frames):
            tt = i / fps
            if reveal > 0 and tt < reveal:
                e = ease_out_cubic(tt / reveal)
            else:
                e = 1.0
            global_a = e
            active = int(round(e * settle_row))  # sweeps 0 -> settle_row

            canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg_rgba)
            draw = ImageDraw.Draw(canvas)
            for r in range(n_rows):
                y = top_y + r * line_h
                if r == active:
                    a = int(round(255 * global_a))
                    draw.text((x, y), text, font=font, fill=solid_rgb + (a,))
                else:
                    dist = abs(r - active)
                    falloff = max(0.45, 1.0 - dist * 0.12)
                    a = int(round(base_outline_a * falloff * global_a))
                    draw.text(
                        (x, y), text, font=font,
                        fill=(0, 0, 0, 0),
                        stroke_width=stroke_w, stroke_fill=line_rgb + (a,),
                    )
            canvas.convert("RGB").save(os.path.join(tmp, f"frame_{i:05d}.png"))

        out_abs = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
        cmd = [
            FFMPEG, "-y",
            "-framerate", str(fps),
            "-i", os.path.join(tmp, "frame_%05d.png"),
            "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-crf", "18", "-movflags", "+faststart",
            out_abs,
        ]
        _run(cmd, what="encode kinetic endcard")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return out_abs


# --------------------------------------------------------------------------- #
# Self-test / CLI
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    """Build one hook card + one lower-third and verify the spec invariants."""
    # brandkit lives next to this file; import it the same way other engine
    # modules do (engine dir is on sys.path when run as a script).
    try:
        import brandkit  # type: ignore

        kit = brandkit.load_brandkit("counza")
    except Exception:
        kit = {"colors": dict(_FALLBACK_COLORS)}

    out_dir = "/tmp/mg_test"
    os.makedirs(out_dir, exist_ok=True)
    hook_path = os.path.join(out_dir, "hook.mp4")
    lt_path = os.path.join(out_dir, "lt.png")

    ok = True

    # 1) hook card -> ffprobe must show 1080x1920, ~2.5s mp4.
    hook_card(
        "Most students think Ivy League is about perfect GRADES",
        hook_path,
        kit,
        highlight="GRADES",
    )
    proc = _run(
        [
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name:format=duration",
            "-of", "default=noprint_wrappers=1", hook_path,
        ],
        what="ffprobe hook card",
    )
    info = dict(
        line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line
    )
    w, h = int(info.get("width", 0)), int(info.get("height", 0))
    dur = float(info.get("duration", 0.0))
    codec = info.get("codec_name", "?")
    hook_ok = (w, h) == (CANVAS_W, CANVAS_H) and abs(dur - 2.5) <= 0.2 and codec == "h264"
    ok = ok and hook_ok
    print("hook_card:")
    print(f"  {hook_path}")
    print(f"  {w}x{h}  {dur:.3f}s  codec={codec}  [{'PASS' if hook_ok else 'FAIL'}]")

    # 2) lower-third -> RGBA PNG with real transparency, tight (not full canvas).
    lower_third_png("Vansh Gupta", "Founder · Counza", lt_path, kit)
    with Image.open(lt_path) as im:
        mode = im.mode
        size = im.size
        alpha_min = im.getchannel("A").getextrema()[0] if mode == "RGBA" else 255
        corner_a = im.convert("RGBA").getpixel((0, 0))[3]
    lt_ok = (
        mode == "RGBA"
        and alpha_min == 0          # contains transparency
        and corner_a == 0           # rounded corner is transparent
        and size[0] < CANVAS_W      # tight bbox, not a full 1080 canvas
        and size[1] < CANVAS_H
    )
    ok = ok and lt_ok
    print("lower_third_png:")
    print(f"  {lt_path}")
    print(
        f"  mode={mode} size={size[0]}x{size[1]} alpha_min={alpha_min} "
        f"corner_alpha={corner_a}  [{'PASS' if lt_ok else 'FAIL'}]"
    )

    print(f"\nOVERALL: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
