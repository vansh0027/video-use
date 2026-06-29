#!/usr/bin/env python3
"""
engine/build_short.py
=====================

Turn ONE source video into a branded vertical (9:16) short, end to end, using
the five content-engine modules:

  * brandkit.py          — palette / fonts / caption style / CTA copy
  * profiles/loader.py   — editing defaults (target length, punch-in, captions…)
  * transforms.py        — normalize-to-vertical + subtle punch-in (-vf strings)
  * audio_polish.py      — voice cleanup, 30 ms boundary fades, final loudnorm
  * captions_animated.py — word-by-word highlighted .ass captions

This is a *self-contained* renderer (an MVP): it does its own per-segment
extract → concat → CTA card → caption burn, rather than delegating to
helpers/render.py. It also writes the **extended EDL** it acted on as an
inspectable artifact next to the output.

Hard-rule alignment (video-use)
-------------------------------
* Per-segment extract with **identical encode params**, then concat.
* **30 ms audio fades** at every segment boundary (audio_polish.boundary_afades).
* **Image overlays composite BEFORE subtitles** — overlays are chained onto the
  base video in the final pass, then subtitles are burned on top LAST so an
  overlay can never hide a caption.
* **Subtitles burned LAST**, in the final pass.
* **loudnorm applied ONCE at the end**, not per segment.
* Cuts land on word boundaries (the caller supplies word-boundary ranges; the
  whole-source default is the trivial single range).

Web-image inserts & seamless loop (MVP additions)
-------------------------------------------------
* ``images`` (an EDL array and/or the ``--images`` JSON arg) drop real logos
  (Wikimedia), generated scenes (Pollinations) or local files onto the output
  timeline as branded overlays. Each insert is resolved to a *pre-positioned*
  1080x1920 RGBA PNG (engine/image_overlay.py) and composited in the final pass
  with ``enable='between(t,at,at+duration)'`` BEFORE subtitles. An ``images``
  entry may instead carry ``queries: [..]`` (2-4 logos) — each is fetched and
  laid out as one centered "trusted by"/"got into" row
  (engine/image_overlay.make_logo_row) with a short alpha fade-in.
* ``--loop [crossfade|freeze_match]`` (+ ``--loop-dur``) post-processes the
  finished file through engine/loop.py so it loops seamlessly. Default: off.

Branded motion graphics (MVP additions)
---------------------------------------
* ``--hook-card "TEXT"`` (+ ``--hook-card-highlight WORD`` / ``--hook-card-bg
  bone|navy``) or an EDL ``hook_card`` block renders an animated full-frame title
  card (engine/motiongfx.hook_card) that opens the short. It is composited as an
  OPAQUE video overlay covering the first ``duration`` seconds (default 2.5s),
  BEFORE subtitles.
* ``--lower-third "Name|Credential|AT|DUR"`` (repeatable) or an EDL
  ``lower_thirds`` array renders branded name bars (engine/motiongfx.lower_third_png)
  that slide in from the left over 0.4s and hold at y≈1450.
* All of the above are graphic overlays: they composite onto the base BEFORE the
  subtitles filter, which is always burned LAST (Hard Rule).

Usage
-----
    build_short.py --source IN.mp4 -o OUT.mp4
    build_short.py --source IN.mp4 --transcript words.json \\
                   --brandkit counza --profile founder_edtech \\
                   --ranges edl.json --cta "Comment FOO" --keyword FOO -o OUT.mp4
    build_short.py --source IN.mp4 --images inserts.json --loop crossfade \\
                   --loop-dur 0.5 -o OUT.mp4
    build_short.py --source IN.mp4 \\
                   --hook-card "Most students get this WRONG" \\
                   --hook-card-highlight WRONG --hook-card-bg navy \\
                   --lower-third "Vansh Gupta|Founder, Counza|3.0|4.0" -o OUT.mp4

Run ``build_short.py --help`` for the full option list. This module imports its
dependencies at import time (so ``--help`` proves the wiring is intact) but
does **not** touch ffmpeg until you actually ask it to build something.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Import the five engine modules.
#
# This file lives in engine/, so its own directory is the package-less import
# root for brandkit / audio_polish / transforms / captions_animated, and
# engine/profiles/ holds loader.py. We make both importable regardless of the
# caller's cwd by putting them on sys.path. (The modules themselves are stdlib-
# only except captions, so this is cheap and side-effect-free.)
# --------------------------------------------------------------------------- #
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROFILES_DIR = os.path.join(_ENGINE_DIR, "profiles")
_HELPERS_DIR = os.path.join(os.path.dirname(_ENGINE_DIR), "helpers")
for _p in (_ENGINE_DIR, _PROFILES_DIR, _HELPERS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import brandkit  # noqa: E402
import audio_polish  # noqa: E402
import transforms  # noqa: E402
import captions_animated  # noqa: E402
import image_fetch  # noqa: E402  (Wikimedia logo/real-image fetch)
import image_gen  # noqa: E402    (generated images, Pollinations default)
import image_overlay  # noqa: E402 (positioned 1080x1920 overlay PNG builder)
import motiongfx  # noqa: E402    (animated hook card + lower-third graphics)
import loop  # noqa: E402         (seamless end->start loop post-process)
from loader import load_profile  # noqa: E402  (engine/profiles/loader.py)

# Auto-B-roll (engine/auto_broll.py) is optional: --auto-broll is the only thing
# that needs it, so a missing module degrades to "no auto b-roll".
try:
    import auto_broll as _auto_broll  # noqa: E402
except Exception:  # pragma: no cover
    _auto_broll = None

# Style presets (engine/styles/) and the grade-preset filter strings
# (helpers/grade.py) are optional: --style is the only thing that needs them, so
# a missing module degrades to "no style" rather than breaking a plain render.
try:
    from styles.registry import load_style, available_styles  # noqa: E402
except Exception:  # pragma: no cover
    load_style = None
    def available_styles():  # type: ignore
        return []
try:
    import grade as _grade  # noqa: E402  (helpers/grade.py: PRESETS + get_preset)
except Exception:  # pragma: no cover
    _grade = None
try:
    from effects import sfx as _sfx  # noqa: E402  (engine/effects/sfx.py)
except Exception:  # pragma: no cover
    _sfx = None


# --------------------------------------------------------------------------- #
# Paths to external tools. Honour overrides so the same code runs on machines
# where ffmpeg is not the slim Homebrew build.
# --------------------------------------------------------------------------- #
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
TRANSCRIBE_PY = "/Users/vanshgupta/Developer/video-use/helpers/transcribe.py"
VENV_PY = "/Users/vanshgupta/Developer/video-use/.venv/bin/python"

# Candidate font files for the CTA card. drawtext on this machine has no
# fontconfig, so we point at concrete files. First existing file wins; if none
# exist we fall back to drawtext's compiled-in default (font="").
_SERIF_CANDIDATES = (
    os.path.expanduser("~/Library/Fonts/Newsreader-Regular.ttf"),
    os.path.expanduser("~/Library/Fonts/Newsreader-Bold.ttf"),
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/NewYork.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
)
_SANS_CANDIDATES = (
    os.path.expanduser("~/Library/Fonts/SpaceGrotesk-Medium.ttf"),
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

# Encode params shared by every produced stream so concat -c copy is legal.
TARGET_W = transforms.TARGET_W   # 1080
TARGET_H = transforms.TARGET_H   # 1920
FPS = 30
CTA_DUR = 2.5
HOOK_DUR = 2.5          # default hook-card length (s)
LOWER_THIRD_DUR = 3.0   # default lower-third hold (s)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class BuildError(RuntimeError):
    """Raised with a human-readable message for any unrecoverable failure."""


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], *, what: str) -> subprocess.CompletedProcess:
    """Run a command, raising BuildError with the cmd + stderr tail on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise BuildError(
            f"{what}: executable not found ({cmd[0]!r}). "
            f"Is it installed and on PATH?\n  cmd: {_fmt_cmd(cmd)}"
        ) from exc
    if proc.returncode != 0:
        tail = _tail(proc.stderr, 30) or _tail(proc.stdout, 30) or "(no output)"
        raise BuildError(
            f"{what} failed (exit {proc.returncode}).\n"
            f"  cmd: {_fmt_cmd(cmd)}\n"
            f"  stderr (last lines):\n{tail}"
        )
    return proc


def _fmt_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _tail(text: Optional[str], n: int) -> str:
    if not text:
        return ""
    lines = text.rstrip().splitlines()
    return "\n".join("    " + ln for ln in lines[-n:])


# --------------------------------------------------------------------------- #
# ffprobe helpers
# --------------------------------------------------------------------------- #
def probe_video(path: str) -> Tuple[int, int, float]:
    """Return (width, height, duration_seconds) for the first video stream."""
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", path,
    ]
    proc = _run(cmd, what=f"ffprobe {os.path.basename(path)}")
    try:
        data = json.loads(proc.stdout)
        stream = data["streams"][0]
        w = int(stream["width"])
        h = int(stream["height"])
        dur = float(data["format"]["duration"])
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise BuildError(
            f"could not parse ffprobe output for {path!r}: {exc}\n"
            f"  raw: {proc.stdout[:400]}"
        ) from exc
    if w <= 0 or h <= 0 or dur <= 0:
        raise BuildError(f"{path!r} reports nonsensical dims/duration {w}x{h} {dur}s")
    return w, h, dur


def probe_output(path: str) -> Dict[str, Any]:
    """Return {width,height,duration,has_audio} for a finished file."""
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "stream=index,codec_type,width,height:format=duration",
        "-of", "json", path,
    ]
    proc = _run(cmd, what=f"ffprobe (verify) {os.path.basename(path)}")
    data = json.loads(proc.stdout)
    w = h = None
    has_audio = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and w is None:
            w, h = s.get("width"), s.get("height")
        if s.get("codec_type") == "audio":
            has_audio = True
    dur = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    return {"width": w, "height": h, "duration": dur, "has_audio": has_audio}


# --------------------------------------------------------------------------- #
# Transcript handling
# --------------------------------------------------------------------------- #
def _coerce_word(entry: Any) -> Optional[Dict[str, Any]]:
    """Pull {word,start,end} out of one transcript entry, or None if unusable."""
    if not isinstance(entry, dict):
        return None
    word = entry.get("word", entry.get("text", entry.get("w")))
    start = entry.get("start", entry.get("s"))
    end = entry.get("end", entry.get("e"))
    if word is None or start is None or end is None:
        return None
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        return None
    word = str(word).strip()
    # WhisperX sometimes glues a leading punctuation mark onto a word (",actually")
    word = word.lstrip(',.;:!?"—–“”‘’ ')
    if not word:
        return None
    if not any(c.isalnum() for c in word):
        # standalone punctuation token — not a caption word
        return None
    return {"word": word, "start": start, "end": end}


def parse_transcript(data: Any) -> List[Dict[str, Any]]:
    """Flatten a WhisperX-style transcript into [{word,start,end}, ...].

    Accepts, in order of preference:
      * {"word_segments": [{word,start,end}, ...]}
      * {"segments": [{"words": [{word,start,end}, ...]}, ...]}
      * a bare list of word dicts.

    Entries missing word/start/end (e.g. unaligned words WhisperX leaves with
    null timestamps) are skipped rather than fatal. The result is sorted by
    start time so downstream timing is monotonic.
    """
    raw: List[Any] = []
    if isinstance(data, dict):
        if isinstance(data.get("word_segments"), list):
            raw = data["word_segments"]
        elif isinstance(data.get("segments"), list):
            for seg in data["segments"]:
                if isinstance(seg, dict) and isinstance(seg.get("words"), list):
                    raw.extend(seg["words"])
                elif isinstance(seg, dict) and _coerce_word(seg):
                    # Some exports put word-level entries directly in segments.
                    raw.append(seg)
        elif isinstance(data.get("words"), list):
            raw = data["words"]
    elif isinstance(data, list):
        raw = data

    words = [w for w in (_coerce_word(e) for e in raw) if w is not None]
    words.sort(key=lambda w: w["start"])
    return words


def load_transcript_for_source(source: str) -> List[Dict[str, Any]]:
    """Run helpers/transcribe.py on the source, then read the cached JSON.

    The cache convention is <source_dir>/edit/transcripts/<stem>.json (per the
    project CLAUDE.md). If the file is already present we still re-run transcribe
    — it caches per source and is a no-op for unchanged files — but tolerate the
    transcriber being unavailable by falling back to an existing cache.
    """
    src_dir = os.path.dirname(os.path.abspath(source))
    stem = os.path.splitext(os.path.basename(source))[0]
    cache = os.path.join(src_dir, "edit", "transcripts", f"{stem}.json")

    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    if os.path.exists(TRANSCRIBE_PY):
        print(f"[build_short] transcribing {os.path.basename(source)} "
              f"(local WhisperX)…", file=sys.stderr)
        try:
            _run([py, TRANSCRIBE_PY, source], what="transcribe.py")
        except BuildError as exc:
            if not os.path.exists(cache):
                raise
            print(f"[build_short] transcribe failed but cache exists, using it.\n"
                  f"  ({exc})", file=sys.stderr)
    if not os.path.exists(cache):
        raise BuildError(
            f"no transcript: expected cache at {cache!r} after transcription. "
            f"Pass --transcript explicitly if your cache lives elsewhere."
        )
    with open(cache, "r", encoding="utf-8") as fh:
        return parse_transcript(json.load(fh))


# --------------------------------------------------------------------------- #
# Range / word-timeline resolution
# --------------------------------------------------------------------------- #
def load_ranges(ranges_path: Optional[str], duration: float) -> List[Dict[str, float]]:
    """Resolve the cut list to [{start,end}, ...] in SOURCE seconds.

    With no --ranges, the whole source is one range [0, duration]. Otherwise we
    read the extended EDL's ``ranges`` array (start/end per range; ``transform``
    and other fields are read elsewhere). Ranges are clamped to [0, duration] and
    validated (end > start).
    """
    if not ranges_path:
        return [{"start": 0.0, "end": float(duration)}]

    with open(ranges_path, "r", encoding="utf-8") as fh:
        try:
            edl = json.load(fh)
        except json.JSONDecodeError as exc:
            raise BuildError(f"--ranges {ranges_path!r} is not valid JSON: {exc}") from exc

    rng_list = edl.get("ranges") if isinstance(edl, dict) else edl
    if not isinstance(rng_list, list) or not rng_list:
        raise BuildError(f"--ranges {ranges_path!r} has no 'ranges' array")

    out: List[Dict[str, float]] = []
    for i, r in enumerate(rng_list):
        if not isinstance(r, dict) or "start" not in r or "end" not in r:
            raise BuildError(f"range #{i} missing start/end: {r!r}")
        s = max(0.0, float(r["start"]))
        e = min(float(duration), float(r["end"]))
        if e <= s:
            raise BuildError(
                f"range #{i} is empty after clamping to [0,{duration:.3f}]: "
                f"start={r['start']} end={r['end']}"
            )
        item: Dict[str, float] = {"start": s, "end": e}
        # carry per-range transform.zoom if present (overrides profile punch-in)
        tf = r.get("transform") if isinstance(r, dict) else None
        if isinstance(tf, dict) and "zoom" in tf:
            try:
                item["zoom"] = float(tf["zoom"])
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


def map_words_to_output(
    words: List[Dict[str, Any]],
    ranges: List[Dict[str, float]],
    duration: float,
    seam_overlaps: Optional[List[float]] = None,
    range_out_starts: Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """Project source-time words onto the OUTPUT timeline.

    * Single full-source range -> words pass through unchanged, clamped to
      [0, duration].
    * Multi-range -> keep only words whose [start,end] overlaps a kept range,
      and offset each word so its time is measured from the start of the
      concatenated output:  out_t = w_t - range.start + range_offset, where
      range_offset is the cumulative length of all earlier ranges.

    ``seam_overlaps`` (len ``len(ranges)-1``) makes the map transition-aware:
    when seam i is an xfade of ``d`` seconds, range i+1 (and everything after)
    starts ``d`` earlier on the output timeline, because the xfade overlaps the
    outgoing tail with the incoming head. Passing it keeps captions in sync with
    a transitioned concat. ``None`` (the default) means plain hard-cut concat.

    A word straddling a range boundary is clipped to that range's bounds.
    """
    is_full_single = (
        len(ranges) == 1
        and abs(ranges[0]["start"]) < 1e-6
        and abs(ranges[0]["end"] - duration) < 1e-3
    )
    if is_full_single:
        out: List[Dict[str, Any]] = []
        for w in words:
            s = max(0.0, min(float(duration), w["start"]))
            e = max(0.0, min(float(duration), w["end"]))
            if e > s:
                out.append({"word": w["word"], "start": s, "end": e})
        return out

    mapped: List[Dict[str, Any]] = []
    offset = 0.0
    for idx, rng in enumerate(ranges):
        rs, re_ = rng["start"], rng["end"]
        # Prefer the caller's measured output start for this range (from ACTUAL
        # encoded segment durations) — ffmpeg frame-snaps segments a touch longer
        # than (end-start), so nominal offsets drift over a multi-cut reel.
        if range_out_starts is not None and idx < len(range_out_starts):
            offset = float(range_out_starts[idx])
        for w in words:
            # overlap test against [rs, re_)
            if w["end"] <= rs or w["start"] >= re_:
                continue
            ws = max(w["start"], rs)
            we = min(w["end"], re_)
            if we <= ws:
                continue
            mapped.append({
                "word": w["word"],
                "start": ws - rs + offset,
                "end": we - rs + offset,
            })
        if range_out_starts is None:
            offset += (re_ - rs)
            # An xfade at this seam pulls every later range earlier by its overlap.
            if seam_overlaps and idx < len(seam_overlaps):
                offset -= max(0.0, float(seam_overlaps[idx]))
    mapped.sort(key=lambda w: w["start"])
    return mapped


def _apply_brand_vocab(out_words: List[Dict[str, Any]],
                       kit: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fix brand mishearings in caption words from the kit's ``vocab``.

    ``vocab`` is ``"Canon=alias1|alias2,word,..."`` — WhisperX commonly hears
    "Counza" as "cancer"/"Canza", so the alias maps the misheard word back to the
    canonical spelling on screen. Whole-word and case-insensitive; preserves
    surrounding punctuation and an all-caps original. No-op when the kit defines
    no aliases.
    """
    raw = str((kit or {}).get("vocab") or "")
    if "=" not in raw:
        return out_words
    alias_map: Dict[str, str] = {}
    for term in raw.split(","):
        if "=" not in term:
            continue
        canon, _, aliases = term.partition("=")
        canon = canon.strip()
        for a in aliases.split("|"):
            a = a.strip().lower()
            if a:
                alias_map[a] = canon
    if not alias_map:
        return out_words
    import re as _re
    for w in out_words:
        txt = str(w.get("word", ""))
        m = _re.match(r"^(\W*)(.*?)(\W*)$", txt, _re.DOTALL)
        if not m:
            continue
        pre, core, post = m.group(1), m.group(2), m.group(3)
        repl = alias_map.get(core.lower())
        if repl:
            if core.isupper():
                repl = repl.upper()
            w["word"] = pre + repl + post
    return out_words


# --------------------------------------------------------------------------- #
# Font resolution for the CTA card
# --------------------------------------------------------------------------- #
def _first_existing(paths) -> Optional[str]:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def _drawtext_font_arg(fontfile: Optional[str]) -> str:
    """Return the font part of a drawtext spec.

    Prefer an explicit fontfile (no fontconfig dependency); if none was found,
    omit it so drawtext uses its compiled-in default face.
    """
    return f"fontfile={_escape_drawtext_path(fontfile)}:" if fontfile else ""


def _escape_drawtext_path(path: str) -> str:
    r"""Escape a filesystem path for use inside a drawtext option value.

    drawtext's option parser treats ':' and '\' specially, and the filter-graph
    layer treats ',' specially. Backslash-escape them.
    """
    return path.replace("\\", "\\\\").replace(":", r"\:").replace(",", r"\,")


def _escape_drawtext_text(text: str) -> str:
    r"""Escape literal text for a drawtext ``text=`` value.

    Order matters: escape backslashes first, then the characters drawtext / the
    filtergraph parser would otherwise eat: ':', single quote, '%', and ','.
    Newlines become an escaped form drawtext renders as a line break.
    """
    s = str(text)
    s = s.replace("\\", "\\\\")
    s = s.replace(":", r"\:")
    s = s.replace("'", r"\'")
    s = s.replace("%", r"\%")
    s = s.replace(",", r"\,")
    s = s.replace("\n", r"\n")
    return s


# --------------------------------------------------------------------------- #
# Encode steps
# --------------------------------------------------------------------------- #
def _common_video_out(cmd_tail: List[str]) -> List[str]:
    """Append the shared encode params used by every produced segment/card."""
    return cmd_tail + [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
    ]


def encode_segment(
    source: str,
    rng: Dict[str, float],
    src_w: int,
    src_h: int,
    out_path: str,
    *,
    punch: Optional[float],
    grade: Optional[str] = None,
) -> None:
    """Extract one range, normalize to vertical (+ optional punch-in), polish audio.

    loudnorm is intentionally NOT applied here — it runs once in the final pass.
    Boundary 30 ms fades ARE applied per segment so concatenated seams never pop.
    ``grade`` is an optional ffmpeg colour-filter string (from a style preset /
    helpers.grade); applied to the normalized frame, before the punch crop, so
    every segment carries the look and the concat stays consistent.
    """
    start, end = rng["start"], rng["end"]
    dur = end - start

    vf = transforms.compose([
        transforms.normalize_vertical(src_w, src_h),
        grade or "",
        transforms.punch_in(punch) if punch and punch > 1.0 else "",
    ])

    # 30 ms boundary fades require dur > 0.06 s; guard so we give a clear error
    # rather than letting audio_polish raise deep in the chain.
    if dur <= 2 * audio_polish.FADE_DUR:
        raise BuildError(
            f"range [{start:.3f},{end:.3f}] is only {dur:.3f}s — too short for "
            f"{audio_polish.FADE_DUR*1000:.0f}ms boundary fades "
            f"(need > {2*audio_polish.FADE_DUR:.3f}s). Widen the cut."
        )
    af = audio_polish.polish_af() + "," + audio_polish.boundary_afades(dur)

    cmd = [
        FFMPEG, "-y",
        "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-i", source,
        "-vf", vf, "-af", af,
    ]
    cmd = _common_video_out(cmd)
    cmd += [out_path]
    _run(cmd, what=f"encode segment [{start:.2f}-{end:.2f}]")


def encode_cta_card(kit: Dict[str, Any], keyword: str, text: str, out_path: str) -> None:
    """Render a ~2.5s end-card: keyword (orange, serif) + CTA line (white, sans).

    Same encode params as the segments so it concats cleanly. The card is a
    navy_deep solid with a silent AAC track of equal length.
    """
    bg_hex = kit["colors"].get("navy_deep", kit["colors"].get("navy", "#13294b"))
    bg = bg_hex.lstrip("#")
    orange = kit["colors"].get("orange", "#e46e24").lstrip("#")
    white = "FFFFFF"

    serif = _first_existing(_SERIF_CANDIDATES)
    sans = _first_existing(_SANS_CANDIDATES)

    kw = (keyword or "").strip().upper()
    body = (text or "").strip()

    draw = []
    if kw:
        draw.append(
            "drawtext="
            f"{_drawtext_font_arg(serif)}"
            f"text={_escape_drawtext_text(kw)}:"
            f"fontcolor=0x{orange}:fontsize=140:"
            "x=(w-text_w)/2:y=(h/2)-text_h-20"
        )
    if body:
        import textwrap
        body_lines = textwrap.wrap(body, width=24) or [body]
        line_h = 66
        for i, ln in enumerate(body_lines):
            draw.append(
                "drawtext="
                f"{_drawtext_font_arg(sans)}"
                f"text={_escape_drawtext_text(ln)}:"
                f"fontcolor=0x{white}:fontsize=48:"
                f"x=(w-text_w)/2:y=(h/2)+40+{i * line_h}"
            )
    vf = ",".join(draw) if draw else "null"

    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i", f"color=c=0x{bg}:s={TARGET_W}x{TARGET_H}:d={CTA_DUR}:r={FPS}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", f"{CTA_DUR}",
        "-vf", vf,
    ]
    cmd = _common_video_out(cmd)
    cmd += ["-shortest", out_path]
    _run(cmd, what="encode CTA card")


def concat_segments(seg_paths: List[str], out_path: str, tmpdir: str) -> None:
    """Concat with the demuxer (`-c copy`); fall back to the concat filter on error.

    All inputs are produced with identical params, so stream-copy concat is the
    fast path. If a build still rejects the copy (param drift), we re-encode via
    the concat filter with the same target params.
    """
    list_file = os.path.join(tmpdir, "concat.txt")
    with open(list_file, "w", encoding="utf-8") as fh:
        for p in seg_paths:
            # the concat demuxer needs single-quoted, quote-escaped paths
            safe = p.replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")

    copy_cmd = [
        FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy", out_path,
    ]
    try:
        _run(copy_cmd, what="concat (stream copy)")
        return
    except BuildError as exc:
        print(f"[build_short] copy-concat failed, re-encoding via concat filter.\n"
              f"  ({exc})", file=sys.stderr)

    # Fallback: concat filter (re-encode), same target params.
    inputs: List[str] = []
    for p in seg_paths:
        inputs += ["-i", p]
    n = len(seg_paths)
    streams = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    fc = f"{streams}concat=n={n}:v=1:a=1[v][a]"
    cmd = [FFMPEG, "-y", *inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    cmd = _common_video_out(cmd)
    cmd += [out_path]
    _run(cmd, what="concat (filter re-encode)")


def apply_style_sfx(
    in_path: str,
    seam_times: List[float],
    sfx_cfg: Optional[Dict[str, Any]],
    tmpdir: str,
) -> str:
    """Mix a style's SFX cue at each internal cut seam; return a new path.

    ``seam_times`` are the internal cut times on the OUTPUT timeline (already
    overlap-adjusted when transitions shorten the timeline, so cues land on the
    real seams). A no-op (returns ``in_path``) when SFX is off, effects.sfx is
    unavailable, or there are no internal seams. Audio-only — safe on a
    spoken_only face reel. Failures degrade to the un-sfx'd input.
    """
    if _sfx is None or not sfx_cfg:
        return in_path
    intensity = str(sfx_cfg.get("intensity") or "off").lower()
    if intensity == "off" or not seam_times:
        return in_path
    gain = _sfx.INTENSITY_GAIN_DB.get(intensity, -12)
    if gain is None:
        return in_path

    cues = list(sfx_cfg.get("cues") or ["whoosh"])
    cut_cue = "whoosh" if "whoosh" in cues else (cues[0] if cues else "whoosh")
    # seam_times are already the internal cuts (no leading 0) -> no skip_first.
    events = _sfx.events_from_cuts(list(seam_times), cue=cut_cue, skip_first=False)
    if not events:
        return in_path

    try:
        lib = _sfx.build_cue_library(os.path.join(tmpdir, "sfx_cache"), names=[cut_cue])
        mix = _sfx.build_sfx_mix(events, lib, gain_db=float(gain))
        if not mix.filtergraph or mix.n_cues == 0:
            return in_path
        out_path = os.path.join(tmpdir, "with_sfx.mp4")
        cmd = [
            FFMPEG, "-y", "-i", in_path, *mix.inputs,
            "-filter_complex", mix.filtergraph,
            "-map", "0:v:0", "-map", mix.out_label,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", out_path,
        ]
        _run(cmd, what=f"style sfx ({intensity}: {len(events)} '{cut_cue}')")
    except BuildError as exc:
        print(f"[build_short] sfx mix skipped ({exc})", file=sys.stderr)
        return in_path
    print(f"[build_short] style sfx: {len(events)} '{cut_cue}' cue(s) at cuts "
          f"(gain {gain}dB)", file=sys.stderr)
    return out_path


# Map a style transition name to an ffmpeg xfade transition.
_XFADE_MAP = {
    "zoom": "zoomin", "zoomin": "zoomin",
    "whip": "slideleft", "whip_pan": "slideleft",
    "dissolve": "fade", "fade": "fade", "crossfade": "fade",
    "fade_through_black": "fadeblack", "fadeblack": "fadeblack",
}


def plan_transitions(
    seg_durations: List[float],
    style_transition: Optional[Dict[str, Any]],
) -> Tuple[List[Optional[Dict[str, Any]]], List[float]]:
    """Decide which seams get an xfade. Returns (plan, seam_overlaps).

    ``plan[i]`` is ``{"type","dur"}`` for an xfade at seam i (between segment i
    and i+1) or ``None`` for a hard cut. ``seam_overlaps[i]`` is the matching
    overlap in seconds (0 for hard cuts) — the SAME numbers fed to
    :func:`map_words_to_output` so captions stay in sync.

    Selection is deterministic (no RNG): an even ``frequency`` fraction of seams,
    and transitioned seams are never adjacent (a transition fuses its pair, so
    the next seam is forced to a hard cut). A seam whose segments are too short
    to host the xfade is demoted to a hard cut.
    """
    seams = max(0, len(seg_durations) - 1)
    plan: List[Optional[Dict[str, Any]]] = [None] * seams
    overlaps: List[float] = [0.0] * seams
    if seams < 1 or not isinstance(style_transition, dict):
        return plan, overlaps

    raw = str(style_transition.get("default") or "hard_cut").lower()
    if raw in ("hard_cut", "hard", "none", ""):
        return plan, overlaps
    ttype = _XFADE_MAP.get(raw, "fade")
    freq = float(style_transition.get("frequency", 1.0) or 0.0)
    dur = float(style_transition.get("duration_s", 0.3) or 0.3)
    if freq <= 0.0 or dur <= 0.0:
        return plan, overlaps

    i = 0
    while i < seams:
        # Even selection: pick seam i when the freq-ramp crosses an integer.
        if int((i + 1) * freq) > int(i * freq):
            d = min(dur, 0.5 * seg_durations[i], 0.5 * seg_durations[i + 1])
            if d >= 0.12:
                plan[i] = {"type": ttype, "dur": round(d, 3)}
                overlaps[i] = round(d, 3)
                i += 2  # fused pair -> next seam is forced hard (non-adjacent)
                continue
        i += 1
    return plan, overlaps


def _render_xfade_pair(
    seg_a: str, seg_b: str, dur_a: float, spec: Dict[str, Any],
    tmpdir: str, idx: int,
) -> str:
    """Fuse two segments with an xfade (video) + acrossfade (audio).

    The xfade offset is ``dur_a - d`` so the outgoing tail overlaps the incoming
    head; output length is ``dur_a + dur_b - d``. Same encode params as the
    segments so the result concats cleanly with un-fused clips.
    """
    d = float(spec["dur"])
    ttype = str(spec["type"])
    offset = max(0.0, float(dur_a) - d)
    out_path = os.path.join(tmpdir, f"fused_{idx:03d}.mp4")
    fc = (
        f"[0:v][1:v]xfade=transition={ttype}:duration={d:.3f}:offset={offset:.3f}[v];"
        f"[0:a][1:a]acrossfade=d={d:.3f}[a]"
    )
    cmd = [
        FFMPEG, "-y", "-i", seg_a, "-i", seg_b,
        "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
    ]
    cmd = _common_video_out(cmd)
    cmd += [out_path]
    _run(cmd, what=f"xfade pair {idx} ({ttype}, {d:.2f}s)")
    return out_path


def concat_with_transitions(
    seg_paths: List[str], seg_durations: List[float],
    plan: List[Optional[Dict[str, Any]]], out_path: str, tmpdir: str,
) -> None:
    """Concat segments, fusing the planned seams with xfades first.

    Falls back to a plain hard-cut concat when ``plan`` has no transitions.
    """
    if not any(plan):
        concat_segments(seg_paths, out_path, tmpdir)
        return
    clips: List[str] = []
    j, n = 0, len(seg_paths)
    while j < n:
        if j < n - 1 and plan[j] is not None:
            clips.append(_render_xfade_pair(
                seg_paths[j], seg_paths[j + 1], seg_durations[j], plan[j], tmpdir, j))
            j += 2
        else:
            clips.append(seg_paths[j])
            j += 1
    concat_segments(clips, out_path, tmpdir)


def final_pass(
    in_path: str,
    ass_path: Optional[str],
    out_path: str,
    overlays: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Final render: composite graphic overlays, burn subtitles LAST, loudnorm at end.

    Ordering is a video-use Hard Rule: every graphic overlay (hook card, logo
    rows, lower-thirds, single images) is composited onto the base video FIRST,
    then subtitles are burned on top of the result LAST, so nothing can hide a
    caption. loudnorm runs once, here, at the very end.

    ``overlays`` is an ordered list of uniform overlay specs (bottom -> top):

        {
          "kind":       "image" | "hook_card" | "logo_row" | "lower_third",
          "asset":      <png or mp4 path>,    # one -i per asset
          "at":         <output-time start, s>,
          "duration":   <visible length, s>,
          "loop_input": bool,        # add -loop 1 -framerate FPS (PNG -> stream)
          "fade_in":    float|None,  # alpha fade-in length (needs loop_input)
        }

    Compositing per kind (every overlay is gated to its window with
    ``enable='between(t,at,at+duration)'``):
      * ``image`` / ``hook_card`` / ``logo_row`` -> ``overlay=0:0`` (the asset is
        already full-frame; the hook-card mp4 is opaque and covers its window).
      * ``lower_third`` -> slides in from off-screen left over 0.4s then holds at
        x=60, y=1450.
    A ``logo_row`` may alpha-fade in over ``fade_in`` seconds; its PNG is looped
    into a continuous stream so ``fade`` has frames to ramp. Stills that don't
    animate their alpha stay single-frame (overlay's eof_action=repeat keeps them
    available for the whole window).

    * No overlays  -> a simple ``-vf [subtitles]`` / ``-af loudnorm`` pass
      (behaviour unchanged from the original renderer).
    * With overlays -> one ``-filter_complex`` chaining base -> each overlay ->
      ``subtitles`` LAST, with ``loudnorm`` on the audio in the same graph.

    Video is re-encoded at CRF 18; +faststart for web playback.
    """
    overlays = overlays or []

    # --- Fast path: no overlays (behaviour unchanged) -----------------------
    if not overlays:
        vf_parts = []
        if ass_path:
            # subtitles filter wants the .ass path escaped inside the filter value
            vf_parts.append(f"subtitles=filename={_escape_subs_path(ass_path)}")
        vf = ",".join(vf_parts) if vf_parts else None

        cmd = [FFMPEG, "-y", "-i", in_path]
        if vf:
            cmd += ["-vf", vf]
        cmd += [
            "-af", audio_polish.loudnorm_af(),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            out_path,
        ]
        _run(cmd, what="final pass (subtitles + loudnorm)")
        return

    # --- Overlay path: overlays BEFORE subtitles (Hard Rule) ----------------
    # input 0 is the base video; inputs 1..N are the overlay assets (one -i each,
    # in list order = z-order). A PNG that animates over time (a logo-row alpha
    # fade) is looped into a continuous stream so its time-based filter has frames
    # to work with; the hook-card mp4 and the static stills are added as-is.
    #
    # A looped image MUST be bounded (-t): an unbounded `-loop 1` input never
    # signals EOF and makes ffmpeg encode forever. We cap it just past its own
    # window (it is disabled outside [at, end] anyway), which keeps the stream
    # finite while still covering the fade.
    #
    # The overlay chain runs to the LONGEST input, so a looped overlay can push
    # the output past the base length. We probe the base duration and cap the
    # output (-t) to it, so adding overlays never changes the runtime.
    base_dur = probe_video(in_path)[2]

    cmd = [FFMPEG, "-y", "-i", in_path]
    for ov in overlays:
        if ov.get("loop_input"):
            cap = float(ov["at"]) + float(ov["duration"]) + 0.5
            cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{cap:.3f}",
                    "-i", ov["asset"]]
        else:
            cmd += ["-i", ov["asset"]]

    fc_parts: List[str] = []
    prev = "0:v"
    for i, ov in enumerate(overlays):
        idx = i + 1                       # ffmpeg input index of this asset
        kind = str(ov.get("kind", "image"))
        at = float(ov["at"])
        end = at + float(ov["duration"])

        # Optional per-input pre-filter: alpha fade-in (logo rows). The input
        # must be a timed stream (loop_input) for `fade` to ramp across frames.
        src = f"{idx}:v"
        fade = ov.get("fade_in")
        if fade and float(fade) > 0:
            pre = f"ovsrc{idx}"
            fc_parts.append(
                f"[{idx}:v]format=rgba,"
                f"fade=t=in:st={at:.3f}:d={float(fade):.3f}:alpha=1[{pre}]"
            )
            src = pre

        lbl = f"ov{idx}"
        if kind == "lower_third":
            # Slide in from off-screen left over 0.4s, then hold at x=60. The
            # x-expression is single-quoted so its commas are not read as filter
            # separators (same trick as enable=).
            at4 = at + 0.4
            xexpr = (
                f"if(gte(t,{at4:.3f}),60,"
                f"60-(1-(t-{at:.3f})/0.4)*(overlay_w+120))"
            )
            fc_parts.append(
                f"[{prev}][{src}]overlay=x='{xexpr}':y=1450:"
                f"enable='between(t,{at:.3f},{end:.3f})'[{lbl}]"
            )
        elif kind == "video":
            # A moving B-roll clip (e.g. flux_morph). Reset its PTS and offset to
            # the overlay window so it plays from its OWN frame 0 at `at` (without
            # this it would show the middle of the clip / EOF). Full-frame cutaway,
            # gated to its window; its audio is ignored (base audio stays master).
            pre = f"vid{idx}"
            fc_parts.append(
                f"[{src}]setpts=PTS-STARTPTS+{at:.3f}/TB,setsar=1[{pre}]"
            )
            fc_parts.append(
                f"[{prev}][{pre}]overlay=0:0:"
                f"enable='between(t,{at:.3f},{end:.3f})'[{lbl}]"
            )
        else:
            # hook_card / logo_row / single image: pre-positioned full-frame, so
            # it sits at 0:0 and is gated to its output-timeline window. The
            # window bounds are single-quoted so the comma inside between() is not
            # read as a filter separator.
            fc_parts.append(
                f"[{prev}][{src}]overlay=0:0:"
                f"enable='between(t,{at:.3f},{end:.3f})'[{lbl}]"
            )
        prev = lbl

    # Subtitles LAST, on top of every overlay.
    if ass_path:
        fc_parts.append(
            f"[{prev}]subtitles=filename={_escape_subs_path(ass_path)}[vout]"
        )
        vmap = "[vout]"
    else:
        vmap = f"[{prev}]"

    # loudnorm the audio in the same graph (single, end-of-chain normalization).
    fc_parts.append(f"[0:a]{audio_polish.loudnorm_af()}[aout]")

    cmd += [
        "-filter_complex", ";".join(fc_parts),
        "-map", vmap, "-map", "[aout]",
        "-t", f"{base_dur:.3f}",   # never let a looped overlay extend the runtime
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), "-crf", "18",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out_path,
    ]
    _run(cmd, what="final pass (overlays + subtitles + loudnorm)")


def _escape_subs_path(path: str) -> str:
    r"""Escape a path for the ``subtitles=filename=`` filter value.

    Special to the filtergraph/option parser: '\', ':', "'" and ','.
    """
    return (
        path.replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
    )


# --------------------------------------------------------------------------- #
# Image inserts (B-roll / logos / generated scenes)
#
# Each insert is resolved to a real raster (Wikimedia logo, generated scene, or
# a local file) and then wrapped into a *pre-positioned* 1080x1920 RGBA overlay
# PNG by engine/image_overlay.py. The PNGs are composited in final_pass() BEFORE
# subtitles. Resolution is best-effort: a failed fetch/gen/file is warned about
# and skipped, never fatal — overlays are enhancements and fetch/gen are
# network-dependent.
# --------------------------------------------------------------------------- #
_VALID_PLACEMENTS = ("under_caption", "top", "corner_tr", "full")
# Sensible default placement per source kind: logos read best on a chip just
# above the caption band; generated scenes and local files fill the frame.
_DEFAULT_PLACEMENT = {
    "wikimedia": "under_caption",
    "file": "full",
    "gen": "full",
}


def load_images_spec(images_arg: Optional[str],
                     ranges_path: Optional[str]) -> List[Dict[str, Any]]:
    """Collect the image-insert specs from the EDL and/or the --images arg.

    Two interchangeable sources, concatenated (EDL first) so neither is silently
    dropped:
      * the ``images`` array inside the extended EDL passed as ``--ranges``;
      * ``--images``, which is either a path to a JSON file OR an inline JSON
        string, holding a bare array or an object with an ``images`` array.

    Returns a (possibly empty) list of raw spec dicts; resolution/validation is
    done later by :func:`resolve_images`.
    """
    specs: List[Dict[str, Any]] = []

    # 1) images embedded in the extended EDL (the --ranges file). By the time we
    #    get here load_ranges() has already validated this file is JSON.
    if ranges_path and os.path.isfile(ranges_path):
        try:
            with open(ranges_path, "r", encoding="utf-8") as fh:
                edl = json.load(fh)
            if isinstance(edl, dict) and isinstance(edl.get("images"), list):
                specs.extend(edl["images"])
        except (json.JSONDecodeError, OSError):
            pass  # the ranges side already raised / will raise a clearer error

    # 2) --images: a file path or an inline JSON string.
    if images_arg:
        if os.path.isfile(images_arg):
            with open(images_arg, "r", encoding="utf-8") as fh:
                try:
                    data: Any = json.load(fh)
                except json.JSONDecodeError as exc:
                    raise BuildError(f"--images file {images_arg!r} is not valid JSON: {exc}")
        else:
            try:
                data = json.loads(images_arg)
            except json.JSONDecodeError as exc:
                raise BuildError(
                    f"--images is neither an existing file nor valid inline JSON: {exc}"
                )
        if isinstance(data, dict) and isinstance(data.get("images"), list):
            specs.extend(data["images"])
        elif isinstance(data, list):
            specs.extend(data)
        else:
            raise BuildError(
                "--images must be a JSON array of image specs, or an object with "
                "an 'images' array."
            )

    return specs


def _infer_source(entry: Dict[str, Any]) -> str:
    """Return the normalized insert source: 'wikimedia' | 'gen' | 'file' | ''."""
    s = str(entry.get("source") or "").strip().lower()
    if s in ("wikimedia", "gen", "file"):
        return s
    # friendly aliases
    if s in ("wiki", "logo", "real"):
        return "wikimedia"
    if s in ("pollinations", "generate", "generated", "ai", "diffusion"):
        return "gen"
    if s in ("local", "path", "disk"):
        return "file"
    # infer from which payload key is present
    if entry.get("file") or entry.get("path"):
        return "file"
    if entry.get("prompt"):
        return "gen"
    if entry.get("query"):
        return "wikimedia"
    return ""


def _resolve_one_image_source(
    entry: Dict[str, Any],
    placement: str,
    source_dir: str,
    assets_dir: str,
) -> Optional[str]:
    """Resolve one insert spec to an absolute raster path, or None on failure.

    wikimedia -> image_fetch.fetch_image; gen -> image_gen.gen_image;
    file -> the local path (absolute, cwd-relative, or source-dir-relative).
    """
    src = _infer_source(entry)

    if src == "file":
        f = entry.get("file") or entry.get("path")
        if not f:
            print("[build_short] image insert source=file but no 'file' path; skipping.",
                  file=sys.stderr)
            return None
        cands = ([f] if os.path.isabs(f)
                 else [os.path.abspath(f), os.path.join(source_dir, f)])
        for c in cands:
            if os.path.isfile(c):
                return os.path.abspath(c)
        print(f"[build_short] image file not found: {f!r} (tried {cands}); skipping.",
              file=sys.stderr)
        return None

    if src == "wikimedia":
        q = entry.get("query") or entry.get("q") or entry.get("label")
        if not q:
            print("[build_short] wikimedia image insert has no 'query'; skipping.",
                  file=sys.stderr)
            return None
        want = str(entry.get("want", "logo"))
        try:
            path = image_fetch.fetch_image(str(q), assets_dir, want=want)
        except Exception as exc:  # fetch_image promises None, but stay defensive
            print(f"[build_short] image_fetch error for {q!r}: {exc}; skipping.",
                  file=sys.stderr)
            return None
        if not path:
            print(f"[build_short] could not fetch a Wikimedia image for {q!r}; skipping.",
                  file=sys.stderr)
        return path

    if src == "gen":
        prompt = entry.get("prompt") or entry.get("query")
        if not prompt:
            print("[build_short] gen image insert has no 'prompt'; skipping.",
                  file=sys.stderr)
            return None
        # A portrait default frames the 'full' placement nicely; chips contain
        # the element anyway, so a square is fine there.
        if placement == "full":
            dw, dh = 1080, 1350
        else:
            dw, dh = 1024, 1024
        try:
            w = int(entry.get("width", dw))
            h = int(entry.get("height", dh))
            seed = int(entry.get("seed", 0) or 0)
        except (TypeError, ValueError):
            w, h, seed = dw, dh, 0
        backend = entry.get("backend")
        try:
            path = image_gen.gen_image(str(prompt), assets_dir,
                                       width=w, height=h, seed=seed, backend=backend)
        except Exception as exc:  # gen_image promises None, but stay defensive
            print(f"[build_short] image_gen error for {prompt!r}: {exc}; skipping.",
                  file=sys.stderr)
            return None
        if not path:
            print(f"[build_short] could not generate an image for {prompt!r}; skipping.",
                  file=sys.stderr)
        return path

    print(f"[build_short] image insert has unknown source {entry.get('source')!r}; "
          f"skipping. ({entry})", file=sys.stderr)
    return None


def resolve_images(
    images: List[Dict[str, Any]],
    kit: Dict[str, Any],
    source_dir: str,
    tmpdir: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Resolve each insert spec to a positioned 1080x1920 overlay PNG.

    Two insert shapes are handled:
      * a single insert (``query`` / ``prompt`` / ``file``) -> one positioned
        overlay via image_overlay.make_overlay (kind ``"image"``);
      * a multi-logo row (``queries: [..]``, 2-4 marks) -> a centered chip row
        via image_overlay.make_logo_row (kind ``"logo_row"``, alpha fade-in).

    Returns ``(composites, normalized)``:
      * ``composites`` -> uniform overlay specs ({"kind","asset","at","duration",
        ...}; see final_pass) for compositing.
      * ``normalized`` -> the same inserts as written into the EDL artifact
        (resolved paths + the fields we actually used), for inspection/re-render.

    Any spec that cannot be resolved (bad shape, fetch/gen failure, missing file,
    overlay build error) is warned about and skipped — never fatal.
    """
    assets_dir = os.path.join(tmpdir, "assets")
    ov_dir = os.path.join(tmpdir, "overlays")
    os.makedirs(assets_dir, exist_ok=True)
    os.makedirs(ov_dir, exist_ok=True)

    composites: List[Dict[str, Any]] = []
    normalized: List[Dict[str, Any]] = []

    for i, entry in enumerate(images):
        if not isinstance(entry, dict):
            print(f"[build_short] image insert #{i} is not an object; skipping.",
                  file=sys.stderr)
            continue

        # timing — shared by single inserts and multi-logo rows.
        try:
            at = float(entry.get("at", 0.0))
        except (TypeError, ValueError):
            at = 0.0
        try:
            dur = float(entry.get("duration", 2.5))
        except (TypeError, ValueError):
            dur = 2.5
        if dur <= 0:
            print(f"[build_short] image insert #{i} has non-positive duration; skipping.",
                  file=sys.stderr)
            continue

        # --- Multi-logo row: entry carries a non-empty 'queries' list --------
        queries = entry.get("queries")
        if isinstance(queries, list) and any(str(q).strip() for q in queries):
            row = _resolve_logo_row(entry, queries, kit, at, dur, i,
                                    assets_dir, ov_dir)
            if row is not None:
                composite, nrm = row
                composites.append(composite)
                normalized.append(nrm)
            continue

        # --- Single insert (logo / generated scene / local file) -------------
        src = _infer_source(entry)
        default_placement = _DEFAULT_PLACEMENT.get(src, "under_caption")
        placement = entry.get("placement") or default_placement
        if placement not in _VALID_PLACEMENTS:
            print(f"[build_short] image insert #{i} unknown placement {placement!r}; "
                  f"using {default_placement!r}.", file=sys.stderr)
            placement = default_placement

        label = entry.get("label")
        label = str(label) if label else None

        img_path = _resolve_one_image_source(entry, placement, source_dir, assets_dir)
        if not img_path or not os.path.isfile(img_path):
            continue  # already warned

        ov_png = os.path.join(ov_dir, f"overlay_{i:03d}.png")
        try:
            ov_png = image_overlay.make_overlay(img_path, placement, ov_png, kit,
                                                label=label)
        except Exception as exc:
            print(f"[build_short] make_overlay failed for insert #{i} "
                  f"({img_path}): {exc}; skipping.", file=sys.stderr)
            continue

        composites.append({"kind": "image", "asset": ov_png, "at": at, "duration": dur})

        nrm: Dict[str, Any] = {
            "source": src or "unknown",
            "at": round(at, 3),
            "duration": round(dur, 3),
            "placement": placement,
            "resolved_image": img_path,
            "overlay_png": ov_png,
        }
        for k in ("query", "prompt", "file", "path", "label", "seed",
                  "want", "backend", "width", "height"):
            if entry.get(k) is not None:
                nrm[k] = entry[k]
        normalized.append(nrm)

    return composites, normalized


# Placements make_logo_row understands; "logo_row" (or anything else falls back
# to "under_caption", matching make_logo_row's own behaviour).
_LOGO_ROW_PLACEMENTS = ("under_caption", "top", "center")


def _resolve_logo_row(
    entry: Dict[str, Any],
    queries: List[Any],
    kit: Dict[str, Any],
    at: float,
    dur: float,
    idx: int,
    assets_dir: str,
    ov_dir: str,
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Fetch up to 4 logos for a ``queries`` insert and build a logo-row overlay.

    Returns ``(composite, normalized)`` or ``None`` when no logo could be
    fetched. Never raises — a query that can't be resolved is warned about and
    dropped (its label, if any, drops with it so the rest stay aligned).
    """
    placement = entry.get("placement") or "under_caption"
    row_placement = placement if placement in _LOGO_ROW_PLACEMENTS else "under_caption"
    want = str(entry.get("want", "logo"))
    raw_labels = entry.get("labels")
    labels = raw_labels if isinstance(raw_labels, list) else []

    logo_paths: List[str] = []
    used_queries: List[Any] = []
    used_labels: List[Optional[str]] = []
    for qi, q in enumerate(queries):
        if len(logo_paths) >= 4:  # make_logo_row is designed for up to 4 marks
            break
        qs = str(q).strip()
        if not qs:
            continue
        try:
            p = image_fetch.fetch_image(qs, assets_dir, want=want)
        except Exception as exc:  # fetch_image promises None, but stay defensive
            print(f"[build_short] image_fetch error for logo {qs!r}: {exc}; "
                  f"dropping from row.", file=sys.stderr)
            p = None
        if p and os.path.isfile(p):
            logo_paths.append(p)
            used_queries.append(q)
            lbl = labels[qi] if qi < len(labels) else None
            used_labels.append(str(lbl) if lbl else None)
        else:
            print(f"[build_short] could not fetch a logo for {qs!r}; "
                  f"dropping from row.", file=sys.stderr)

    if not logo_paths:
        print(f"[build_short] logo-row insert #{idx} resolved no logos; skipping.",
              file=sys.stderr)
        return None

    ov_png = os.path.join(ov_dir, f"logo_row_{idx:03d}.png")
    try:
        ov_png = image_overlay.make_logo_row(
            logo_paths, ov_png, kit, placement=row_placement,
            labels=used_labels if any(used_labels) else None,
        )
    except Exception as exc:
        print(f"[build_short] make_logo_row failed for insert #{idx}: {exc}; skipping.",
              file=sys.stderr)
        return None

    composite = {
        "kind": "logo_row",
        "asset": ov_png,
        "at": at,
        "duration": dur,
        "loop_input": True,   # looped stream so the alpha fade has frames to ramp
        "fade_in": 0.3,
    }
    nrm: Dict[str, Any] = {
        "source": "logo_row",
        "at": round(at, 3),
        "duration": round(dur, 3),
        "placement": row_placement,
        "queries": used_queries,
        "overlay_png": ov_png,
    }
    if any(used_labels):
        nrm["labels"] = used_labels
    return composite, nrm


# --------------------------------------------------------------------------- #
# Hook card (animated full-frame title) + lower-thirds (name/credential bars)
#
# Both are branded motion-graphics from engine/motiongfx.py and, like image
# inserts, composite in final_pass() BEFORE subtitles (Hard Rule). The hook card
# is an opaque full-frame mp4 that covers the output's first DUR seconds;
# lower-thirds are tight RGBA PNGs that slide in from the left and hold. Both are
# best-effort: a render failure is warned about and skipped, never fatal.
# --------------------------------------------------------------------------- #
def load_hook_card_spec(
    hook_card_arg: Optional[str],
    highlight_arg: Optional[str],
    bg_arg: Optional[str],
    ranges_path: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Merge the hook-card spec from the EDL and the CLI (CLI wins per field).

    EDL: a top-level ``hook_card`` object ``{text, highlight, bg, duration}``.
    CLI: ``--hook-card TEXT`` [``--hook-card-highlight WORD``]
         [``--hook-card-bg bone|navy``].
    Returns the merged spec, or ``None`` when no text was supplied anywhere.
    """
    spec: Dict[str, Any] = {}
    if ranges_path and os.path.isfile(ranges_path):
        try:
            with open(ranges_path, "r", encoding="utf-8") as fh:
                edl = json.load(fh)
            if isinstance(edl, dict) and isinstance(edl.get("hook_card"), dict):
                spec.update(edl["hook_card"])
        except (json.JSONDecodeError, OSError):
            pass  # the ranges side already raised / will raise a clearer error
    if hook_card_arg is not None:
        spec["text"] = hook_card_arg
    if highlight_arg is not None:
        spec["highlight"] = highlight_arg
    if bg_arg is not None:
        spec["bg"] = bg_arg
    if not str(spec.get("text", "")).strip():
        return None
    return spec


def resolve_hook_card(
    spec: Optional[Dict[str, Any]],
    kit: Dict[str, Any],
    tmpdir: str,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Render the hook card to a full-frame mp4 -> ``(composites, normalized)``.

    The card is opaque and covers the output's first ``duration`` seconds
    (overlay 0:0, enable between 0 and duration). A render failure is warned
    about and skipped — never fatal.
    """
    if not spec or not str(spec.get("text", "")).strip():
        return [], None

    text = str(spec["text"])
    highlight = spec.get("highlight")
    highlight = str(highlight) if highlight else None
    bg = str(spec.get("bg") or "bone")
    try:
        dur = float(spec.get("duration", HOOK_DUR))
    except (TypeError, ValueError):
        dur = HOOK_DUR
    if dur <= 0:
        dur = HOOK_DUR

    mp4 = os.path.join(tmpdir, "hook_card.mp4")
    try:
        mp4 = motiongfx.hook_card(text, mp4, kit, duration=dur,
                                  highlight=highlight, bg=bg)
    except Exception as exc:  # ValueError / MotionGfxError / anything — non-fatal
        print(f"[build_short] hook card render failed: {exc}; skipping.",
              file=sys.stderr)
        return [], None

    composite = {"kind": "hook_card", "asset": mp4, "at": 0.0, "duration": dur}
    nrm: Dict[str, Any] = {
        "text": text,
        "highlight": highlight,
        "bg": bg,
        "duration": round(dur, 3),
        "clip": mp4,
    }
    return [composite], nrm


def parse_lower_third_cli(s: str) -> Optional[Dict[str, Any]]:
    """Parse a ``--lower-third "Name|Credential|AT|DUR"`` string into a spec dict.

    Only the name is required; missing AT/DUR fall back to defaults, and a
    malformed number is tolerated (treated as the default).
    """
    parts = [p.strip() for p in str(s).split("|")]
    name = parts[0] if parts else ""
    if not name:
        return None
    credential = parts[1] if len(parts) > 1 else ""
    at = 0.0
    if len(parts) > 2 and parts[2]:
        try:
            at = float(parts[2])
        except ValueError:
            at = 0.0
    dur = LOWER_THIRD_DUR
    if len(parts) > 3 and parts[3]:
        try:
            dur = float(parts[3])
        except ValueError:
            dur = LOWER_THIRD_DUR
    return {"name": name, "credential": credential, "at": at, "duration": dur}


def load_lower_thirds_spec(
    lower_third_args: Optional[List[str]],
    ranges_path: Optional[str],
) -> List[Dict[str, Any]]:
    """Collect lower-third specs from the EDL and the CLI (EDL entries first).

    EDL: a top-level ``lower_thirds`` array of ``{name, credential, at, duration}``.
    CLI: repeatable ``--lower-third "Name|Credential|AT|DUR"``.
    """
    specs: List[Dict[str, Any]] = []
    if ranges_path and os.path.isfile(ranges_path):
        try:
            with open(ranges_path, "r", encoding="utf-8") as fh:
                edl = json.load(fh)
            if isinstance(edl, dict) and isinstance(edl.get("lower_thirds"), list):
                specs.extend(edl["lower_thirds"])
        except (json.JSONDecodeError, OSError):
            pass
    for s in (lower_third_args or []):
        parsed = parse_lower_third_cli(s)
        if parsed:
            specs.append(parsed)
    return specs


def resolve_lower_thirds(
    specs: List[Dict[str, Any]],
    kit: Dict[str, Any],
    tmpdir: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Render each lower-third to a tight RGBA PNG -> ``(composites, normalized)``.

    Each composite slides in from the left and holds (the slide is built in
    final_pass). A spec without a name, or a render failure, is warned about and
    skipped — never fatal.
    """
    composites: List[Dict[str, Any]] = []
    normalized: List[Dict[str, Any]] = []
    for i, entry in enumerate(specs):
        if not isinstance(entry, dict):
            print(f"[build_short] lower-third #{i} is not an object; skipping.",
                  file=sys.stderr)
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            print(f"[build_short] lower-third #{i} has no name; skipping.",
                  file=sys.stderr)
            continue
        credential = str(entry.get("credential") or entry.get("title") or "")
        try:
            at = float(entry.get("at", 0.0))
        except (TypeError, ValueError):
            at = 0.0
        try:
            dur = float(entry.get("duration", LOWER_THIRD_DUR))
        except (TypeError, ValueError):
            dur = LOWER_THIRD_DUR
        if dur <= 0:
            print(f"[build_short] lower-third #{i} has non-positive duration; skipping.",
                  file=sys.stderr)
            continue

        png = os.path.join(tmpdir, f"lower_third_{i:03d}.png")
        try:
            png = motiongfx.lower_third_png(name, credential, png, kit)
        except Exception as exc:  # non-fatal — overlays are enhancements
            print(f"[build_short] lower_third_png failed for #{i} ({name!r}): "
                  f"{exc}; skipping.", file=sys.stderr)
            continue

        composites.append({
            "kind": "lower_third", "asset": png, "at": at, "duration": dur,
        })
        normalized.append({
            "name": name,
            "credential": credential,
            "at": round(at, 3),
            "duration": round(dur, 3),
            "png": png,
        })
    return composites, normalized


# --------------------------------------------------------------------------- #
# Extended-EDL artifact
# --------------------------------------------------------------------------- #
def build_edl_artifact(
    *,
    account: str,
    profile_name: str,
    source: str,
    ranges: List[Dict[str, float]],
    punch_default: Optional[float],
    captions_on: bool,
    cta_on: bool,
    keyword: str,
    cta_text: str,
    images: Optional[List[Dict[str, Any]]] = None,
    loop_cfg: Optional[Dict[str, Any]] = None,
    hook_card: Optional[Dict[str, Any]] = None,
    lower_thirds: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Assemble the extended EDL we acted on (for inspection / re-render)."""
    stem = os.path.basename(source)
    out_ranges = []
    for r in ranges:
        zoom = r.get("zoom", punch_default if (punch_default and punch_default > 1.0) else None)
        entry: Dict[str, Any] = {
            "source": stem,
            "start": round(r["start"], 3),
            "end": round(r["end"], 3),
        }
        if zoom and zoom > 1.0:
            entry["transform"] = {"zoom": round(float(zoom), 4)}
        out_ranges.append(entry)

    edl: Dict[str, Any] = {
        "account": account,
        "profile": profile_name,
        "sources": {stem: os.path.abspath(source)},
        "ranges": out_ranges,
        "transform": {
            "normalize": f"{TARGET_W}x{TARGET_H}",
            "punch_in": (round(float(punch_default), 4)
                         if (punch_default and punch_default > 1.0) else None),
            "fps": FPS,
        },
        "captions": {"style": "animated" if captions_on else "off"},
        "cta": {"on": bool(cta_on), "keyword": keyword, "text": cta_text} if cta_on
                else {"on": False},
        "_engine": "build_short.py (self-contained MVP render)",
    }
    # Hook card actually rendered (the resolved clip + the copy we used).
    if hook_card:
        edl["hook_card"] = hook_card
    # Lower-thirds actually rendered (resolved PNGs + their timing).
    if lower_thirds:
        edl["lower_thirds"] = lower_thirds
    # Image inserts actually composited (resolved paths + the fields we used).
    if images:
        edl["images"] = images
    # Seamless-loop post-process record.
    if loop_cfg and loop_cfg.get("on"):
        edl["loop"] = {
            "on": True,
            "mode": loop_cfg.get("mode", "crossfade"),
            "duration": round(float(loop_cfg.get("duration", 0.5)), 3),
        }
    else:
        edl["loop"] = {"on": False}
    return edl


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def build(args: argparse.Namespace) -> int:
    source = os.path.abspath(args.source)
    if not os.path.isfile(source):
        raise BuildError(f"--source not found: {source!r}")
    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    # 1) brandkit + profile, probe source.
    kit = brandkit.load_brandkit(args.brandkit)
    profile = load_profile(args.profile)
    src_w, src_h, duration = probe_video(source)
    print(f"[build_short] source {src_w}x{src_h}, {duration:.2f}s", file=sys.stderr)

    # punch-in: profile['punch_in'] is a dict {pct,frames}; truthy => enabled.
    punch_default: Optional[float] = None
    pin = profile.get("punch_in")
    if pin:
        if isinstance(pin, dict) and "pct" in pin:
            try:
                punch_default = float(pin["pct"]) / 100.0
            except (TypeError, ValueError):
                punch_default = 1.12
        else:
            punch_default = 1.12

    # --style: a reel "look" (engine/styles/<name>.json) layered over the brand
    # kit + profile. A style sets grade + punch + caption preset + (later) sfx,
    # but NEVER colours (identity stays in the brand kit) and NEVER on-screen
    # text — so it is fully compatible with the spoken_only face-reel policy.
    style_grade: Optional[str] = None
    style_sfx: Optional[Dict[str, Any]] = None
    style_transition: Optional[Dict[str, Any]] = None
    style_name = getattr(args, "style", None)
    if style_name:
        if load_style is None:
            print("[build_short] --style requested but engine/styles is "
                  "unavailable; ignoring.", file=sys.stderr)
        else:
            try:
                style = load_style(style_name)
            except Exception as exc:
                raise BuildError(
                    f"--style {style_name!r}: {exc} "
                    f"(available: {', '.join(available_styles()) or 'none'})"
                ) from exc
            # grade -> resolve the preset name to an ffmpeg filter string.
            g = (style.get("grade") or {})
            gp = g.get("preset") if isinstance(g, dict) else g
            if gp and gp != "none":
                if _grade is None:
                    print("[build_short] style grade requested but helpers/grade "
                          "is unavailable; skipping grade.", file=sys.stderr)
                else:
                    try:
                        style_grade = _grade.get_preset(str(gp))
                    except Exception as exc:
                        print(f"[build_short] style grade {gp!r} skipped: {exc}",
                              file=sys.stderr)
            # punch -> override the profile default.
            spin = style.get("punch_in")
            if isinstance(spin, dict) and "pct" in spin:
                try:
                    punch_default = float(spin["pct"]) / 100.0
                except (TypeError, ValueError):
                    pass
            # captions -> merge preset + NON-COLOUR overrides onto the brand kit's
            # caption_style. Colours (fill/highlight/outline) stay from the kit.
            scap = style.get("captions") or {}
            cap_style = dict(kit.get("caption_style") or {})
            if scap.get("preset"):
                cap_style["preset"] = scap["preset"]
            for k, v in (scap.get("overrides") or {}).items():
                if k not in ("fill", "highlight", "outline"):
                    cap_style[k] = v
            kit["caption_style"] = cap_style
            # sfx config -> applied at the cut seams after concat (audio only).
            ssfx = style.get("sfx")
            if isinstance(ssfx, dict):
                style_sfx = ssfx
            # transition config -> xfade on a fraction of the cut seams.
            strans = style.get("transition")
            if isinstance(strans, dict):
                style_transition = strans
            print(f"[build_short] style '{style_name}': grade="
                  f"{(gp if style_grade else 'none')}, punch={punch_default}, "
                  f"caption={cap_style.get('preset')}, "
                  f"sfx={(style_sfx or {}).get('intensity', 'off')}, "
                  f"transition={(style_transition or {}).get('default', 'hard_cut')}",
                  file=sys.stderr)

    captions_cfg = profile.get("captions") or {}
    captions_on = bool(captions_cfg.get("on", True)) and bool(captions_cfg.get("animated", True))

    # CTA: profile cta.on plus brandkit copy, overridable on the CLI.
    cta_profile = profile.get("cta") or {}
    cta_on = bool(cta_profile.get("on", False))
    kit_cta = kit.get("cta") or {}
    keyword = args.keyword or kit_cta.get("keyword", "")
    cta_text = args.cta or kit_cta.get("text", "")
    if args.cta or args.keyword:
        cta_on = True  # explicit CLI copy forces the card on

    # Face-reel added-text policy (engine/SURFACES.md): a profile marked
    # "added_text": "spoken_only" (or cta.spoken_only) is a founder talking-head
    # surface where the ONLY on-screen text may be spoken-word captions. Suppress
    # every unspoken overlay — CTA endcard, hook card, lower-third/wordmark.
    # Cards live on the product-video path (engine/templates/product_video.py),
    # never on a face reel.
    added_text = str(profile.get("added_text") or "").strip().lower()
    spoken_only = added_text == "spoken_only" or bool(cta_profile.get("spoken_only", False))
    if spoken_only:
        if cta_on:
            print("[build_short] spoken_only profile: suppressing CTA endcard "
                  "(unspoken text not allowed on face reels; put the CTA in the "
                  "post caption). See engine/SURFACES.md.", file=sys.stderr)
        cta_on = False
        if getattr(args, "hook_card", None):
            print("[build_short] spoken_only profile: ignoring --hook-card "
                  "(no title cards on face reels).", file=sys.stderr)
            args.hook_card = None
        if getattr(args, "lower_third", None):
            print("[build_short] spoken_only profile: ignoring --lower-third "
                  "(no name banners/wordmarks on face reels).", file=sys.stderr)
            args.lower_third = None

    # 2) ranges + words on the output timeline.
    ranges = load_ranges(args.ranges, duration)
    if args.transcript:
        if not os.path.isfile(args.transcript):
            raise BuildError(f"--transcript not found: {args.transcript!r}")
        with open(args.transcript, "r", encoding="utf-8") as fh:
            try:
                tdata = json.load(fh)
            except json.JSONDecodeError as exc:
                raise BuildError(f"--transcript {args.transcript!r} is not valid JSON: {exc}") from exc
        words = parse_transcript(tdata)
    else:
        words = load_transcript_for_source(source)
    # Transition plan first: it shortens the output timeline at each xfade seam,
    # so the SAME overlaps must drive the caption map (kept in sync). The caption
    # map itself is built AFTER encoding, off the ACTUAL segment durations.
    seg_durations = [float(r["end"]) - float(r["start"]) for r in ranges]
    trans_plan, seam_overlaps = plan_transitions(seg_durations, style_transition)
    n_trans = sum(1 for t in trans_plan if t)

    # temp workspace
    stem = os.path.splitext(os.path.basename(source))[0]
    safe_stem = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)
    tmpdir = os.path.join("/tmp", f"cz_build_{safe_stem}")
    if os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)

    # 3) encode each range with identical params.
    seg_paths: List[str] = []
    for i, rng in enumerate(ranges):
        seg = os.path.join(tmpdir, f"seg_{i:03d}.mp4")
        punch = rng.get("zoom", punch_default)
        encode_segment(source, rng, src_w, src_h, seg, punch=punch, grade=style_grade)
        seg_paths.append(seg)

    # 3b) caption map off ACTUAL encoded durations (ffmpeg frame-snaps segments a
    #     touch longer than end-start; using nominal drifts captions over a
    #     multi-cut reel). range_out_starts[k] = sum(actual durs before k) minus
    #     the xfade overlaps before k.
    actual_durs: List[float] = []
    for seg in seg_paths:
        try:
            actual_durs.append(float(probe_video(seg)[2]))
        except Exception:
            actual_durs.append(0.0)
    range_out_starts: List[float] = []
    acc, ov = 0.0, 0.0
    for k in range(len(ranges)):
        range_out_starts.append(round(acc - ov, 4))
        acc += actual_durs[k] if k < len(actual_durs) else 0.0
        if k < len(seam_overlaps):
            ov += max(0.0, float(seam_overlaps[k]))
    out_words = map_words_to_output(words, ranges, duration,
                                    range_out_starts=range_out_starts)
    out_words = _apply_brand_vocab(out_words, kit)
    print(f"[build_short] {len(ranges)} range(s), {len(out_words)} caption word(s)"
          + (f", {n_trans} transition(s)" if n_trans else ""), file=sys.stderr)

    # 4) concat -> base.mp4 (xfade the planned seams; plain concat otherwise).
    base = os.path.join(tmpdir, "base.mp4")
    concat_with_transitions(seg_paths, seg_durations, trans_plan, base, tmpdir)
    pre_caption = base

    # 4b) style SFX: lay the style's cue at each internal cut seam (audio only,
    #     no-op for a single take / sfx=off). Done on the base before any CTA so
    #     the seam times line up with the footage segments. Internal cut times on
    #     the OUTPUT timeline, overlap-adjusted so cues land on the real seams
    #     even when transitions shortened the timeline.
    # Each internal seam i is the output start of segment i+1 (already
    # overlap- and actual-duration-adjusted in range_out_starts).
    seam_times = [t for t in range_out_starts[1:] if t > 0.0]
    pre_caption = apply_style_sfx(pre_caption, seam_times, style_sfx, tmpdir)

    # 5) optional CTA card appended after the base.
    if cta_on and (keyword or cta_text):
        card = os.path.join(tmpdir, "cta.mp4")
        encode_cta_card(kit, keyword, cta_text, card)
        withcta = os.path.join(tmpdir, "withcta.mp4")
        concat_segments([pre_caption, card], withcta, tmpdir)
        pre_caption = withcta

    # 6) animated captions (.ass) on the speech timeline (none over CTA card).
    ass_path: Optional[str] = None
    if captions_on and out_words:
        ass_path = captions_animated.build_ass(
            out_words, os.path.join(tmpdir, "captions.ass"), kit
        )

    # 6b) graphic overlays -> assets resolved here, composited in the final pass
    #     BEFORE subtitles (Hard Rule). Chain order = z-order (bottom -> top):
    #     hook card, logo rows, lower-thirds, single images. One -i per asset.
    hook_spec = load_hook_card_spec(
        args.hook_card, args.hook_card_highlight, args.hook_card_bg, args.ranges
    )
    hook_overlays, hook_norm = resolve_hook_card(hook_spec, kit, tmpdir)

    lt_spec = load_lower_thirds_spec(args.lower_third, args.ranges)
    lt_overlays, lt_norm = resolve_lower_thirds(lt_spec, kit, tmpdir)

    images_spec = load_images_spec(args.images, args.ranges)
    # --auto-broll: pull real footage/imagery for concept moments and append the
    # inserts to the image spec (resolved through the same file-overlay path).
    # Visual only (no on-screen text) -> safe on spoken_only face reels.
    n_broll = int(getattr(args, "auto_broll", 0) or 0)
    auto_video_overlays: List[Dict[str, Any]] = []
    if n_broll > 0:
        if _auto_broll is None:
            print("[build_short] --auto-broll requested but engine/auto_broll is "
                  "unavailable; skipping.", file=sys.stderr)
        else:
            # motion=True -> abstract concepts become free moving flux_morph clips.
            broll = _auto_broll.auto_broll_specs(
                out_words, duration, kit, max_inserts=n_broll,
                out_dir=os.path.join(tmpdir, "assets"), motion=True,
                reserve_start=2.0,   # never open on B-roll; let the face/hook land
            )
            if broll and spoken_only:
                # Face reel: B-roll is allowed (it's visual) but its LABEL is
                # unspoken text — strip it and show full-frame with no chip/caption.
                for b in broll:
                    b.pop("label", None)
                    b["placement"] = "full"
            if broll:
                # Moving clips -> video overlays (setpts-timed); stills -> image path.
                broll_videos = [b for b in broll if b.get("video")]
                broll_images = [b for b in broll if not b.get("video")]
                for b in broll_videos:
                    auto_video_overlays.append({
                        "kind": "video", "asset": b["file"],
                        "at": float(b["at"]), "duration": float(b["duration"]),
                    })
                images_spec = list(images_spec) + broll_images
                print(f"[build_short] auto-broll: {len(broll)} insert(s) at concept "
                      f"moments ({len(broll_videos)} moving, {len(broll_images)} "
                      f"still)", file=sys.stderr)
            else:
                print("[build_short] auto-broll: no concept moments resolved "
                      "(no keys/assets?); continuing without.", file=sys.stderr)

    # --broll: manually-verified full-bleed cutaways (PATH:AT:DUR). Images are
    # wrapped in a free Ken Burns clip; videos used as-is. Composited full-frame.
    for spec in (getattr(args, "broll", None) or []):
        try:
            path, at_s, dur_s = str(spec).rsplit(":", 2)
            at_f, dur_f = float(at_s), float(dur_s)
        except ValueError:
            raise BuildError(f"--broll must be PATH:AT:DUR, got {spec!r}")
        if not os.path.isfile(path):
            raise BuildError(f"--broll asset not found: {path!r}")
        ext = os.path.splitext(path)[1].lower()
        clip = path
        if ext not in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
            # an image -> full-bleed Ken Burns clip (no border)
            if 'video_gen' in sys.modules or _auto_broll is not None:
                import video_gen as _vg
                clip = _vg.gen_video(image=path, out_dir=os.path.join(tmpdir, "assets"),
                                     duration=dur_f, width=1080, height=1920,
                                     seed=int(at_f * 10) % 100000, backend="kenburns")
            if not clip or not os.path.isfile(clip):
                print(f"[build_short] --broll: could not build clip for {path!r}; "
                      "skipping.", file=sys.stderr)
                continue
        auto_video_overlays.append({"kind": "video", "asset": clip,
                                    "at": at_f, "duration": dur_f})
        print(f"[build_short] --broll: verified cutaway {os.path.basename(path)} "
              f"@ {at_f:.1f}s for {dur_f:.1f}s", file=sys.stderr)

    img_overlays, images_norm = resolve_images(
        images_spec, kit, os.path.dirname(source), tmpdir
    )
    logo_row_overlays = [o for o in img_overlays if o.get("kind") == "logo_row"]
    single_overlays = [o for o in img_overlays if o.get("kind") != "logo_row"]

    overlays = (hook_overlays + logo_row_overlays + lt_overlays
                + single_overlays + auto_video_overlays)
    if overlays:
        print(f"[build_short] overlays: {len(hook_overlays)} hook card, "
              f"{len(logo_row_overlays)} logo row(s), {len(lt_overlays)} "
              f"lower-third(s), {len(single_overlays)} image(s)", file=sys.stderr)

    # 7) final pass: overlays BEFORE subtitles; subtitles LAST; loudnorm at end.
    final_pass(pre_caption, ass_path, output, overlays=overlays)

    # 7b) optional seamless loop: re-encode the finished file so end -> start.
    #     A loop-blend failure is non-fatal — keep the un-looped render.
    loop_cfg: Dict[str, Any] = {"on": bool(args.loop)}
    if args.loop:
        loop_cfg.update(mode=args.loop, duration=float(args.loop_dur))
        tmp_loop = output + ".looptmp.mp4"
        try:
            loop.make_seamless(output, tmp_loop, mode=args.loop,
                               dur=float(args.loop_dur))
            os.replace(tmp_loop, output)
            print(f"[build_short] seamless loop applied "
                  f"({args.loop}, {args.loop_dur}s)", file=sys.stderr)
        except loop.LoopError as exc:
            if os.path.exists(tmp_loop):
                os.remove(tmp_loop)
            loop_cfg["on"] = False
            print(f"[build_short] WARNING: loop failed; keeping un-looped output.\n"
                  f"  ({exc})", file=sys.stderr)

    # 8) extended-EDL artifact next to the output.
    edl = build_edl_artifact(
        account=args.brandkit,
        profile_name=args.profile,
        source=source,
        ranges=ranges,
        punch_default=punch_default,
        captions_on=captions_on,
        cta_on=cta_on and (bool(keyword) or bool(cta_text)),
        keyword=keyword,
        cta_text=cta_text,
        images=images_norm,
        loop_cfg=loop_cfg,
        hook_card=hook_norm,
        lower_thirds=lt_norm,
    )
    edl_path = os.path.splitext(output)[0] + ".edl.json"
    with open(edl_path, "w", encoding="utf-8") as fh:
        json.dump(edl, fh, indent=2)
    print(f"[build_short] wrote extended EDL -> {edl_path}", file=sys.stderr)

    # 9) verify the output.
    info = probe_output(output)
    print(f"[build_short] OUTPUT {output}")
    print(f"  width={info['width']} height={info['height']} "
          f"duration={info['duration']:.2f}s has_audio={info['has_audio']}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build_short.py",
        description="Turn one source video into a branded vertical short "
                    "(self-contained MVP render).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", required=True, help="source video (required)")
    p.add_argument("--transcript", default=None,
                   help="WhisperX word-level JSON. If omitted, runs "
                        "helpers/transcribe.py and reads the cached "
                        "<source_dir>/edit/transcripts/<stem>.json.")
    p.add_argument("--brandkit", default="counza",
                   help="brand kit name (engine/brandkits/<name>.json)")
    p.add_argument("--profile", default="founder_edtech",
                   help="edit profile name (engine/profiles/<name>.json)")
    p.add_argument("--style", default=None,
                   help="reel style preset (engine/styles/<name>.json): "
                        "clean_premium | dark_sizzle | founder_raw | "
                        "hormozi_punch. Sets grade + punch + caption preset "
                        "(never colours, never on-screen text — safe on face "
                        "reels). Omit for the plain brand-kit look.")
    p.add_argument("--auto-broll", type=int, default=0, metavar="N",
                   help="auto-pull up to N real-footage cutaways (Pexels/Pixabay "
                        "stock, school crests, or generated scenes) at concept "
                        "moments in the transcript. Visual only (no on-screen "
                        "text) so it's safe on face reels. Keep small (2-3) for "
                        "talking-head; 0 = off.")
    p.add_argument("--broll", action="append", default=None, metavar="PATH:AT:DUR",
                   help="add ONE verified B-roll cutaway full-frame: a local "
                        "image or video PATH, shown at output time AT for DUR "
                        "seconds (e.g. cornell.jpg:11.5:2.2). Images are wrapped "
                        "in a free full-bleed Ken Burns clip. Repeatable. Use this "
                        "to insert only assets you've visually verified — unlike "
                        "--auto-broll which fetches blind.")
    p.add_argument("--cta", default=None,
                   help="CTA line override (default: brandkit cta.text)")
    p.add_argument("--keyword", default=None,
                   help="CTA keyword override (default: brandkit cta.keyword)")
    p.add_argument("--ranges", default=None,
                   help="extended EDL JSON with a 'ranges' array. If omitted, "
                        "the whole source is one range [0, duration]. May also "
                        "carry an 'images' array (see --images).")
    p.add_argument("--images", default=None,
                   help="image inserts: a path to a JSON file OR an inline JSON "
                        "string, holding an array of image specs (or an object "
                        "with an 'images' array). Merged with any 'images' in "
                        "the --ranges EDL (EDL entries first). Each spec: "
                        "{\"source\":\"wikimedia\"|\"gen\"|\"file\", one of "
                        "\"query\"/\"prompt\"/\"file\", \"at\":sec, "
                        "\"duration\":sec, \"placement\":\"under_caption\"|"
                        "\"top\"|\"corner_tr\"|\"full\", \"label\":opt, "
                        "\"seed\":opt}. A spec may instead use "
                        "\"queries\":[..] (2-4 logos) + optional \"labels\":[..] "
                        "to build one centered logo row. See EDL_SCHEMA.md 2.3.")
    p.add_argument("--hook-card", default=None, metavar="TEXT",
                   help="opening hook title card: an animated full-frame clip "
                        "(engine/motiongfx.hook_card) shown OPAQUELY over the "
                        "first few seconds. Pass the headline text. Also "
                        "settable via the EDL 'hook_card' block.")
    p.add_argument("--hook-card-highlight", default=None, metavar="WORD",
                   help="word/phrase in the hook card drawn in Counza orange.")
    p.add_argument("--hook-card-bg", default=None, choices=["bone", "navy"],
                   help="hook card background: 'bone' (navy text) or 'navy' "
                        "(bone text). Default: bone.")
    p.add_argument("--lower-third", action="append", default=None,
                   metavar="Name|Credential|AT|DUR",
                   help="lower-third name bar that slides in from the left and "
                        "holds at y~1450. Format 'Name|Credential|AT|DUR' (AT/DUR "
                        "in output seconds; only Name required). Repeatable. Also "
                        "settable via the EDL 'lower_thirds' array.")
    p.add_argument("--loop", nargs="?", const="crossfade", default=None,
                   choices=["crossfade", "freeze_match"],
                   help="after rendering, make the short loop seamlessly "
                        "(end flows into start). Bare --loop uses 'crossfade'; "
                        "pass 'freeze_match' for near-static endings. "
                        "Default: off.")
    p.add_argument("--loop-dur", type=float, default=0.5,
                   help="loop blend length in seconds (auto-clamped to "
                        "<= 0.5*clip length). Only used with --loop.")
    p.add_argument("-o", "--output", required=True, help="output .mp4 (required)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return build(args)
    except BuildError as exc:
        print(f"\n[build_short] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
