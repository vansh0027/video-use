#!/usr/bin/env python3
"""
engine/templates/product_video.py
=================================

Assemble a **launch / product-demo** video from a list of *beats*.

A beat is one of:

  * a **screenshot** (static image) — gets a Ken-Burns push-in (the engine's
    ``transforms.punch_in_animated``), so a flat PNG/JPG reads as motion;
  * a **screen-recording** (a video clip) — normalized to the target frame
    (``transforms.normalize_vertical``) with an optional gentle punch-in;
  * a **generated motion-graphic card** — one of the ``mg_templates`` cards
    (stat / quote / feature_bullets / cta_endcard / logo_reveal), or a future
    ``video_gen`` b-roll clip.

Each beat carries an optional **caption line** and a **duration**. The assembler
produces a vertical (1080x1920, default) — or 16:9 via ``aspect="16:9"`` — mp4
with hard cuts between beats, word-timed animated captions burned LAST, branded
cards interleaved, and an optional **music bed** ducked under any beat audio.

Why a separate assembler (vs build_short.py)
--------------------------------------------
``build_short.py`` cuts ONE talking-head source into a reel. A launch video is
*assembled from many heterogeneous assets* (screens, recordings, cards) with no
single source transcript — the captions come from the per-beat script lines, not
forced alignment. This module is that assembler; it reuses the same primitives
(``transforms``, ``captions_animated``, ``audio_polish``, ``mg_templates``) and
obeys the same Hard Rules.

Hard-rule alignment
-------------------
* **Per-beat extract -> lossless concat** (Rule 2): every beat is rendered to an
  intermediate mp4 with *identical* encode params, then concatenated with the
  demuxer ``-c copy`` (filter-concat fallback only on param drift).
* **30 ms boundary audio fades** on every beat (Rule 3), via
  ``audio_polish.boundary_afades`` — beats with no audio get a silent track so
  the fade still applies and concat stays uniform.
* **Captions burned LAST** (Rule 1): the concatenated body is the base; the
  music bed (if any) is mixed and then the .ass is burned in the final pass,
  after everything else. loudnorm runs ONCE at the end (Rule, mirrors
  build_short).
* Card beats animate from their own frame 0, so they need no ``setpts`` here
  (they are full beats, not windowed overlays).

Workstream dependencies (late-imported, optional)
--------------------------------------------------
* ``engine.effects`` (Workstream 1) — sfx cues + transitions. Imported lazily
  inside ``try/except``; absent => no sfx, hard cuts only.
* ``engine.video_gen`` (Workstream 2) — generated b-roll. Imported lazily; absent
  => a ``card``/``broll`` beat that asks for generated footage degrades to a
  solid brand-colour placeholder beat so the build still completes.

The module runs **standalone** with neither present.

Public API
----------
    build_product_video(spec, out_path, brand, *, aspect="9:16",
                        music=None, fps=30, work_dir=None) -> dict

    spec   list of beat dicts (see ``Beat spec`` below) OR a dict with a
           ``"beats"`` array (and optional ``"music"``).
    Returns a small report dict: {"output", "width", "height", "duration",
           "beats", "captions", "music", "degraded": [...]}.

Beat spec
---------
    {
      "kind":      "screenshot" | "screen_recording" | "card",
      # --- screenshot / screen_recording ---
      "path":      "<image-or-video path>",     # required for those kinds
      "zoom":      1.12,                          # Ken-Burns / punch strength
      # --- card ---
      "card":      "stat_card" | "quote_card" | "feature_bullets" |
                   "cta_endcard" | "logo_reveal",
      "args":      { ... },   # kwargs forwarded to the card builder
      "bg":        "bone"|"navy"|...,            # card background
      # --- common ---
      "caption":   "spoken line for this beat",  # optional, burned as caption
      "duration":  3.0                            # seconds (cards default 2.6)
    }

Stdlib + PIL + the project ffmpeg. Imports — never modifies — sibling engine
modules.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Sibling-engine imports. engine/ (this file's grandparent dir) is the import
# root for the package-less engine modules, mirroring build_short.py.
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINE_DIR = os.path.dirname(_THIS_DIR)
for _p in (_ENGINE_DIR, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import transforms  # noqa: E402
import audio_polish  # noqa: E402
import captions_animated  # noqa: E402
import mg_templates  # noqa: E402  (sibling, in engine/templates/)

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

_DEFAULT_FPS = 30
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


class ProductVideoError(RuntimeError):
    """Raised with a human-readable message for any unrecoverable failure."""


# --------------------------------------------------------------------------- #
# Optional Workstream dependencies — late, defensive import.
# --------------------------------------------------------------------------- #
def _try_import_effects():
    """Return the engine.effects package or None (Workstream 1, optional)."""
    try:
        import effects  # type: ignore  # engine/effects/__init__.py
        return effects
    except Exception:
        return None


def _try_import_video_gen():
    """Return the engine.video_gen module or None (Workstream 2, optional)."""
    for name in ("video_gen",):
        try:
            return __import__(name)
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# Subprocess + probe helpers (self-contained; mirror build_short style)
# --------------------------------------------------------------------------- #
def _run(cmd: List[str], *, what: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ProductVideoError(
            f"{what}: executable not found ({cmd[0]!r}). Is it installed and on PATH?"
        ) from exc
    if proc.returncode != 0:
        tail = "\n".join(
            "    " + ln for ln in (proc.stderr or proc.stdout or "").rstrip().splitlines()[-25:]
        ) or "    (no output)"
        raise ProductVideoError(
            f"{what} failed (exit {proc.returncode}).\n"
            f"  cmd: {' '.join(shlex.quote(c) for c in cmd)}\n"
            f"  stderr (last lines):\n{tail}"
        )
    return proc


def _probe_dims(path: str) -> Tuple[int, int]:
    proc = _run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", path],
        what=f"ffprobe dims {os.path.basename(path)}",
    )
    data = json.loads(proc.stdout)
    s = data["streams"][0]
    return int(s["width"]), int(s["height"])


def probe_output(path: str) -> Dict[str, Any]:
    """Return {width,height,duration,has_audio,codec} for a finished file."""
    proc = _run(
        [FFPROBE, "-v", "error",
         "-show_entries", "stream=index,codec_type,codec_name,width,height:format=duration",
         "-of", "json", path],
        what=f"ffprobe verify {os.path.basename(path)}",
    )
    data = json.loads(proc.stdout)
    w = h = None
    codec = "?"
    has_audio = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and w is None:
            w, h, codec = s.get("width"), s.get("height"), s.get("codec_name", "?")
        if s.get("codec_type") == "audio":
            has_audio = True
    dur = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    return {"width": w, "height": h, "duration": dur, "has_audio": has_audio, "codec": codec}


def _program_is_silent(path: str, threshold_db: float = -70.0) -> bool:
    """True if the file's audio is effectively silence (mean below threshold).

    ``loudnorm`` on pure digital silence emits NaN/Inf and the AAC encoder then
    rejects the frame, so the final pass must *skip* loudnorm when the program
    audio carries no signal (e.g. a launch video assembled entirely from silent
    screenshots / cards). We measure with ``volumedetect``.
    """
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", path, "-map", "0:a:0?",
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return False
    text = (proc.stderr or "") + (proc.stdout or "")
    mean = None
    for ln in text.splitlines():
        if "mean_volume:" in ln:
            try:
                mean = float(ln.split("mean_volume:")[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
    if mean is None:
        return False
    return mean <= threshold_db


def _has_audio(path: str) -> bool:
    proc = _run(
        [FFPROBE, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "json", path],
        what=f"ffprobe audio {os.path.basename(path)}",
    )
    return bool(json.loads(proc.stdout).get("streams"))


# --------------------------------------------------------------------------- #
# Target frame / encode params
# --------------------------------------------------------------------------- #
def _target_dims(aspect: str) -> Tuple[int, int]:
    a = str(aspect or "9:16").strip().lower().replace("x", ":")
    if a in ("9:16", "vertical", "portrait", "reel", "short"):
        return 1080, 1920
    if a in ("16:9", "horizontal", "landscape", "wide", "youtube"):
        return 1920, 1080
    if a in ("1:1", "square"):
        return 1080, 1080
    if a in ("4:5", "feed"):
        return 1080, 1350
    if ":" in a:
        try:
            wr, hr = a.split(":")
            wr, hr = float(wr), float(hr)
            if wr >= hr:
                w = 1920
                h = int(round(w * hr / wr / 2) * 2)
            else:
                h = 1920
                w = int(round(h * wr / hr / 2) * 2)
            return w, h
        except Exception:
            pass
    return 1080, 1920


def _common_video_out(cmd_tail: List[str], fps: int) -> List[str]:
    return cmd_tail + [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
    ]


def _kind_of(beat: Dict[str, Any]) -> str:
    """Classify a beat into 'card' | 'screenshot' | 'screen_recording'."""
    k = str(beat.get("kind") or "").strip().lower()
    if k in ("card", "mg", "graphic"):
        return "card"
    if beat.get("card"):
        return "card"
    if k in ("screenshot", "image", "still", "shot"):
        return "screenshot"
    if k in ("screen_recording", "recording", "video", "clip", "screencast"):
        return "screen_recording"
    # infer from path extension
    p = str(beat.get("path") or "")
    ext = os.path.splitext(p)[1].lower()
    if ext in _IMAGE_EXTS:
        return "screenshot"
    if ext in _VIDEO_EXTS:
        return "screen_recording"
    return "screenshot"


# --------------------------------------------------------------------------- #
# Beat renderers — each writes one intermediate mp4 with the shared params.
# --------------------------------------------------------------------------- #
def _render_screenshot_beat(
    beat: Dict[str, Any], out_path: str, tw: int, th: int, fps: int, dur: float,
) -> None:
    """Static image -> Ken-Burns push-in beat (with a silent audio track)."""
    path = beat.get("path")
    if not path or not os.path.isfile(path):
        raise ProductVideoError(f"screenshot beat path not found: {path!r}")
    src_w, src_h = _probe_dims(path)  # ffprobe reads image dims too
    try:
        zoom = float(beat.get("zoom", 1.14))
    except (TypeError, ValueError):
        zoom = 1.14
    if zoom < 1.0:
        zoom = 1.0

    # normalize to the frame, then a visible Ken-Burns ramp across the beat.
    vf = transforms.compose([
        transforms.normalize_vertical(src_w, src_h, tw, th),
        transforms.punch_in_animated(zoom=max(1.0001, zoom), duration_s=dur,
                                     fps=fps, target_w=tw, target_h=th)
        if zoom > 1.0 else transforms.punch_in(1.0, tw, th),
    ])
    cmd = [
        FFMPEG, "-y",
        "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", path,
        "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", vf,
        "-af", audio_polish.boundary_afades(dur),
    ]
    cmd = _common_video_out(cmd, fps)
    cmd += ["-shortest", out_path]
    _run(cmd, what=f"render screenshot beat {os.path.basename(str(path))}")


def _render_recording_beat(
    beat: Dict[str, Any], out_path: str, tw: int, th: int, fps: int, dur: float,
) -> None:
    """Screen-recording clip -> normalized beat (optional gentle punch-in)."""
    path = beat.get("path")
    if not path or not os.path.isfile(path):
        raise ProductVideoError(f"screen_recording beat path not found: {path!r}")
    src_w, src_h = _probe_dims(path)
    try:
        zoom = float(beat.get("zoom", 1.0))
    except (TypeError, ValueError):
        zoom = 1.0

    vf = transforms.compose([
        transforms.normalize_vertical(src_w, src_h, tw, th),
        transforms.punch_in(zoom, tw, th) if zoom > 1.0 else "",
    ])
    # Trim to the beat duration; loop is not needed for a real clip.
    src_has_audio = _has_audio(path)
    cmd = [FFMPEG, "-y", "-t", f"{dur:.3f}", "-i", path]
    if not src_has_audio:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    cmd += ["-vf", vf, "-af", audio_polish.boundary_afades(dur)]
    if not src_has_audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    cmd = _common_video_out(cmd, fps)
    cmd += ["-shortest", out_path]
    _run(cmd, what=f"render recording beat {os.path.basename(str(path))}")


def _render_card_beat(
    beat: Dict[str, Any], out_path: str, brand: Dict[str, Any],
    tw: int, th: int, fps: int, dur: float, *, degraded: List[str],
    video_gen,
) -> None:
    """Generated motion-graphic card -> beat mp4 (then conform to frame + audio)."""
    card_name = str(beat.get("card") or "quote_card").strip()
    args = dict(beat.get("args") or {})
    if "bg" in beat and "bg" not in args:
        args["bg"] = beat["bg"]
    args.setdefault("duration", dur)
    args["fps"] = fps

    tmp_card = out_path + ".card.mp4"
    builder = mg_templates.CARD_BUILDERS.get(card_name)

    made = False
    if builder is not None:
        # Pull the positional arg(s) each builder wants out of the spec/args.
        try:
            if card_name == "stat_card":
                number = args.pop("number", beat.get("number", beat.get("caption", "")))
                label = args.pop("label", beat.get("label", ""))
                builder(str(number), str(label), tmp_card, brand, **args)
            elif card_name == "quote_card":
                text = args.pop("text", beat.get("text", beat.get("caption", "")))
                builder(str(text), tmp_card, brand, **args)
            elif card_name == "feature_bullets":
                bullets = args.pop("bullets", beat.get("bullets", []))
                builder(list(bullets), tmp_card, brand, **args)
            elif card_name == "cta_endcard":
                text = args.pop("text", beat.get("text", beat.get("caption", "")))
                builder(str(text), tmp_card, brand, **args)
            elif card_name == "logo_reveal":
                wordmark = args.pop("wordmark", beat.get("wordmark", beat.get("text", "")))
                builder(str(wordmark), tmp_card, brand, **args)
            made = True
        except Exception as exc:
            degraded.append(f"card '{card_name}' failed ({exc}); using placeholder")

    if not made:
        # Degraded path: a solid brand-colour placeholder beat. If video_gen is
        # present a future build could substitute generated b-roll here.
        gen_fn = (getattr(video_gen, "gen_broll_clip", None)
                  or getattr(video_gen, "gen_video", None)) if video_gen is not None else None
        if gen_fn is not None and beat.get("prompt"):
            try:
                gen_dir = os.path.dirname(tmp_card) or "."
                # gen_broll_clip(concept, out_dir, ...) / gen_video(prompt=..., out_dir=...)
                if getattr(gen_fn, "__name__", "") == "gen_broll_clip":
                    gen = gen_fn(str(beat["prompt"]), gen_dir,
                                 duration=dur, width=tw, height=th)
                else:
                    gen = gen_fn(prompt=str(beat["prompt"]), out_dir=gen_dir,
                                 duration=dur, width=tw, height=th)
                if gen and os.path.isfile(gen):
                    tmp_card = gen
                    made = True
            except Exception as exc:
                degraded.append(f"video_gen b-roll failed ({exc}); using placeholder")
        if not made:
            _render_placeholder_beat(beat, tmp_card, brand, tw, th, fps, dur, label_only=True)

    # Conform the card (1080x1920 opaque, no audio) into the target frame with a
    # silent audio track + boundary fades so it concats with every other beat.
    src_w, src_h = _probe_dims(tmp_card)
    vf = transforms.compose([transforms.normalize_vertical(src_w, src_h, tw, th)])
    cmd = [
        FFMPEG, "-y",
        "-i", tmp_card,
        "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", vf, "-af", audio_polish.boundary_afades(dur),
        "-map", "0:v:0", "-map", "1:a:0",
    ]
    cmd = _common_video_out(cmd, fps)
    cmd += ["-shortest", out_path]
    _run(cmd, what=f"conform card beat '{card_name}'")
    try:
        os.remove(tmp_card)
    except OSError:
        pass


def _render_placeholder_beat(
    beat: Dict[str, Any], out_path: str, brand: Dict[str, Any],
    tw: int, th: int, fps: int, dur: float, *, label_only: bool = False,
) -> None:
    """Solid brand-colour beat (degraded fallback / synthetic screenshot).

    Used both as the graceful-degradation target when a card/b-roll can't be
    produced, and as a synthetic-asset generator for tests. Draws an optional
    centered label via PIL so we don't depend on drawtext/fontconfig.
    """
    from PIL import Image, ImageDraw  # local import keeps module import light

    colors = (brand or {}).get("colors", {})
    bg_key = str(beat.get("bg") or "navy_deep")
    bg_hex = colors.get(bg_key) or colors.get("navy_deep") or "#13294b"
    rgb = mg_templates.mg._hex_to_rgb(bg_hex)

    label = str(beat.get("caption") or beat.get("label") or "").strip()
    frame = Image.new("RGB", (tw, th), rgb)
    if label:
        draw = ImageDraw.Draw(frame)
        font = mg_templates.mg._sans_font(max(40, tw // 18), weight="bold")
        # naive wrap
        lines = mg_templates._sans_wrap(label, font, tw * 0.8)
        asc, desc = font.getmetrics()
        lh = int((asc + desc) * 1.2)
        top = (th - lh * len(lines)) / 2
        accent = mg_templates.mg._brand_rgb(brand, "bone")
        for k, ln in enumerate(lines):
            w = font.getlength(ln)
            draw.text(((tw - w) / 2, top + k * lh), ln, font=font, fill=accent)

    png = out_path + ".png"
    frame.save(png)
    cmd = [
        FFMPEG, "-y",
        "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", png,
        "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", "setsar=1", "-af", audio_polish.boundary_afades(dur),
    ]
    cmd = _common_video_out(cmd, fps)
    cmd += ["-shortest", out_path]
    _run(cmd, what="render placeholder beat")
    try:
        os.remove(png)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Concat (lossless demuxer copy, filter-concat fallback) — mirrors build_short.
# --------------------------------------------------------------------------- #
def _concat(beat_paths: List[str], out_path: str, tmpdir: str, fps: int) -> None:
    list_file = os.path.join(tmpdir, "concat.txt")
    with open(list_file, "w", encoding="utf-8") as fh:
        for p in beat_paths:
            safe = p.replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")
    copy_cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                "-c", "copy", out_path]
    try:
        _run(copy_cmd, what="concat (stream copy)")
        return
    except ProductVideoError:
        pass
    inputs: List[str] = []
    for p in beat_paths:
        inputs += ["-i", p]
    n = len(beat_paths)
    streams = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    fc = f"{streams}concat=n={n}:v=1:a=1[v][a]"
    cmd = [FFMPEG, "-y", *inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    cmd = _common_video_out(cmd, fps)
    cmd += [out_path]
    _run(cmd, what="concat (filter re-encode)")


# --------------------------------------------------------------------------- #
# Captions — one caption line per beat, mapped to the OUTPUT timeline.
# --------------------------------------------------------------------------- #
def _build_caption_words(
    beats: List[Dict[str, Any]], durations: List[float],
) -> List[Dict[str, Any]]:
    """Turn each beat's caption line into output-timeline word events.

    A beat's caption is split into words and time-spread evenly across that
    beat's span on the concatenated output timeline (output_time = beat offset +
    within-beat share). This satisfies the caption builder's word-event contract
    without a forced-alignment transcript — the launch-video captions are
    script-driven, not ASR-driven.
    """
    words: List[Dict[str, Any]] = []
    offset = 0.0
    for beat, dur in zip(beats, durations):
        cap = str(beat.get("caption") or "").strip()
        if cap:
            toks = cap.split()
            if toks:
                # leave a small margin at the head/tail of the beat
                pad = min(0.25, dur * 0.12)
                span = max(0.01, dur - 2 * pad)
                per = span / len(toks)
                for k, tok in enumerate(toks):
                    s = offset + pad + k * per
                    e = s + per * 0.96
                    words.append({"word": tok, "start": round(s, 3), "end": round(e, 3)})
        offset += dur
    return words


# --------------------------------------------------------------------------- #
# Final pass — music bed (ducked) + captions LAST + loudnorm once.
# --------------------------------------------------------------------------- #
def _final_pass(
    body_path: str, ass_path: Optional[str], music_path: Optional[str],
    out_path: str, fps: int,
) -> None:
    """Mix optional music, burn captions LAST, loudnorm once (Hard Rules)."""
    sub = (f"subtitles=filename={_escape_subs_path(ass_path)}" if ass_path else None)
    # loudnorm on pure silence emits NaN and breaks the AAC encode, so skip it
    # when the program audio carries no signal (all-silent assembly). When there
    # IS signal, loudnorm runs and we resample to a clean 48 kHz stream after it.
    silent = _program_is_silent(body_path)
    if silent:
        af = "aresample=48000"
    else:
        af = audio_polish.loudnorm_af() + ",aresample=48000:async=1:first_pts=0"

    if not music_path:
        cmd = [FFMPEG, "-y", "-i", body_path]
        if sub:
            cmd += ["-vf", sub]
        cmd += [
            "-af", af,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
            out_path,
        ]
        _run(cmd, what="final pass (captions + loudnorm)")
        return

    # Music bed: input 0 = body (voice/program), input 1 = music. music_duck
    # returns the audio filtergraph body ending in [aout]; we add the video
    # subtitle node in the same filter_complex so captions still burn LAST.
    audio_graph = audio_polish.music_duck(music_path)
    fc = [audio_graph]
    if sub:
        fc.append(f"[0:v]{sub}[vout]")
        vmap = "[vout]"
    else:
        vmap = "0:v"
    cmd = [
        FFMPEG, "-y", "-i", body_path, "-stream_loop", "-1", "-i", music_path,
        "-filter_complex", ";".join(fc),
        "-map", vmap, "-map", "[aout]",
        "-t", f"{probe_output(body_path)['duration']:.3f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), "-crf", "18",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
        out_path,
    ]
    _run(cmd, what="final pass (music + captions + loudnorm)")


def _escape_subs_path(path: str) -> str:
    return (path.replace("\\", "\\\\").replace(":", r"\:")
            .replace("'", r"\'").replace(",", r"\,"))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def build_product_video(
    spec: Any,
    out_path: str,
    brand: Dict[str, Any],
    *,
    aspect: str = "9:16",
    music: Optional[str] = None,
    fps: int = _DEFAULT_FPS,
    work_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble a launch / product video from a list of beats. See module docstring.

    Args:
        spec:     A list of beat dicts, or a dict ``{"beats": [...], "music": ...}``.
        out_path: Where to write the final mp4.
        brand:    Brand-kit dict (from brandkit.load_brandkit).
        aspect:   "9:16" (default) | "16:9" | "1:1" | "4:5" | "<w>:<h>".
        music:    Optional path to a music bed (ducked under beat audio). A
                  ``"music"`` key on a dict-form spec is used if this is None.
        fps:      Frame rate.
        work_dir: Scratch dir for intermediates (a temp dir is used if None).

    Returns:
        A report dict: {"output","width","height","duration","beats","captions",
        "music","degraded"}.

    Raises:
        ProductVideoError / ValueError on unrecoverable problems (no beats, a
        required asset missing, an ffmpeg failure).
    """
    if isinstance(spec, dict):
        beats = list(spec.get("beats") or [])
        if music is None:
            music = spec.get("music")
    else:
        beats = list(spec or [])
    if not beats:
        raise ValueError("build_product_video: spec has no beats")

    fps = int(fps)
    if fps <= 0:
        raise ValueError(f"fps must be > 0, got {fps!r}")
    tw, th = _target_dims(aspect)

    # Optional workstreams — probe once, record availability.
    effects = _try_import_effects()
    video_gen = _try_import_video_gen()
    degraded: List[str] = []
    if effects is None:
        degraded.append("engine.effects absent: no sfx/transitions (hard cuts only)")
    if video_gen is None:
        degraded.append("engine.video_gen absent: generated b-roll -> placeholder")

    if music and not os.path.isfile(music):
        degraded.append(f"music bed not found ({music}); continuing without it")
        music = None

    own_tmp = work_dir is None
    tmpdir = work_dir or tempfile.mkdtemp(prefix="product_video_")
    os.makedirs(tmpdir, exist_ok=True)

    try:
        beat_paths: List[str] = []
        durations: List[float] = []
        min_dur = 2 * audio_polish.FADE_DUR + 0.04  # boundary fades need > 0.06s

        for i, beat in enumerate(beats):
            if not isinstance(beat, dict):
                raise ValueError(f"beat #{i} is not an object: {beat!r}")
            kind = _kind_of(beat)
            try:
                dur = float(beat.get("duration", 2.6))
            except (TypeError, ValueError):
                dur = 2.6
            if dur < min_dur:
                dur = min_dur  # keep boundary fades legal; never silently drop a beat
            durations.append(dur)

            bp = os.path.join(tmpdir, f"beat_{i:03d}.mp4")
            if kind == "screenshot":
                _render_screenshot_beat(beat, bp, tw, th, fps, dur)
            elif kind == "screen_recording":
                _render_recording_beat(beat, bp, tw, th, fps, dur)
            else:  # card
                _render_card_beat(beat, bp, brand, tw, th, fps, dur,
                                  degraded=degraded, video_gen=video_gen)
            beat_paths.append(bp)

        # Concat beats (lossless) -> body.
        body = os.path.join(tmpdir, "body.mp4")
        _concat(beat_paths, body, tmpdir, fps)

        # Captions from the per-beat script lines (output-timeline word events).
        words = _build_caption_words(beats, durations)
        ass_path = None
        if words:
            ass_path = os.path.join(tmpdir, "captions.ass")
            captions_animated.build_ass(words, ass_path, brand)

        # Final pass: music (ducked) + captions LAST + loudnorm once.
        out_abs = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
        _final_pass(body, ass_path, music, out_abs, fps)

        info = probe_output(out_abs)
        return {
            "output": out_abs,
            "width": info["width"],
            "height": info["height"],
            "duration": info["duration"],
            "beats": len(beat_paths),
            "captions": len(words),
            "music": bool(music),
            "degraded": degraded,
        }
    finally:
        if own_tmp:
            shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI: build from a spec JSON file.
# --------------------------------------------------------------------------- #
def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Assemble a launch/product video from a beats spec JSON.")
    ap.add_argument("spec", help="Path to a JSON spec (list of beats, or {beats,music}).")
    ap.add_argument("-o", "--output", required=True, help="Output mp4 path.")
    ap.add_argument("--brandkit", default="counza", help="Brand-kit name (default counza).")
    ap.add_argument("--aspect", default="9:16", help="9:16 | 16:9 | 1:1 | 4:5 | w:h.")
    ap.add_argument("--music", default=None, help="Optional music-bed path.")
    ap.add_argument("--fps", type=int, default=_DEFAULT_FPS)
    args = ap.parse_args(argv)

    try:
        import brandkit
        kit = brandkit.load_brandkit(args.brandkit)
    except Exception:
        kit = {"colors": dict(mg_templates.mg._FALLBACK_COLORS), "name": args.brandkit}

    with open(args.spec, "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    report = build_product_video(spec, args.output, kit, aspect=args.aspect,
                                 music=args.music, fps=args.fps)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
