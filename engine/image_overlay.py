"""Branded image-overlay builder for the Counza content engine.

Turns a fetched logo / real image / generated scene image into a single
**1080x1920 RGBA overlay PNG** — transparent everywhere except the placed
element — ready to composite onto a vertical talking-head video with a plain
ffmpeg ``overlay`` filter (one overlay covers the whole frame at 0,0).

Honor the video-use Hard Rules: this overlay composites onto the base video
*before* subtitles. Subtitles are burned last, by the renderer — never here.

Design language (Counza brand kit, loaded via engine/brandkit.py):
  * Logos / real images sit on a **bone rounded chip** with padding and a soft
    navy drop shadow so they read on any background (e.g. a busy talking head).
  * Generated scene images ("full") get **rounded corners + a subtle navy
    border**, no chip — they are meant to fill the frame area.

Placements (the element is positioned on a transparent 1080x1920 canvas):
  - "under_caption" : centered around y=1380 (just ABOVE the caption band near
                      y=1600). Element max height ~180px, on a bone chip with
                      soft navy shadow. Optional small navy label (Arial) under
                      it. Use for a logo called out while the speaker talks.
  - "top"           : centered near y=300, height ~220 (chip + optional label).
  - "corner_tr"     : top-right card, ~320px wide, rounded corners + shadow.
  - "full"          : contain within ~960x1400 centered, rounded corners +
                      subtle navy border. For generated scene images.

Public API:
    make_overlay(image_path, placement, out_path, brand, label=None) -> str
    make_logo_row(image_paths, out_path, brand, placement="under_caption",
                  labels=None, gap=40) -> str

``make_logo_row`` arranges 1-4 logos in a centered horizontal row of bone
chips (each with its own soft navy shadow) — use it for "trusted by" /
"got into" multi-logo callouts.

``brand`` is a brand-kit dict (the thing ``brandkit.load_brandkit("counza")``
returns) — i.e. it has a ``["colors"]`` mapping of name -> "#rrggbb".

Stdlib + PIL only (no numpy needed).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --- Canvas -----------------------------------------------------------------
CANVAS_W = 1080
CANVAS_H = 1920

# --- Brand fallbacks (used only if a colour is missing from the kit) ---------
_FALLBACK_COLORS = {
    "navy": "#003f7d",
    "navy_deep": "#13294b",
    "orange": "#e46e24",
    "bone": "#f4f8fd",
}

# --- Font discovery ---------------------------------------------------------
# macOS Arial locations (this machine ships Arial under Supplemental). We try a
# few common paths and degrade gracefully to PIL's default bitmap font so the
# module never hard-fails on a box without Arial.
_ARIAL_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_ARIAL_BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Return an Arial (or graceful fallback) TrueType font at ``size`` px."""
    candidates = _ARIAL_BOLD_CANDIDATES if bold else _ARIAL_CANDIDATES
    # If bold requested but unavailable, fall through to the regular list too.
    for path in candidates + _ARIAL_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    # Last resort: PIL's built-in bitmap font (not scalable, but never crashes).
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


# --- Geometry helpers -------------------------------------------------------
def _open_rgba(image_path: str) -> Image.Image:
    """Open an image and return it as RGBA, flattening palette/transparency."""
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"overlay source image not found: {image_path}")
    img = Image.open(image_path)
    # Respect EXIF orientation if present (photos), then go RGBA.
    try:
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img.convert("RGBA")


def _contain(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """Resize ``img`` to fit within (max_w, max_h), preserving aspect ratio."""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    scale = min(max_w / w, max_h / h)
    # Don't upscale tiny logos past 3x — keeps raster logos from going mushy,
    # but still allows reasonable enlargement of small source art.
    scale = min(scale, 3.0)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    if (new_w, new_h) == (w, h):
        return img
    return img.resize((new_w, new_h), Image.LANCZOS)


def _rounded_mask(size: Tuple[int, int], radius: int) -> Image.Image:
    """An 'L' mode rounded-rectangle mask (255 inside, 0 outside)."""
    w, h = size
    radius = max(0, min(radius, w // 2, h // 2))
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    return mask


def _soft_shadow(
    size: Tuple[int, int],
    radius: int,
    rgb: Tuple[int, int, int],
    blur: int,
    alpha: int,
) -> Image.Image:
    """Return a full-canvas RGBA layer holding a blurred rounded shadow.

    The shadow shape is drawn at the top-left of its own padded tile, blurred,
    then the caller pastes it onto the canvas at the desired offset. To keep
    this simple we return a tile sized (w+2*blur, h+2*blur) with the shape
    centered, plus the (pad) so callers can position by the shape's top-left.
    """
    w, h = size
    pad = blur * 3 + 4
    tile = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    r = max(0, min(radius, w // 2, h // 2))
    d.rounded_rectangle(
        (pad, pad, pad + w - 1, pad + h - 1),
        radius=r,
        fill=(rgb[0], rgb[1], rgb[2], alpha),
    )
    tile = tile.filter(ImageFilter.GaussianBlur(blur))
    return tile  # caller knows pad = blur*3+4


def _shadow_pad(blur: int) -> int:
    return blur * 3 + 4


def _draw_label(
    canvas: Image.Image,
    text: str,
    center_x: int,
    top_y: int,
    brand: Dict[str, Any],
    font_px: int = 34,
) -> None:
    """Draw a small navy, centered label onto ``canvas`` (in place)."""
    if not text:
        return
    draw = ImageDraw.Draw(canvas)
    font = _load_font(font_px, bold=True)
    navy = _brand_rgb(brand, "navy")
    # Measure.
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        off_x, off_y = bbox[0], bbox[1]
    except Exception:
        tw, th = draw.textsize(text, font=font) if hasattr(draw, "textsize") else (len(text) * font_px // 2, font_px)
        off_x = off_y = 0
    x = center_x - tw // 2 - off_x
    y = top_y - off_y
    # Subtle white halo so navy text stays legible over dark footage too.
    halo = (255, 255, 255, 200)
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        draw.text((x + dx, y + dy), text, font=font, fill=halo)
    draw.text((x, y), text, font=font, fill=(navy[0], navy[1], navy[2], 255))


# --- Chip composition -------------------------------------------------------
def _make_chip(
    element: Image.Image,
    brand: Dict[str, Any],
    pad: int = 28,
    radius: int = 28,
    min_w: Optional[int] = None,
) -> Image.Image:
    """Wrap ``element`` (RGBA) in a bone rounded chip with padding.

    Returns an RGBA image of the chip alone (no shadow, no canvas).
    """
    ew, eh = element.size
    cw = ew + pad * 2
    ch = eh + pad * 2
    if min_w is not None and cw < min_w:
        cw = min_w
    bone = _brand_rgb(brand, "bone")
    chip = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    body = Image.new("RGBA", (cw, ch), (bone[0], bone[1], bone[2], 255))
    mask = _rounded_mask((cw, ch), radius)
    chip.paste(body, (0, 0), mask)
    # Center the element on the chip.
    ex = (cw - ew) // 2
    ey = (ch - eh) // 2
    chip.paste(element, (ex, ey), element)
    return chip


def _paste_with_shadow(
    canvas: Image.Image,
    panel: Image.Image,
    top_left: Tuple[int, int],
    brand: Dict[str, Any],
    radius: int,
    blur: int = 18,
    shadow_alpha: int = 110,
    dy: int = 10,
) -> None:
    """Paste ``panel`` onto ``canvas`` at ``top_left`` with a soft navy shadow."""
    x, y = top_left
    pw, ph = panel.size
    navy_deep = _brand_rgb(brand, "navy_deep")
    pad = _shadow_pad(blur)
    shadow = _soft_shadow((pw, ph), radius, navy_deep, blur, shadow_alpha)
    # Shadow tile's shape top-left sits at (pad, pad); align it to panel pos.
    canvas.alpha_composite(shadow, (x - pad, y - pad + dy))
    canvas.alpha_composite(panel, (x, y))


# --- Placement implementations ---------------------------------------------
def _place_chip_centered(
    canvas: Image.Image,
    element: Image.Image,
    center_y: int,
    brand: Dict[str, Any],
    label: Optional[str],
    chip_pad: int = 28,
    chip_radius: int = 28,
) -> None:
    """Build a bone chip around ``element``, center it horizontally on canvas,
    vertically centered on ``center_y``, with a soft shadow and optional label.
    """
    chip = _make_chip(element, brand, pad=chip_pad, radius=chip_radius)
    cw, ch = chip.size
    x = (CANVAS_W - cw) // 2
    y = center_y - ch // 2
    _paste_with_shadow(canvas, chip, (x, y), brand, radius=chip_radius)
    if label:
        _draw_label(canvas, label, CANVAS_W // 2, y + ch + 18, brand)


def _placement_under_caption(
    canvas: Image.Image, src: Image.Image, brand: Dict[str, Any], label: Optional[str]
) -> None:
    # Element max height ~180; chip centered around y=1380 (above caption band).
    element = _contain(src, max_w=760, max_h=180)
    _place_chip_centered(canvas, element, center_y=1380, brand=brand, label=label)


def _placement_top(
    canvas: Image.Image, src: Image.Image, brand: Dict[str, Any], label: Optional[str]
) -> None:
    # Centered near y=300, element height ~220.
    element = _contain(src, max_w=860, max_h=220)
    _place_chip_centered(canvas, element, center_y=300, brand=brand, label=label)


def _placement_corner_tr(
    canvas: Image.Image, src: Image.Image, brand: Dict[str, Any], label: Optional[str]
) -> None:
    # Top-right card, ~320px wide, rounded corners + shadow.
    card_w = 320
    margin = 48
    element = _contain(src, max_w=card_w - 48, max_h=320)
    chip = _make_chip(element, brand, pad=24, radius=26, min_w=card_w)
    cw, ch = chip.size
    x = CANVAS_W - margin - cw
    y = margin + 40  # nudge below the very top edge
    _paste_with_shadow(canvas, chip, (x, y), brand, radius=26)
    if label:
        _draw_label(canvas, label, x + cw // 2, y + ch + 14, brand, font_px=28)


def _placement_full(
    canvas: Image.Image, src: Image.Image, brand: Dict[str, Any], label: Optional[str]
) -> None:
    # Contain within ~960x1400 centered; rounded corners + subtle navy border.
    element = _contain(src, max_w=960, max_h=1400)
    ew, eh = element.size
    radius = 40
    border = 6
    navy = _brand_rgb(brand, "navy")

    # Rounded-corner the scene image.
    rounded = Image.new("RGBA", (ew, eh), (0, 0, 0, 0))
    mask = _rounded_mask((ew, eh), radius)
    rounded.paste(element, (0, 0), mask)

    x = (CANVAS_W - ew) // 2
    y = (CANVAS_H - eh) // 2

    # Soft shadow behind the panel for depth.
    _paste_with_shadow(
        canvas, rounded, (x, y), brand, radius=radius, blur=26, shadow_alpha=120, dy=14
    )

    # Subtle navy border drawn just inside the rounded edge, onto the canvas.
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (x, y, x + ew - 1, y + eh - 1),
        radius=radius,
        outline=(navy[0], navy[1], navy[2], 255),
        width=border,
    )
    if label:
        _draw_label(canvas, label, CANVAS_W // 2, y + eh + 22, brand, font_px=40)


_PLACERS = {
    "under_caption": _placement_under_caption,
    "top": _placement_top,
    "corner_tr": _placement_corner_tr,
    "full": _placement_full,
}


# --- Public API -------------------------------------------------------------
def make_overlay(
    image_path: str,
    placement: str,
    out_path: str,
    brand: Dict[str, Any],
    label: Optional[str] = None,
) -> str:
    """Build a branded 1080x1920 RGBA overlay PNG from ``image_path``.

    Args:
        image_path: Path to the source logo / real image / generated scene.
        placement:  One of "under_caption", "top", "corner_tr", "full".
        out_path:   Where to write the PNG. Parent dirs are created.
        brand:      A brand-kit dict (from brandkit.load_brandkit), i.e. it has
                    ["colors"] mapping name -> "#rrggbb".
        label:      Optional small navy caption drawn under the element
                    (ignored for placements that don't show one well — all
                    current placements support it).

    Returns:
        Absolute path to the written RGBA PNG (1080x1920).

    Raises:
        FileNotFoundError: if ``image_path`` doesn't exist.
        ValueError:        if ``placement`` is unknown.
    """
    if placement not in _PLACERS:
        raise ValueError(
            f"unknown placement {placement!r}; expected one of "
            f"{', '.join(sorted(_PLACERS))}"
        )

    src = _open_rgba(image_path)
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))

    _PLACERS[placement](canvas, src, brand, label)

    out_abs = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    canvas.save(out_abs, "PNG")
    return out_abs


# --- Multi-logo row ---------------------------------------------------------
# Vertical center (y) of the chip row for each placement. "under_caption" sits
# just ABOVE the caption band (~y=1600); "top" rides high; "center" is mid-frame.
_ROW_CENTER_Y = {
    "under_caption": 1360,
    "top": 300,
    "center": 900,
}

# Row tuning constants.
_ROW_H = 150          # uniform logo box height (each logo contained to this)
_ROW_CHIP_PAD = 22    # bone padding around each logo inside its chip
_ROW_CHIP_RADIUS = 24
_ROW_MARGIN = 44      # min canvas margin on each side
_ROW_MAX_W_PER = 360  # cap very wide wordmarks so one logo can't hog the row


def _layout_logo_row(
    canvas: Image.Image,
    items,  # list of (RGBA image, label-or-None)
    brand: Dict[str, Any],
    center_y: int,
    gap: int,
) -> None:
    """Lay ``items`` out as a centered row of bone chips on ``canvas`` (in place).

    Every chip shares a uniform height (so the row reads as one group) while
    each logo keeps its own aspect ratio, vertically centered in its chip. If
    the natural row is wider than the available canvas width, the whole row is
    scaled down uniformly to fit.
    """
    row_h = _ROW_H
    pad = _ROW_CHIP_PAD
    gap = max(0, int(gap))
    n = len(items)

    # Contain each logo to the uniform row height (with a width cap).
    elements = [_contain(img, max_w=_ROW_MAX_W_PER, max_h=row_h) for img, _ in items]
    chip_ws = [el.size[0] + 2 * pad for el in elements]
    total_w = sum(chip_ws) + gap * (n - 1)
    avail = CANVAS_W - 2 * _ROW_MARGIN

    # If too wide for the frame, scale row height / padding / gap down to fit.
    if total_w > avail and total_w > 0:
        factor = avail / total_w
        row_h = max(1, int(row_h * factor))
        pad = max(6, int(pad * factor))
        gap = int(gap * factor)
        max_w = max(1, int(_ROW_MAX_W_PER * factor))
        elements = [_contain(img, max_w=max_w, max_h=row_h) for img, _ in items]
        chip_ws = [el.size[0] + 2 * pad for el in elements]
        total_w = sum(chip_ws) + gap * (n - 1)

    chip_h = row_h + 2 * pad
    x = (CANVAS_W - total_w) // 2
    y = center_y - chip_h // 2

    for el, (_, lbl) in zip(elements, items):
        ew, eh = el.size
        # Pad the (possibly short, wide) logo onto a uniform-height tile so
        # every chip ends up the same height, logo vertically centered.
        tile = Image.new("RGBA", (ew, row_h), (0, 0, 0, 0))
        tile.paste(el, (0, (row_h - eh) // 2), el)
        chip = _make_chip(tile, brand, pad=pad, radius=_ROW_CHIP_RADIUS)
        cw, ch = chip.size
        _paste_with_shadow(
            canvas, chip, (x, y), brand,
            radius=_ROW_CHIP_RADIUS, blur=16, shadow_alpha=110, dy=8,
        )
        if lbl:
            _draw_label(canvas, lbl, x + cw // 2, y + ch + 12, brand, font_px=30)
        x += cw + gap


def make_logo_row(
    image_paths,
    out_path: str,
    brand: Dict[str, Any],
    placement: str = "under_caption",
    labels=None,
    gap: int = 40,
) -> str:
    """Build a 1080x1920 RGBA overlay with 1-4 logos in a centered row.

    Each logo is contained to a uniform row height (~150px) and wrapped in a
    bone (#f4f8fd) rounded chip with a soft navy drop shadow, so the marks read
    over any background (e.g. a busy talking head). Chips are laid left-to-right
    with equal ``gap`` spacing and centered as a group. Optional small navy
    ``labels`` are drawn under each chip.

    Robustness: any image path that is missing or unreadable is silently
    skipped — this function never raises for bad image inputs. If no image is
    usable, a fully transparent canvas is written (still a valid overlay).

    Honors the same Hard Rule as ``make_overlay``: this is a graphic overlay,
    composited onto the base video BEFORE subtitles are burned.

    Args:
        image_paths: Iterable of 1-4 source logo paths (designed for 2-4).
        out_path:    Where to write the PNG. Parent dirs are created.
        brand:       Brand-kit dict (from brandkit.load_brandkit), with
                     ["colors"] mapping name -> "#rrggbb".
        placement:   Row vertical position: "under_caption" (~y=1360, above the
                     caption band), "top" (~y=300) or "center" (~y=900). An
                     unknown value falls back to "under_caption".
        labels:      Optional list of small navy labels, aligned with
                     ``image_paths`` by index. A label whose image is skipped is
                     dropped along with it (so labels stay matched to logos).
        gap:         Horizontal pixels between adjacent chips.

    Returns:
        Absolute path to the written RGBA PNG (1080x1920).
    """
    labels = list(labels) if labels else []
    paths = list(image_paths) if image_paths else []

    # Open valid logos, keeping each label paired with its (surviving) logo so
    # a skipped image also drops its label and the rest stay aligned.
    items = []  # list of (RGBA image, label-or-None)
    for i, p in enumerate(paths):
        lbl = labels[i] if i < len(labels) else None
        try:
            img = _open_rgba(p)
        except Exception:
            # Missing / unreadable / not an image — skip, never raise.
            continue
        items.append((img, lbl))

    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    if items:
        center_y = _ROW_CENTER_Y.get(placement, _ROW_CENTER_Y["under_caption"])
        _layout_logo_row(canvas, items, brand, center_y, gap)

    out_abs = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    canvas.save(out_abs, "PNG")
    return out_abs


# --- Self-test / CLI --------------------------------------------------------
def _selftest() -> int:
    """Generate quick test overlays for 'under_caption' and 'full' and verify
    each output is a 1080x1920 RGBA PNG that actually contains transparency.
    """
    import tempfile

    # brandkit lives next to this file; import it the same way other engine
    # modules do (sys.path includes the engine dir when run as a script).
    try:
        import brandkit  # type: ignore

        brand = brandkit.load_brandkit("counza")
    except Exception:
        # Fall back to an inline brand dict so the self-test is standalone.
        brand = {"colors": dict(_FALLBACK_COLORS)}

    tmp = tempfile.mkdtemp(prefix="image_overlay_selftest_")

    # 1) A quick PIL-generated solid test image (with a contrasting inner box
    #    so we can see containment/rounding in the output).
    test_src = os.path.join(tmp, "test_src.png")
    ti = Image.new("RGBA", (600, 400), (228, 110, 36, 255))  # orange block
    d = ImageDraw.Draw(ti)
    d.rectangle((150, 100, 450, 300), fill=(0, 63, 125, 255))  # navy inner
    ti.save(test_src, "PNG")

    results = []
    ok = True
    for placement in ("under_caption", "full"):
        out = os.path.join(tmp, f"overlay_{placement}.png")
        path = make_overlay(test_src, placement, out, brand, label="COUNZA")
        with Image.open(path) as im:
            size_ok = im.size == (CANVAS_W, CANVAS_H)
            mode_ok = im.mode == "RGBA"
            # Transparency check: the four corners must be fully transparent
            # (alpha == 0) since the element is centered.
            rgba = im.convert("RGBA")
            corners = [
                rgba.getpixel((0, 0)),
                rgba.getpixel((CANVAS_W - 1, 0)),
                rgba.getpixel((0, CANVAS_H - 1)),
                rgba.getpixel((CANVAS_W - 1, CANVAS_H - 1)),
            ]
            transparent_ok = all(px[3] == 0 for px in corners)
            # Also confirm SOMETHING was drawn (non-zero alpha somewhere).
            bbox = rgba.getbbox()  # bbox of non-zero (non-transparent) region
            drew_ok = bbox is not None
        this_ok = size_ok and mode_ok and transparent_ok and drew_ok
        ok = ok and this_ok
        results.append(
            f"  {placement:13} -> size={im.size} mode={im.mode} "
            f"corners_transparent={transparent_ok} drew={drew_ok} "
            f"content_bbox={bbox}  [{'PASS' if this_ok else 'FAIL'}]\n"
            f"                  {path}"
        )

    print("image_overlay self-test")
    print(f"  source test image: {test_src}  (600x400 RGBA)")
    print("\n".join(results))
    print(f"\nOVERALL: {'PASS' if ok else 'FAIL'}")
    print(f"(artifacts in {tmp})")
    return 0 if ok else 1


def _selftest_logo_row() -> int:
    """Fetch two real logos and verify make_logo_row writes a valid overlay.

    Checks the output is a 1080x1920 RGBA PNG with transparent corners and
    non-empty (opaque) pixels in the row band around the "under_caption" y.
    """
    try:
        import brandkit  # type: ignore

        brand = brandkit.load_brandkit("counza")
    except Exception:
        brand = {"colors": dict(_FALLBACK_COLORS)}

    try:
        import image_fetch  # type: ignore
    except Exception as exc:  # pragma: no cover
        print(f"make_logo_row self-test: cannot import image_fetch: {exc}")
        return 1

    out_dir = "/tmp/mg_test"
    os.makedirs(out_dir, exist_ok=True)
    h = image_fetch.fetch_image("Harvard University", out_dir, want="logo")
    y = image_fetch.fetch_image("Yale University", out_dir, want="logo")
    print("make_logo_row self-test")
    print(f"  fetched Harvard -> {h}")
    print(f"  fetched Yale    -> {y}")
    paths = [p for p in (h, y) if p]

    out = os.path.join(out_dir, "row.png")
    path = make_logo_row(
        paths, out, brand, placement="under_caption", labels=["Harvard", "Yale"]
    )

    with Image.open(path) as im:
        size_ok = im.size == (CANVAS_W, CANVAS_H)
        mode_ok = im.mode == "RGBA"
        rgba = im.convert("RGBA")
        corners = [
            rgba.getpixel((0, 0)),
            rgba.getpixel((CANVAS_W - 1, 0)),
            rgba.getpixel((0, CANVAS_H - 1)),
            rgba.getpixel((CANVAS_W - 1, CANVAS_H - 1)),
        ]
        transparent_ok = all(px[3] == 0 for px in corners)
        # The chip row sits around y=1360; sample a band for opaque pixels.
        band_top, band_bot = 1250, 1470
        band = rgba.crop((0, band_top, CANVAS_W, band_bot))
        opaque = 0
        for yy in range(0, band.size[1], 6):
            for xx in range(0, band.size[0], 6):
                if band.getpixel((xx, yy))[3] > 200:
                    opaque += 1
        band_ok = opaque > 0
        content_bbox = rgba.getbbox()

    ok = size_ok and mode_ok and transparent_ok and band_ok and bool(paths)
    print(f"  inputs: {paths}")
    print(
        f"  -> size={im.size} mode={im.mode} corners_transparent={transparent_ok} "
        f"row_band_opaque_samples={opaque} content_bbox={content_bbox}"
    )
    print(f"  path: {path}")
    print(f"OVERALL: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    # Default (no args) runs the original make_overlay self-test unchanged.
    # Pass "row" to run the multi-logo-row self-test (needs network for logos).
    if len(sys.argv) > 1 and sys.argv[1] == "row":
        raise SystemExit(_selftest_logo_row())
    raise SystemExit(_selftest())
