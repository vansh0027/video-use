#!/usr/bin/env python3
"""compose_best.py — the "best-version" SPLIT-LAYOUT composer for Counza shorts.

Where ``build_short.py`` produces a single full-frame talking-head short (footage
fills the 1080x1920 frame, captions + the odd overlay on top), THIS composer ties
the newer engine modules — ``storyboard`` (visual story plan), ``image_overlay`` /
``image_fetch`` / ``image_gen`` / ``stock`` (top-zone assets), ``motiongfx`` (hook
card + lower-third), ``captions_animated`` (sliding captions), ``audio_polish``
(loudnorm) and ``loop`` (seamless tail) — into ONE editorial SPLIT layout that is
designed to blend into a Meta Reels / TikTok feed.

THE SPLIT LAYOUT (1080x1920, BLACK background — never white/bone, so it reads as
native dark-mode feed content):

    +------------------------------------------+  y=0
    |                                          |
    |   TOP ZONE  (1080 x 1080 square)         |  the VISUAL STORY:
    |   logos / people-free AI scenes / stock  |  storyboard beats, hard-cut
    |   — changes beat by beat over time       |  (optional 0.3s crossfade)
    |                                          |
    +------------------------------------------+  y=1080
    |   CAPTION BAND  (1080 x 233, dark)       |  animated captions live here
    +------------------------------------------+  y=1313   (margin_v=720 in kit)
    |                                          |
    |   FOOTAGE  (1080 x 607, full width)      |  BOTH founders, graded brighter
    |        [ NAME STRIP pinned ~y=1800 ]     |  lower-third over the footage
    +------------------------------------------+  y=1920

Hard Rules honoured (from ``~/Developer/video-use/SKILL.md``):
  * subtitles are burned **LAST**, after every graphic overlay;
  * loudnorm is applied **once**, at the end of the audio chain;
  * a 0.3s crossfade tail makes the clip loop seamlessly (``loop.make_seamless``).

The whole render is a single ``ffmpeg -filter_complex`` invocation: one ``-i`` per
overlay asset (footage strip, each beat square, name strip, hook card), chained
base -> footage -> beats -> name strip -> hook card -> subtitles, with loudnorm on
the audio in the same graph. An inspectable plan JSON is written next to the
output so a human can see exactly which zones/assets/timings were composited.

CLI
---
    compose_best.py --source <video> --transcript <json> --start S --end E \
        [--storyboard <json>] [--hook-card TEXT] [--hook-card-highlight WORD] \
        [--name "Name"] [--credential "Co-founders · Counza"] [--cta TEXT] \
        [--keyword KW] [--brandkit counza] [--no-loop] -o OUTPUT

Stdlib + the sibling engine modules only (which themselves lean on PIL/ffmpeg).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# --- Make sibling engine modules importable regardless of cwd ---------------
# The other engine modules `import brandkit` bare, so the engine dir must be on
# sys.path whether this file is run as a script or imported as engine.compose_best.
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

import brandkit  # noqa: E402
import storyboard  # noqa: E402
import captions_animated  # noqa: E402
import motiongfx  # noqa: E402
import audio_polish  # noqa: E402

# loop is optional at import time: a broken loop module must not stop a render
# (we degrade to "no seamless tail" and say so), exactly like build_short treats
# its optional helpers.
try:
    import loop  # type: ignore
except Exception:  # pragma: no cover - only on a broken tree
    loop = None  # type: ignore


# --------------------------------------------------------------------------- #
# Constants — canvas + the split-layout zone geometry
# --------------------------------------------------------------------------- #
CANVAS_W = 1080
CANVAS_H = 1920

# Top zone: the 1080x1080 square that carries the visual story.
TOPZONE_H = 1080            # y = 0 .. 1080

# Caption band: the dark strip the captions sit in (margin_v=720 in counza.json
# places the caption baseline here). It is not drawn as a separate element — the
# black canvas already provides the dark band; this constant documents the seam.
CAPTION_BAND_TOP = 1080     # y = 1080 .. 1313
FOOTAGE_TOP = 1313          # footage begins here (y), full width to y=1920
FOOTAGE_H = CANVAS_H - FOOTAGE_TOP   # 607 px tall, 1080 wide (16:9 source -> 607)

# Name strip (lower-third) pinned near the bottom edge, over the footage. Sits
# well below the caption band (≈1080-1313) so the two never collide.
NAME_STRIP_X = 40
NAME_STRIP_BOTTOM_MARGIN = 70        # gap from the very bottom edge to the bar
NAME_STRIP_APPEAR_AT = 0.0           # visible the whole clip by default

# Hook card: a full-frame branded title that plays over the first HOOK_DUR
# seconds; captions are suppressed underneath it so they don't collide.
HOOK_DUR = 2.5
HOOK_SUPPRESS_UNTIL = HOOK_DUR + 0.1  # drop caption words ending before this

# Beat crossfade (top-zone) — a short dissolve between story beats. Hard cuts if
# set to 0. Kept small so the story still feels punchy.
BEAT_XFADE = 0.3

# CTA end-card.
CTA_DUR = 2.5

# Seamless-loop crossfade tail length.
LOOP_DUR = 0.3

FPS = 30

# Footage colour grade — the night footage needs lifting. Brightness/contrast/
# saturation bump per the brief (eq is in the confirmed ffmpeg build).
GRADE_EQ = "eq=brightness=0.06:contrast=1.12:saturation=1.12"

# External tools (honour overrides so the same code runs where ffmpeg is the
# full Homebrew build, mirroring build_short.py / motiongfx.py).
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

# Brand-misheard-word fix: WhisperX hears "Counza" as Kansa/Kanza/Konza/etc.
# The brief asks specifically for ``(?i)^k[ae]n[sz]a[s]?$ -> Counza`` on a whole
# token; storyboard.fix_brand also normalises in-sentence occurrences, so we use
# both (token-exact here for caption words, fix_brand for free text).
_BRAND_TOKEN_RE = re.compile(r"(?i)^k[ae]n[sz]a[s]?$")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ComposeError(RuntimeError):
    """Raised with a human-readable message for any unrecoverable failure."""


# --------------------------------------------------------------------------- #
# Subprocess helpers (mirror build_short.py style)
# --------------------------------------------------------------------------- #
def _fmt_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _tail(text: Optional[str], n: int) -> str:
    if not text:
        return ""
    lines = text.rstrip().splitlines()
    return "\n".join("    " + ln for ln in lines[-n:])


def _run(cmd: List[str], *, what: str) -> subprocess.CompletedProcess:
    """Run a command, raising ComposeError with the cmd + stderr tail on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ComposeError(
            f"{what}: executable not found ({cmd[0]!r}). "
            f"Is it installed and on PATH?\n  cmd: {_fmt_cmd(cmd)}"
        ) from exc
    if proc.returncode != 0:
        tail = _tail(proc.stderr, 30) or _tail(proc.stdout, 30) or "(no output)"
        raise ComposeError(
            f"{what} failed (exit {proc.returncode}).\n"
            f"  cmd: {_fmt_cmd(cmd)}\n"
            f"  stderr (last lines):\n{tail}"
        )
    return proc


# --------------------------------------------------------------------------- #
# ffprobe helper
# --------------------------------------------------------------------------- #
def probe_video(path: str) -> Tuple[int, int, float]:
    """Return (display_width, display_height, duration_seconds).

    Uses ffprobe WITHOUT -noautorotate so the dimensions reported are the
    *display* (autorotated) dimensions — the same orientation the footage will
    have once we extract it (ffmpeg autorotates by default, which gives this
    source UPRIGHT per the project notes).
    """
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
        raise ComposeError(
            f"could not parse ffprobe output for {path!r}: {exc}\n"
            f"  raw: {proc.stdout[:400]}"
        ) from exc
    if w <= 0 or h <= 0 or dur <= 0:
        raise ComposeError(f"{path!r} reports nonsensical dims/duration {w}x{h} {dur}s")
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
# Step 1 — clip-local words (filter to [S,E], shift to 0, fix Counza spelling)
# --------------------------------------------------------------------------- #
def _fix_brand_token(word: str) -> str:
    """Token-exact Counza fix: ``(?i)^k[ae]n[sz]a[s]?$`` -> 'Counza'.

    Preserves any leading/trailing punctuation that WhisperX glued on (e.g.
    "Kansa," -> "Counza,") by splitting the alnum core out, substituting it, and
    re-attaching the surrounding punctuation.
    """
    if not word:
        return word
    m = re.match(r"^(\W*)(.*?)(\W*)$", word, flags=re.DOTALL)
    if not m:
        return word
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    if _BRAND_TOKEN_RE.match(core):
        core = "Counza"
    return f"{lead}{core}{trail}"


def load_clip_words(
    transcript_path: str, clip_start: float, clip_end: float
) -> List[Dict[str, Any]]:
    """Load the cached transcript, filter to [clip_start, clip_end), shift to
    clip-local seconds (start at 0), drop punctuation-only tokens, and fix the
    Counza mishearing on each token.

    Accepts the WhisperX shapes used across the engine:
      * {"words": [{text/word, start, end}, ...]}
      * {"word_segments": [...]}
      * {"segments": [{"words": [...]}, ...]}
      * a bare list of word dicts.

    Returns clip-local ``{"word": str, "start": float, "end": float}`` dicts —
    the shape both ``captions_animated.build_ass`` and ``storyboard`` accept
    (storyboard tolerates ``word`` because it reads ``text`` OR is fed via the
    helper below; we hand it a ``text`` alias too).
    """
    if not os.path.isfile(transcript_path):
        raise ComposeError(f"transcript not found: {transcript_path!r}")
    with open(transcript_path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ComposeError(f"transcript {transcript_path!r} is not valid JSON: {exc}")

    # Flatten to a raw list of word dicts (mirror build_short.parse_transcript's
    # source-shape tolerance, but keep timestamps so we can window them).
    raw: List[Any] = []
    if isinstance(data, dict):
        if isinstance(data.get("words"), list):
            raw = data["words"]
        elif isinstance(data.get("word_segments"), list):
            raw = data["word_segments"]
        elif isinstance(data.get("segments"), list):
            for seg in data["segments"]:
                if isinstance(seg, dict) and isinstance(seg.get("words"), list):
                    raw.extend(seg["words"])
    elif isinstance(data, list):
        raw = data

    out: List[Dict[str, Any]] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        s = w.get("start", w.get("s"))
        e = w.get("end", w.get("e"))
        txt = w.get("text", w.get("word", w.get("w")))
        if s is None or e is None or txt is None:
            continue
        try:
            s = float(s)
            e = float(e)
        except (TypeError, ValueError):
            continue
        # Window: keep words whose START falls inside [clip_start, clip_end).
        if not (clip_start <= s < clip_end):
            continue
        word = _fix_brand_token(str(txt).strip())
        # Drop pure-punctuation tokens (no alphanumerics) — not caption words.
        if not any(c.isalnum() for c in word):
            continue
        ls = round(s - clip_start, 3)
        le = round(e - clip_start, 3)
        if le <= ls:
            le = ls + 0.01
        out.append({"word": word, "text": word, "start": ls, "end": le})

    out.sort(key=lambda d: d["start"])
    return out


# --------------------------------------------------------------------------- #
# Step 2 — storyboard beats + top-zone assets
# --------------------------------------------------------------------------- #
def plan_storyboard(
    words: List[Dict[str, Any]],
    clip_dur: float,
    brand: Dict[str, Any],
    storyboard_path: Optional[str],
) -> List[Dict[str, Any]]:
    """Return the list of beats. From ``storyboard_path`` if given, else from
    ``storyboard.storyboard``. Beats must cover [0, clip_dur] contiguously; an
    externally-supplied plan is trusted as-is (it is the author's intent) but
    lightly clamped to the clip window so a stray value can't break ffmpeg.
    """
    if storyboard_path:
        if not os.path.isfile(storyboard_path):
            raise ComposeError(f"--storyboard file not found: {storyboard_path!r}")
        with open(storyboard_path, "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ComposeError(f"--storyboard {storyboard_path!r} not valid JSON: {exc}")
        beats = data.get("beats") if isinstance(data, dict) else data
        if not isinstance(beats, list) or not beats:
            raise ComposeError(f"--storyboard {storyboard_path!r} has no 'beats' array")
        clamped: List[Dict[str, Any]] = []
        for b in beats:
            if not isinstance(b, dict):
                continue
            try:
                bs = max(0.0, float(b.get("start", 0.0)))
                be = min(float(clip_dur), float(b.get("end", clip_dur)))
            except (TypeError, ValueError):
                continue
            if be <= bs:
                continue
            clamped.append({
                "start": round(bs, 3),
                "end": round(be, 3),
                "kind": (b.get("kind") or "none"),
                "query": b.get("query") or "",
                "label": b.get("label"),
            })
        if not clamped:
            raise ComposeError(f"--storyboard {storyboard_path!r} has no usable beats")
        return clamped

    return storyboard.storyboard(words, clip_dur, brand)


def resolve_beat_assets(
    beats: List[Dict[str, Any]],
    out_dir: str,
    brand: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Render each beat's top-zone PNG (1080x1080) via ``storyboard.resolve_beat``.

    Returns the beats with an added ``"asset"`` key (absolute PNG path or None).
    A ``None`` asset means "show nothing in the top zone for this beat" — the
    black canvas shows through, keeping the footage the focus. Network failures
    surface as ``None`` (resolve_beat never raises), so the render always
    proceeds even fully offline.
    """
    resolved: List[Dict[str, Any]] = []
    for i, beat in enumerate(beats):
        asset: Optional[str] = None
        kind = (beat.get("kind") or "none").lower()
        if kind != "none":
            try:
                asset = storyboard.resolve_beat(beat, out_dir, brand)
            except Exception as exc:  # resolve_beat shouldn't raise, but be safe
                print(f"[compose_best] beat {i} ({kind}) asset failed: {exc}",
                      file=sys.stderr)
                asset = None
            if asset is None:
                print(f"[compose_best] beat {i} ({kind}, query={beat.get('query','')!r}) "
                      f"produced no asset — top zone stays black for "
                      f"{beat['start']:.2f}-{beat['end']:.2f}s.", file=sys.stderr)
        out = dict(beat)
        out["asset"] = asset
        resolved.append(out)
    return resolved


# --------------------------------------------------------------------------- #
# Step 3-6 — the single split-layout ffmpeg filtergraph
# --------------------------------------------------------------------------- #
def _escape_subs_path(path: str) -> str:
    r"""Escape a path for the ``subtitles=filename=`` filter value.

    Special to the filtergraph/option parser: '\', ':', "'" and ','. (Identical
    to build_short._escape_subs_path so the two renderers agree.)
    """
    return (
        path.replace("\\", "\\\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
    )


def build_filtergraph(
    *,
    source: str,
    clip_start: float,
    clip_end: float,
    beats: List[Dict[str, Any]],
    name_strip_png: Optional[str],
    name_strip_at: float,
    hook_card_mp4: Optional[str],
    ass_path: Optional[str],
    out_path: str,
) -> Tuple[List[str], Dict[str, Any]]:
    """Construct the full ``ffmpeg`` command for the SPLIT layout in one pass.

    Inputs (one ``-i`` each, in this fixed order so labels are predictable):
        0  base BLACK canvas  (lavfi color, clip_dur long, with a silent track —
                               but we override audio from the footage, see below)
        1  footage            (the extracted [S,E] talking-head, autorotated)
        2..  beat squares     (only beats whose asset is not None; looped PNG)
        N  name strip PNG     (looped, if provided)
        N+1 hook card mp4     (if provided)

    Filtergraph (bottom -> top):
        black canvas
          [+ footage]   scaled to 1080 wide, placed at y=FOOTAGE_TOP
          [+ beat i]    each placed at y=0, gated to its window (opt. xfade)
          [+ name strip] pinned near the bottom edge over the footage
          [+ hook card] full-frame over [0, HOOK_DUR]
          [+ subtitles] burned LAST
        audio: footage audio -> polish -> loudnorm  (single, end of chain)

    Returns ``(cmd, plan)`` where ``plan`` is a JSON-serialisable description of
    every composited element + its zone, for the inspectable plan file.
    """
    clip_dur = round(clip_end - clip_start, 3)

    # --- Assemble the input list ------------------------------------------- #
    # input 0: black canvas, exactly clip_dur long (the runtime anchor).
    cmd: List[str] = [
        FFMPEG, "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={CANVAS_W}x{CANVAS_H}:r={FPS}:d={clip_dur:.3f}",
    ]

    # input 1: the footage. ffmpeg autorotates by default (gives this source
    # UPRIGHT). -ss/-to are INPUT options here so the decode is seeked cheaply;
    # the footage carries the program audio.
    cmd += [
        "-ss", f"{clip_start:.6f}", "-to", f"{clip_end:.6f}", "-i", source,
    ]

    # inputs 2..: beat squares (only those with a real asset). Each PNG is looped
    # into a finite stream (capped just past its window) so a time-based fade has
    # frames to ramp; eof_action=repeat keeps it available across its window.
    beat_inputs: List[Dict[str, Any]] = []
    for b in beats:
        if not b.get("asset"):
            continue
        cap = float(b["end"]) + 0.5
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{cap:.3f}",
                "-i", b["asset"]]
        beat_inputs.append(b)

    # name strip input (looped PNG) — visible for the whole clip from name_at.
    name_idx: Optional[int] = None
    if name_strip_png:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{clip_dur:.3f}",
                "-i", name_strip_png]

    # hook card input (opaque mp4; covers its own window).
    hook_idx: Optional[int] = None
    if hook_card_mp4:
        cmd += ["-i", hook_card_mp4]

    # --- Compute the ffmpeg input indices now the order is fixed ----------- #
    # 0 canvas, 1 footage, then beats, then (name strip?), then (hook?).
    next_idx = 2
    for b in beat_inputs:
        b["_idx"] = next_idx
        next_idx += 1
    if name_strip_png:
        name_idx = next_idx
        next_idx += 1
    if hook_card_mp4:
        hook_idx = next_idx
        next_idx += 1

    # --- Build the filtergraph -------------------------------------------- #
    fc: List[str] = []
    plan_layers: List[Dict[str, Any]] = []

    # Footage: autorotated stream -> scale to 1080 wide (height follows aspect,
    # ~607 for 16:9), grade brighter, force SAR 1. Keep both founders (full
    # width, no crop).
    fc.append(
        f"[1:v]{GRADE_EQ},scale={CANVAS_W}:-2,setsar=1[foot]"
    )
    # Composite footage onto the black canvas at the bottom band.
    fc.append(
        f"[0:v][foot]overlay=x=0:y={FOOTAGE_TOP}:eof_action=pass[base0]"
    )
    plan_layers.append({
        "layer": "footage",
        "zone": {"x": 0, "y": FOOTAGE_TOP, "w": CANVAS_W, "h": FOOTAGE_H},
        "grade": GRADE_EQ,
        "note": "full width so both founders stay in frame; graded brighter",
    })

    prev = "base0"

    # Top-zone beats. Each beat square is placed at y=0, gated to its window.
    # With a crossfade we ramp the incoming beat's alpha over BEAT_XFADE at the
    # cut so beats dissolve rather than pop; the first beat has no predecessor to
    # dissolve from, so it simply appears.
    for n, b in enumerate(beat_inputs):
        idx = b["_idx"]
        at = float(b["start"])
        end = float(b["end"])
        src = f"{idx}:v"
        if BEAT_XFADE > 0 and n > 0:
            pre = f"beatsrc{idx}"
            fc.append(
                f"[{idx}:v]format=rgba,"
                f"fade=t=in:st={at:.3f}:d={BEAT_XFADE:.3f}:alpha=1[{pre}]"
            )
            src = pre
        lbl = f"beat{idx}"
        fc.append(
            f"[{prev}][{src}]overlay=x=0:y=0:eof_action=pass:"
            f"enable='between(t,{at:.3f},{end:.3f})'[{lbl}]"
        )
        prev = lbl
        plan_layers.append({
            "layer": f"beat[{n}]",
            "zone": {"x": 0, "y": 0, "w": CANVAS_W, "h": TOPZONE_H},
            "kind": b.get("kind"),
            "query": b.get("query"),
            "asset": b.get("asset"),
            "window": [round(at, 3), round(end, 3)],
            "crossfade_in": BEAT_XFADE if n > 0 else 0.0,
        })

    # Name strip: pinned near the bottom edge, over the footage. Its PNG height
    # is unknown here (tight bbox), so anchor its BOTTOM to a fixed line via
    # ``y = H - margin - overlay_h`` (overlay_h is the strip's own height). This
    # keeps it visible and clear of the caption band (~1080-1313).
    if name_strip_png and name_idx is not None:
        yexpr = f"{CANVAS_H}-{NAME_STRIP_BOTTOM_MARGIN}-overlay_h"
        enable = ""
        if name_strip_at > 0:
            enable = f":enable='gte(t,{name_strip_at:.3f})'"
        lbl = "named"
        fc.append(
            f"[{prev}][{name_idx}:v]overlay=x={NAME_STRIP_X}:y='{yexpr}':"
            f"eof_action=pass{enable}[{lbl}]"
        )
        prev = lbl
        plan_layers.append({
            "layer": "name_strip",
            "zone": {"x": NAME_STRIP_X,
                     "y_anchor": f"bottom-{NAME_STRIP_BOTTOM_MARGIN}px",
                     "note": "pinned to bottom edge over footage; clear of caption band"},
            "asset": name_strip_png,
            "appears_at": round(name_strip_at, 3),
        })

    # Hook card: full-frame opaque mp4 over [0, HOOK_DUR]. Sits above everything
    # except the captions (which are suppressed under it).
    if hook_card_mp4 and hook_idx is not None:
        lbl = "hooked"
        fc.append(
            f"[{prev}][{hook_idx}:v]overlay=x=0:y=0:eof_action=pass:"
            f"enable='between(t,0,{HOOK_DUR:.3f})'[{lbl}]"
        )
        prev = lbl
        plan_layers.append({
            "layer": "hook_card",
            "zone": {"x": 0, "y": 0, "w": CANVAS_W, "h": CANVAS_H},
            "asset": hook_card_mp4,
            "window": [0.0, HOOK_DUR],
            "note": "full-frame; captions suppressed underneath",
        })

    # Subtitles LAST, on top of every overlay (Hard Rule).
    if ass_path:
        fc.append(
            f"[{prev}]subtitles=filename={_escape_subs_path(ass_path)}[vout]"
        )
        vmap = "[vout]"
        plan_layers.append({
            "layer": "captions",
            "zone": {"band_top": CAPTION_BAND_TOP, "band_bottom": FOOTAGE_TOP,
                     "margin_v": 720},
            "ass": ass_path,
            "note": "burned LAST; sliding-highlight in the caption band",
        })
    else:
        vmap = f"[{prev}]"

    # Audio: program audio from the footage (input 1) -> voice polish -> loudnorm
    # ONCE at the end of the chain (Hard Rule). 30 ms boundary fades smooth the
    # single in/out so the trimmed clip never clicks.
    af_chain = audio_polish.polish_af()
    try:
        af_chain += "," + audio_polish.boundary_afades(clip_dur)
    except ValueError:
        pass  # clip too short for fades — skip them, loudnorm still applies
    af_chain += "," + audio_polish.loudnorm_af()
    fc.append(f"[1:a]{af_chain}[aout]")

    cmd += [
        "-filter_complex", ";".join(fc),
        "-map", vmap, "-map", "[aout]",
        "-t", f"{clip_dur:.3f}",       # the black canvas anchors the runtime
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), "-crf", "18",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out_path,
    ]

    plan = {
        "canvas": {"w": CANVAS_W, "h": CANVAS_H, "bg": "black", "fps": FPS},
        "zones": {
            "top_zone": {"y": [0, TOPZONE_H], "role": "visual story (logos/scenes/stock)"},
            "caption_band": {"y": [CAPTION_BAND_TOP, FOOTAGE_TOP], "role": "animated captions (margin_v=720)"},
            "footage": {"y": [FOOTAGE_TOP, CANVAS_H], "w": CANVAS_W,
                        "role": "talking heads, full width, graded brighter"},
            "name_strip": {"y_anchor": f"~{CANVAS_H - NAME_STRIP_BOTTOM_MARGIN}px (bottom edge)",
                           "role": "lower-third over footage, clear of captions"},
        },
        "clip": {"source_start": clip_start, "source_end": clip_end, "duration": clip_dur},
        "layers_bottom_to_top": plan_layers,
        "hard_rules": [
            "subtitles burned LAST (after all overlays)",
            "loudnorm applied ONCE at end of audio chain",
            "black background (blends into dark feed)",
        ],
    }
    return cmd, plan


# --------------------------------------------------------------------------- #
# CTA end-card + concat
# --------------------------------------------------------------------------- #
def encode_cta_card(
    brand: Dict[str, Any],
    keyword: str,
    text: str,
    out_path: str,
) -> str:
    """Render a ~2.5s navy end-card via ``motiongfx.hook_card`` (navy bg).

    Reuses the hook-card generator (a clean branded title) for the CTA so the
    end-card matches the opening card's typography. The keyword is the headline,
    highlighted orange; the body text is appended on its own. Returns the path to
    a 1080x1920 mp4 that concats cleanly with the main clip (same encode params
    are applied by the re-encoding concat fallback).
    """
    head = (text or keyword or "").strip()
    hl = (keyword or "").strip() or None
    motiongfx.hook_card(head, out_path, brand, duration=CTA_DUR,
                        highlight=hl, bg="navy")
    return os.path.abspath(out_path)


def concat_two(main_mp4: str, cta_mp4: str, out_path: str, tmpdir: str) -> str:
    """Concat the main clip + CTA card. The CTA card is silent (hook_card emits
    no audio), so we normalise both to the same a/v params via the concat filter
    (a stream-copy concat would choke on the missing audio stream).

    Returns ``out_path``.
    """
    # Give the CTA a silent stereo track and re-encode both through the concat
    # filter so the streams line up exactly (1080x1920, 30fps, aac 48k stereo).
    inputs = ["-i", main_mp4, "-f", "lavfi",
              "-t", f"{CTA_DUR}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
              "-i", cta_mp4]
    # streams: 0 = main (v+a), 1 = silent audio for the CTA, 2 = CTA (v only).
    fc = (
        "[0:v]setsar=1[v0];[2:v]setsar=1[v2];"
        "[v0][0:a][v2][1:a]concat=n=2:v=1:a=1[v][a]"
    )
    cmd = [
        FFMPEG, "-y", *inputs,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), "-crf", "18",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        out_path,
    ]
    _run(cmd, what="concat main + CTA card")
    return out_path


# --------------------------------------------------------------------------- #
# Top-level orchestration
# --------------------------------------------------------------------------- #
def compose(args: argparse.Namespace) -> int:
    """Run the full split-layout composition. Returns a process exit code."""
    source = os.path.abspath(args.source)
    if not os.path.isfile(source):
        raise ComposeError(f"--source not found: {source!r}")

    clip_start = float(args.start)
    clip_end = float(args.end)
    if clip_end <= clip_start:
        raise ComposeError(f"--end ({clip_end}) must be greater than --start ({clip_start})")

    out_path = os.path.abspath(args.output)
    out_dir = os.path.dirname(out_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    # A scratch dir for assets next to the output, so artifacts are inspectable.
    assets_dir = os.path.join(out_dir, "compose_assets")
    os.makedirs(assets_dir, exist_ok=True)

    brand = brandkit.load_brandkit(args.brandkit)

    # --- Probe the source so the clip window is sane ----------------------- #
    src_w, src_h, src_dur = probe_video(source)
    clip_end = min(clip_end, src_dur)
    clip_dur = round(clip_end - clip_start, 3)
    if clip_dur <= 0.1:
        raise ComposeError(f"clip window [{clip_start},{clip_end}] is too short ({clip_dur}s)")
    print(f"[compose_best] source {src_w}x{src_h} {src_dur:.2f}s; "
          f"clip {clip_start:.2f}-{clip_end:.2f} ({clip_dur:.2f}s)", file=sys.stderr)

    # --- Step 1: clip-local words ------------------------------------------ #
    transcript = os.path.abspath(args.transcript)
    words = load_clip_words(transcript, clip_start, clip_end)
    print(f"[compose_best] {len(words)} clip-local words "
          f"(Counza spelling fixed)", file=sys.stderr)

    # --- Step 2: storyboard + top-zone assets ------------------------------ #
    beats = plan_storyboard(words, clip_dur, brand, args.storyboard)
    beats = resolve_beat_assets(beats, assets_dir, brand)
    n_assets = sum(1 for b in beats if b.get("asset"))
    print(f"[compose_best] {len(beats)} beats, {n_assets} top-zone assets resolved",
          file=sys.stderr)

    # --- Step 4: name strip (lower-third) ---------------------------------- #
    name_strip_png: Optional[str] = None
    if args.name:
        name_strip_png = motiongfx.lower_third_png(
            args.name, args.credential or "", os.path.join(assets_dir, "name_strip.png"),
            brand,
        )

    # --- Step 5: hook card ------------------------------------------------- #
    hook_card_mp4: Optional[str] = None
    if args.hook_card:
        hook_card_mp4 = motiongfx.hook_card(
            args.hook_card, os.path.join(assets_dir, "hook_card.mp4"), brand,
            duration=HOOK_DUR, highlight=args.hook_card_highlight, bg="navy",
        )

    # --- Step 6: captions (suppress words under the hook card) ------------- #
    if hook_card_mp4:
        cap_words = [w for w in words if w["end"] >= HOOK_SUPPRESS_UNTIL]
    else:
        cap_words = words
    ass_path: Optional[str] = None
    if cap_words:
        ass_path = captions_animated.build_ass(
            cap_words, os.path.join(assets_dir, "captions.ass"), brand,
        )

    # --- Step 3: build + run the single split-layout filtergraph ----------- #
    core_mp4 = os.path.join(assets_dir, "core_split.mp4")
    cmd, plan = build_filtergraph(
        source=source,
        clip_start=clip_start,
        clip_end=clip_end,
        beats=beats,
        name_strip_png=name_strip_png,
        name_strip_at=NAME_STRIP_APPEAR_AT,
        hook_card_mp4=hook_card_mp4,
        ass_path=ass_path,
        out_path=core_mp4,
    )
    _run(cmd, what="split-layout composition (overlays + subtitles + loudnorm)")

    # --- Step 7a: CTA end-card (optional) ---------------------------------- #
    current = core_mp4
    cta_text = args.cta
    cta_keyword = args.keyword
    if cta_text is None and cta_keyword is None:
        # Fall back to the brand kit's CTA block if neither flag was given.
        kit_cta = brand.get("cta") or {}
        cta_text = kit_cta.get("text")
        cta_keyword = kit_cta.get("keyword")
    if cta_text or cta_keyword:
        cta_mp4 = os.path.join(assets_dir, "cta_card.mp4")
        encode_cta_card(brand, cta_keyword or "", cta_text or "", cta_mp4)
        withcta = os.path.join(assets_dir, "with_cta.mp4")
        concat_two(current, cta_mp4, withcta, assets_dir)
        current = withcta
        plan["cta"] = {"keyword": cta_keyword, "text": cta_text, "asset": cta_mp4,
                       "duration": CTA_DUR}

    # --- Step 7b: seamless loop tail (optional) ---------------------------- #
    looped = False
    if not args.no_loop and loop is not None:
        try:
            loop.make_seamless(current, out_path, mode="crossfade", dur=LOOP_DUR)
            looped = True
        except Exception as exc:
            print(f"[compose_best] seamless-loop pass failed, copying clip as-is.\n"
                  f"  ({exc})", file=sys.stderr)
    if not looped:
        # No loop requested / available / it failed -> the current file IS the
        # deliverable. Move it into place (re-encode-free).
        shutil.copyfile(current, out_path)
    plan["seamless_loop"] = {"applied": looped, "dur": LOOP_DUR if looped else 0.0}

    # --- Write the inspectable plan JSON next to the output ---------------- #
    plan_path = os.path.splitext(out_path)[0] + ".plan.json"
    plan["output"] = out_path
    try:
        info = probe_output(out_path)
        plan["rendered"] = info
    except Exception:
        pass
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)

    print(f"[compose_best] DONE -> {out_path}", file=sys.stderr)
    print(f"[compose_best] plan  -> {plan_path}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="compose_best.py",
        description="Best-version SPLIT-LAYOUT composer for Counza vertical shorts "
                    "(top-zone visual story + caption band + footage + name strip).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", required=True, help="source video (required)")
    p.add_argument("--transcript", required=True,
                   help="cached WhisperX transcript JSON for the source (required)")
    p.add_argument("--start", type=float, required=True,
                   help="clip start in SOURCE seconds (required)")
    p.add_argument("--end", type=float, required=True,
                   help="clip end in SOURCE seconds (required)")
    p.add_argument("--storyboard", default=None,
                   help="optional storyboard plan JSON ({'beats':[...]}); "
                        "if omitted, the plan is generated from the transcript")
    p.add_argument("--hook-card", default=None, metavar="TEXT",
                   help="opening full-frame hook-card headline (first 2.5s)")
    p.add_argument("--hook-card-highlight", default=None, metavar="WORD",
                   help="word/phrase in the hook card to colour orange")
    p.add_argument("--name", default=None,
                   help="name for the bottom-edge lower-third (e.g. 'Vansh & Aryan')")
    p.add_argument("--credential", default=None,
                   help="credential line under the name (e.g. 'Co-founders · Counza')")
    p.add_argument("--cta", default=None,
                   help="CTA end-card body text (falls back to the brand kit's CTA)")
    p.add_argument("--keyword", default=None,
                   help="CTA keyword headline, shown orange (e.g. 'PROFILE')")
    p.add_argument("--keyword-highlight", default=None,
                   help=argparse.SUPPRESS)  # reserved; keyword is already the highlight
    p.add_argument("--brandkit", default="counza",
                   help="brand-kit name under engine/brandkits/")
    p.add_argument("--no-loop", action="store_true",
                   help="skip the seamless-loop crossfade tail")
    p.add_argument("-o", "--output", required=True, help="output .mp4 (required)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return compose(args)
    except ComposeError as exc:
        print(f"\n[compose_best] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
