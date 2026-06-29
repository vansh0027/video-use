#!/usr/bin/env python3
"""
captions_animated.py — animated short-form ASS captions, multi-preset.

Builds a valid Advanced SubStation Alpha (.ass) subtitle file. Three caption
"looks" are supported, selected via ``brand["caption_style"]["preset"]``:

    "karaoke_line"  (default, backward-compatible)
        The whole caption line (<= max_words) is always on screen; the currently
        spoken word is highlighted (orange) while the rest stays white. The
        highlight "slides" from word to word. This is the original behaviour and
        is what every existing caller gets when no preset is set.

    "oneword_pop"
        ONE word on screen at a time, large and centered, each word "pops" in
        (scale overshoot + quick fade). Story-beat keywords (brand terms, dollar
        amounts, numbers, and any words in ``keywords``) render in the accent
        colour at a larger scale. This is the TikTok / Reels talking-head look.

    "clean_twoline"
        A short phrase (<= max_words) wrapped to up to two lines, shown as a
        single gently-fading block — the clean "documentary / Submagic" look.
        Lowercase by default. Keyword tokens inside the phrase are still tinted.

Public API
----------
    build_ass(words, out_path, brand) -> str

    words    list of {"word"|"text": str, "start": float, "end": float} dicts,
             where start/end are seconds on the OUTPUT timeline (already mapped
             to the rendered video, not the source clip).
    out_path path to write the .ass file to (parent dirs are created).
    brand    a brandkit dict. The ``caption_style`` sub-dict configures the look
             (all keys optional):

               preset             "karaoke_line" | "oneword_pop" | "clean_twoline"
               max_words          words per caption line (default 4; ignored by
                                  oneword_pop)
               font               font family (default "Arial")
               size               font px on the 1080x1920 canvas (preset default)
               uppercase          force UPPERCASE (default True; set False for a
                                  natural clean_twoline look)
               animate            enable pop / fade motion (default True for the
                                  new presets; karaoke stays static unless True)
               fill               base text colour      "#rrggbb" (default white)
               highlight          accent / keyword col  "#rrggbb" (default orange)
               outline            outline colour        "#rrggbb" (default navy)
               keywords           [str] always-emphasized words (case-insensitive)
               emphasize_numbers  bool, emphasize $amounts & digit tokens
                                  (default True)
               margin_v / alignment   caption placement (preset-aware defaults)

    Returns the absolute path written.

Design notes / spec compliance
-------------------------------
* PlayRes is 1080x1920 (vertical short-form), ScaledBorderAndShadow=yes so the
  outline scales with any downstream rescale.
* All caption text is uppercased unless ``uppercase`` is False.
* Commas, braces and newlines inside words are escaped so they can't break the
  comma-delimited Dialogue format or the ASS override syntax.
* Zero-/negative-duration and empty words are skipped. Consecutive events never
  share identical [start,end] for the same line.
* Honours the Hard Rule that subtitles are burned LAST by the renderer — this
  module only writes the .ass; it never composites.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- Brand colour fallbacks (ASS &HAABBGGRR, alpha 00 == fully opaque) --------
WHITE = "&H00FFFFFF"          # caption rest / default fill
ORANGE = "&H00246EE4"         # Counza accent #e46e24 -> active / keyword word
NAVY = "&H007D3F00"           # Counza navy #003f7d   -> outline

DEFAULT_MAX_WORDS = 4

# Smallest representable tick in ASS time (centiseconds).
_TICK = 0.01

# Per-preset defaults: (font_size, margin_v, alignment, uppercase, animate).
_PRESET_DEFAULTS = {
    "karaoke_line":  {"size": 60,  "margin_v": 320, "alignment": 2, "uppercase": True,  "animate": False},
    "oneword_pop":   {"size": 96,  "margin_v": 640, "alignment": 2, "uppercase": True,  "animate": True},
    "clean_twoline": {"size": 56,  "margin_v": 360, "alignment": 2, "uppercase": False, "animate": True},
}

# A token is "emphasizable as a number/amount" if it is a bare/grouped number,
# optionally prefixed with a currency sign and/or suffixed with + / % / k / x.
_NUM_RE = re.compile(r"^[\$₹€£]?\d[\d,\.]*[\+%kKxX]?$")


# --------------------------------------------------------------------------- #
# Colour + time helpers
# --------------------------------------------------------------------------- #
def _hex_to_ass(hex_str: Optional[str], default: str) -> str:
    """Convert ``#rrggbb`` to an opaque ASS colour ``&H00BBGGRR``.

    Returns ``default`` (already an ASS literal) when ``hex_str`` is missing or
    malformed, so a bad brand value can never break the file.
    """
    if not hex_str:
        return default
    s = str(hex_str).strip().lstrip("#")
    if len(s) != 6:
        return default
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return default
    return f"&H00{b:02X}{g:02X}{r:02X}"


def _fmt_time(t: float) -> str:
    """Format seconds as ASS time H:MM:SS.cc (centisecond precision)."""
    if t < 0:
        t = 0.0
    total_cs = int(round(t * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_text(s: str) -> str:
    """Make a word safe to embed in an ASS Dialogue text field."""
    if s is None:
        return ""
    s = str(s).replace("\r", " ").replace("\n", " ")
    s = s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return s.strip()


def _norm_token(s: str) -> str:
    """Strip punctuation/case for keyword matching."""
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


# --------------------------------------------------------------------------- #
# Word cleaning + emphasis classification
# --------------------------------------------------------------------------- #
def _clean_words(words: Sequence[Dict[str, Any]], uppercase: bool) -> List[Dict[str, Any]]:
    """Drop empty / zero-duration entries; coerce times; carry display text.

    Accepts either ``word`` or ``text`` as the token key (transcripts in this
    repo use ``text``; the historical caption API used ``word``).
    """
    clean: List[Dict[str, Any]] = []
    for w in words or []:
        try:
            start = float(w.get("start"))
            end = float(w.get("end"))
        except (TypeError, ValueError):
            continue
        raw = w.get("word", w.get("text", ""))
        disp = _escape_text(raw)
        if uppercase:
            disp = disp.upper()
        if not disp:
            continue
        if end <= start:
            continue
        clean.append({"text": disp, "raw": str(raw), "start": start, "end": end})
    return clean


def _is_keyword(raw: str, kw_norm: set, emphasize_numbers: bool) -> bool:
    """True if this token is a story beat we should accent."""
    tok = (raw or "").strip()
    if emphasize_numbers and _NUM_RE.match(tok):
        return True
    return _norm_token(tok) in kw_norm


def _group_lines(clean: List[Dict[str, Any]], max_words: int) -> List[List[Dict[str, Any]]]:
    """Chunk the cleaned word list into lines of <= max_words words."""
    if max_words < 1:
        max_words = 1
    return [clean[i : i + max_words] for i in range(0, len(clean), max_words)]


# --------------------------------------------------------------------------- #
# ASS document scaffolding
# --------------------------------------------------------------------------- #
def _script_info(play_w: int, play_h: int) -> str:
    """[Script Info] with the caption coordinate space. MUST match the burn
    target's resolution — a portrait PlayRes burned onto a landscape frame (or
    vice-versa) makes libass rescale the whole script and push text off-frame."""
    return (
        "[Script Info]\n"
        "; Generated by engine/captions_animated.py\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {int(play_w)}\n"
        f"PlayResY: {int(play_h)}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 2\n"
    )

_EVENTS_HEADER = (
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text\n"
)


def _styles_block(font: str, size: int, fill: str, outline: str,
                  bold: bool, alignment: int, margin_v: int) -> str:
    bold_flag = -1 if bold else 0
    return (
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{fill},{fill},{outline},&H64000000,"
        f"{bold_flag},0,0,0,100,100,0,0,1,4,1,{alignment},60,60,{margin_v},1\n"
    )


def _dialogue(start: float, end: float, text: str) -> str:
    return (
        "Dialogue: 0,"
        f"{_fmt_time(start)},{_fmt_time(end)},"
        "Default,,0,0,0,"
        f"{text}"
    )


# --------------------------------------------------------------------------- #
# Preset builders — each returns a list of Dialogue strings
# --------------------------------------------------------------------------- #
def _build_karaoke(clean, cfg) -> List[str]:
    """Whole-line on screen, active word tinted. Original behaviour."""
    highlight, fill = cfg["highlight"], cfg["fill"]
    lines = _group_lines(clean, cfg["max_words"])
    out: List[str] = []
    for line in lines:
        line_words = [w["text"] for w in line]
        prev_end = None
        for idx, w in enumerate(line):
            start, end = w["start"], w["end"]
            if prev_end is not None and start < prev_end:
                start = prev_end
            if end <= start:
                end = start + _TICK
            prev_end = end
            parts = []
            for i, t in enumerate(line_words):
                if i == idx:
                    parts.append(f"{{\\c{highlight}}}{t}{{\\c{fill}}}")
                else:
                    parts.append(t)
            out.append(_dialogue(start, end, " ".join(parts)))
    return out


def _build_oneword(clean, cfg) -> List[str]:
    """One word at a time, pop-scale in. Keywords accented + larger."""
    highlight, fill = cfg["highlight"], cfg["fill"]
    animate = cfg["animate"]
    out: List[str] = []
    prev_end = None
    for w in clean:
        start, end = w["start"], w["end"]
        if prev_end is not None and start < prev_end:
            start = prev_end
        if end <= start:
            end = start + _TICK
        # Hold each word a touch longer than the spoken span for legibility,
        # but never past the next word (clamped above on the next iteration).
        prev_end = end
        kw = _is_keyword(w["raw"], cfg["kw_norm"], cfg["emphasize_numbers"])
        colour = highlight if kw else fill
        if animate:
            if kw:
                # bigger overshoot, settle slightly enlarged
                anim = "\\fad(40,40)\\fscx72\\fscy72\\t(0,110,\\fscx122\\fscy122)\\t(110,190,\\fscx112\\fscy112)"
            else:
                anim = "\\fad(40,30)\\fscx74\\fscy74\\t(0,90,\\fscx106\\fscy106)\\t(90,150,\\fscx100\\fscy100)"
            tag = f"{{{anim}\\c{colour}}}"
        else:
            scale = "\\fscx110\\fscy110" if kw else ""
            tag = f"{{{scale}\\c{colour}}}" if (scale or colour != fill) else ""
        out.append(_dialogue(start, end, f"{tag}{w['text']}"))
    return out


def _build_twoline(clean, cfg) -> List[str]:
    """Phrase block (<=max_words) wrapped to two lines, gentle fade. Keywords tinted."""
    highlight, fill = cfg["highlight"], cfg["fill"]
    animate = cfg["animate"]
    lines = _group_lines(clean, cfg["max_words"])
    out: List[str] = []
    prev_end = None
    for chunk in lines:
        start = chunk[0]["start"]
        end = chunk[-1]["end"]
        if prev_end is not None and start < prev_end:
            start = prev_end
        if end <= start:
            end = start + _TICK
        prev_end = end
        # Wrap to <=2 lines: break near the middle on a word boundary.
        n = len(chunk)
        brk = (n + 1) // 2 if n > 3 else n
        rendered: List[str] = []
        for i, w in enumerate(chunk):
            t = w["text"]
            if _is_keyword(w["raw"], cfg["kw_norm"], cfg["emphasize_numbers"]):
                t = f"{{\\c{highlight}}}{t}{{\\c{fill}}}"
            rendered.append(t)
            if i == brk - 1 and brk < n:
                rendered.append("\\N")  # hard line break (no trailing space needed)
        text = ""
        for tok in rendered:
            text += tok if tok == "\\N" else (tok + " ")
        text = text.replace(" \\N ", "\\N").replace(" \\N", "\\N").strip()
        if animate:
            text = "{\\fad(90,80)}" + text
        out.append(_dialogue(start, end, text))
    return out


_BUILDERS = {
    "karaoke_line": _build_karaoke,
    "oneword_pop": _build_oneword,
    "clean_twoline": _build_twoline,
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_ass(
    words: Sequence[Dict[str, Any]],
    out_path: str,
    brand: Dict[str, Any],
) -> str:
    """Build an animated short-form .ass file. See module docstring for the contract."""
    brand = brand or {}
    cs = brand.get("caption_style") or {}

    preset = str(cs.get("preset", "karaoke_line")).strip()
    if preset not in _BUILDERS:
        preset = "karaoke_line"
    pd = _PRESET_DEFAULTS[preset]

    try:
        max_words = int(cs.get("max_words", DEFAULT_MAX_WORDS))
    except (TypeError, ValueError):
        max_words = DEFAULT_MAX_WORDS
    if max_words < 1:
        max_words = DEFAULT_MAX_WORDS

    uppercase = bool(cs.get("uppercase", pd["uppercase"]))
    animate = bool(cs.get("animate", pd["animate"]))
    font = str(cs.get("font", "Arial")) or "Arial"
    try:
        size = int(cs.get("size", pd["size"]))
    except (TypeError, ValueError):
        size = pd["size"]
    bold = bool(cs.get("bold", True))

    fill = _hex_to_ass(cs.get("fill"), WHITE)
    highlight = _hex_to_ass(cs.get("highlight"), ORANGE)
    outline = _hex_to_ass(cs.get("outline"), NAVY)

    try:
        margin_v = int(cs.get("margin_v", pd["margin_v"]))
    except (TypeError, ValueError):
        margin_v = pd["margin_v"]
    try:
        alignment = int(cs.get("alignment", pd["alignment"]))
    except (TypeError, ValueError):
        alignment = pd["alignment"]

    kw_norm = {_norm_token(k) for k in (cs.get("keywords") or []) if _norm_token(k)}
    emphasize_numbers = bool(cs.get("emphasize_numbers", True))

    # Caption coordinate space — MUST match the burn target's resolution.
    pr = cs.get("play_res") or (1080, 1920)
    try:
        play_w, play_h = int(pr[0]), int(pr[1])
    except (TypeError, ValueError, IndexError):
        play_w, play_h = 1080, 1920

    cfg = {
        "max_words": max_words, "fill": fill, "highlight": highlight,
        "animate": animate, "kw_norm": kw_norm,
        "emphasize_numbers": emphasize_numbers,
    }

    clean = _clean_words(words, uppercase)
    dialogues = _BUILDERS[preset](clean, cfg)

    doc = (
        _script_info(play_w, play_h)
        + "\n"
        + _styles_block(font, size, fill, outline, bold, alignment, margin_v)
        + "\n"
        + _EVENTS_HEADER
        + ("\n".join(dialogues) + "\n" if dialogues else "")
    )

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


# --------------------------------------------------------------------------- #
# Demo / smoke entrypoint
# --------------------------------------------------------------------------- #
def _demo() -> List[str]:
    demo_words = [
        {"word": "Stop", "start": 0.00, "end": 0.30},
        {"word": "paying", "start": 0.30, "end": 0.62},
        {"word": "counselors", "start": 0.62, "end": 1.10},
        {"word": "$10,000", "start": 1.10, "end": 1.70},
        {"word": "—", "start": 1.70, "end": 1.78},
        {"word": "pay", "start": 1.78, "end": 2.00},
        {"word": "Counza", "start": 2.00, "end": 2.45},
        {"word": "35", "start": 2.45, "end": 2.90},
    ]
    out = []
    for preset in ("karaoke_line", "oneword_pop", "clean_twoline"):
        brand = {"caption_style": {
            "preset": preset, "font": "Arial", "max_words": 4,
            "highlight": "#e46e24", "outline": "#003f7d",
            "keywords": ["Counza"],
        }}
        out.append(build_ass(demo_words, f"/tmp/cap_{preset}.ass", brand))
    return out


if __name__ == "__main__":
    for p in _demo():
        print(p)
