#!/usr/bin/env python3
"""
engine/loop.py
==============

Make a finished vertical short **loop seamlessly** — the end flows back into the
start with no visible/audible jump — so it reads cleanly when a platform
auto-loops it (Reels / TikTok / Shorts) or when it is exported as a looping
clip/GIF.

This runs as a standalone re-encode on an already-rendered MP4: it never
re-cuts, never re-grades, and never re-burns subtitles (they are already in the
pixels). It only blends the tail back toward the head.

Modes
-----
* ``crossfade`` (default) — overlap the last ``dur`` seconds with the first
  ``dur`` seconds via ``xfade`` (+ ``acrossfade`` for audio). The source is fed
  to ffmpeg twice; the second copy's *head* is dissolved under the first copy's
  *tail*, so the output's final frame eases into what frame 0 will be on replay.
  Output length is ``L - dur`` (the overlap is consumed once).

* ``freeze_match`` — for clips whose end is already near-static (a held pose, a
  settled graphic): hold the final frame briefly and blend it toward the head so
  the wrap is smoothed while preserving the bulk of the motion. Implemented as a
  short ``tpad`` freeze on the tail followed by the same xfade-into-head blend,
  with a *tighter* blend window than ``crossfade``.

Robustness (per the spec)
-------------------------
``xfade``'s ``offset`` must sit strictly inside the clip and the overlap must
fit, which breaks on very short clips. :func:`make_seamless` therefore **clamps**
the blend to ``min(dur, 0.5 * length)``; if the structured filtergraph still
fails it falls back to a minimal tail-to-head ``xfade`` at that clamped length,
and if the clip is too short to loop at all it simply re-encodes to spec so the
output is still a valid, standard clip.

Encode standard (kept in lock-step with build_short.py / transforms.py)
----------------------------------------------------------------------
1080x1920, **libx264 / yuv420p / 30 fps**, **AAC 48 kHz stereo**, **+faststart**.
``loudnorm`` is intentionally *not* re-applied — the input already passed the
final loudness stage; ``acrossfade`` is equal-power so perceived loudness holds.

CLI / self-test
---------------
    python engine/loop.py --self-test                 # build 6s clip + loop it
    python engine/loop.py IN.mp4 -o OUT.mp4            # crossfade, 0.5s
    python engine/loop.py IN.mp4 -o OUT.mp4 --mode freeze_match --dur 0.4

Library
-------
    from engine.loop import make_seamless, loop_score
    make_seamless("in.mp4", "out.mp4", mode="crossfade", dur=0.5)
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable, List, Optional, Sequence

__all__ = ["make_seamless", "loop_score", "LoopError", "TARGET_W", "TARGET_H", "FPS"]

# --------------------------------------------------------------------------- #
# Engine encode standard
# --------------------------------------------------------------------------- #
TARGET_W = 1080
TARGET_H = 1920
FPS = 30

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

DEFAULT_DUR = 0.5
VALID_MODES = ("crossfade", "freeze_match")

# Below this we don't try to loop — the overlap math isn't meaningful.
_MIN_LOOPABLE_S = 0.30
# Never consume more than half the clip in the blend.
_MAX_FRAC = 0.5
# Smallest usable blend: a couple of frames.
_MIN_BLEND_S = 2.0 / FPS


class LoopError(RuntimeError):
    """Raised with a human-readable message (including the ffmpeg stderr tail)."""


# --------------------------------------------------------------------------- #
# subprocess + probe helpers (self-contained; mirrors build_short.py style)
# --------------------------------------------------------------------------- #
def _fmt_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(c)) for c in cmd)


def _tail(text: Optional[str], n: int) -> str:
    if not text:
        return ""
    lines = text.rstrip().splitlines()
    return "\n".join("    " + ln for ln in lines[-n:])


def _run(cmd: Sequence[str], *, what: str) -> subprocess.CompletedProcess:
    """Run an ffmpeg/ffprobe command, raising :class:`LoopError` on failure."""
    cmd = [str(c) for c in cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise LoopError(
            f"{what}: executable not found ({cmd[0]!r}). Is it on PATH?\n"
            f"  cmd: {_fmt_cmd(cmd)}"
        ) from exc
    if proc.returncode != 0:
        tail = _tail(proc.stderr, 30) or _tail(proc.stdout, 30) or "(no output)"
        raise LoopError(
            f"{what} failed (exit {proc.returncode}).\n"
            f"  cmd: {_fmt_cmd(cmd)}\n"
            f"  stderr (last lines):\n{tail}"
        )
    return proc


def _probe(path: str) -> dict:
    """Return {duration, has_audio} for an existing media file."""
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "stream=codec_type:format=duration",
        "-of", "json", path,
    ]
    proc = _run(cmd, what=f"ffprobe {os.path.basename(path)}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LoopError(f"could not parse ffprobe output for {path!r}: {exc}") from exc
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    dur = float(data.get("format", {}).get("duration", 0.0) or 0.0)
    if dur <= 0:
        raise LoopError(f"{path!r} reports a nonsensical duration ({dur}s)")
    return {"duration": dur, "has_audio": has_audio}


# --------------------------------------------------------------------------- #
# encode-tail + normalize helpers
# --------------------------------------------------------------------------- #
def _video_out_tail() -> List[str]:
    """Shared output encode params (no loudnorm — input is already normalized)."""
    return [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
    ]


def _normalize_vf() -> str:
    """Fit-and-pad any input to a centered 1080x1920 @ 30fps frame, SAR 1.

    A no-op for clips already at spec, but it guarantees the geometry/SAR/fps the
    rest of the engine assumes and that ``xfade`` needs (both xfade inputs must
    share size + SAR + framerate).
    """
    return (
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
        f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={FPS},format=yuv420p"
    )


# --------------------------------------------------------------------------- #
# loop_score
# --------------------------------------------------------------------------- #
_STOPISH = {
    "the", "a", "an", "and", "or", "but", "so", "to", "of", "in", "on",
    "it", "is", "i", "you", "we", "that", "this", "for", "with", "uh", "um",
}


def loop_score(words: Any) -> bool:
    """Best-effort heuristic: does the *script* already loop (end echoes start)?

    A clip whose narration ends on the same phrase it opened with loops
    naturally — the words hide the seam — so a caller can use this to decide
    whether a visual crossfade is even worth applying.

    ``words`` is flexible: a plain string, a list of token strings, or a list of
    WhisperX-style word dicts (``{"word": ...}`` or ``{"text": ...}``). Anything
    else is coerced with ``str``.

    Heuristic: normalize to a lowercase, punctuation-stripped token list, then
    check whether the first ``k`` tokens reappear as the last ``k`` tokens for
    ``k`` from 3 down to 1 (a longer echo is preferred; a single-word bookend
    still counts, but only if it is a *contentful* word, not a stop/filler word
    that trivially repeats). Returns ``True`` on the first match; scripts with
    fewer than 2 tokens return ``False``.

    This is a hint, not a guarantee.
    """
    toks = _tokenize_words(words)
    n = len(toks)
    if n < 2:
        return False

    max_k = min(3, n // 2)
    for k in range(max_k, 0, -1):
        head = toks[:k]
        tail = toks[-k:]
        if head == tail:
            if k == 1 and head[0] in _STOPISH:
                continue
            return True
    return False


def _tokenize_words(words: Any) -> List[str]:
    """Coerce assorted 'words' inputs into a clean lowercase token list."""
    if words is None:
        return []
    if isinstance(words, str):
        raw: List[str] = words.split()
    elif isinstance(words, dict):
        raw = [str(words.get("word") or words.get("text") or "")]
    elif isinstance(words, Iterable):
        raw = []
        for w in words:
            if isinstance(w, str):
                raw.append(w)
            elif isinstance(w, dict):
                raw.append(str(w.get("word") or w.get("text") or ""))
            else:
                raw.append(str(w))
    else:
        raw = [str(words)]

    out: List[str] = []
    for tok in raw:
        cleaned = tok.strip().strip(".,!?;:\"'()[]{}—–-…").lower()
        if cleaned:
            out.append(cleaned)
    return out


# --------------------------------------------------------------------------- #
# blend builders
# --------------------------------------------------------------------------- #
def _crossfade(src: str, out_path: str, *, blend: float, total: float,
               has_audio: bool) -> None:
    """Dissolve the clip's real tail into its real head — output length ``total``.

    The seamless-loop recipe: stream A is the *whole* normalized clip (length
    ``total``); stream B is just its *head*, the first ``blend`` seconds. Then
    ``xfade=offset=total-blend:duration=blend`` makes the output length
    ``total + blend - blend = total`` whose trailing ``blend`` seconds dissolve
    the tail toward frame 0, so wrapping ``total -> 0`` is invisible. Audio is
    handled the same way with ``acrossfade`` (full A tail into head B), which is
    equal-power and also resolves to length ``total``.

    Both xfade inputs are normalized to identical size/SAR/fps (xfade requires
    it). The source is decoded twice (cheap; one is trimmed to ``blend`` s).
    """
    offset = max(0.0, total - blend)
    b = f"{blend:.4f}"
    off = f"{offset:.4f}"
    nv = _normalize_vf()

    if has_audio:
        fc = (
            # A = full clip; B = head (first `blend`s), reset to a 0 PTS.
            f"[0:v]{nv}[v0];"
            f"[1:v]{nv},trim=0:{b},setpts=PTS-STARTPTS[v1];"
            f"[v0][v1]xfade=transition=fade:duration={b}:offset={off}[v];"
            f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a0];"
            f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"atrim=0:{b},asetpts=PTS-STARTPTS[a1];"
            f"[a0][a1]acrossfade=d={b}:c1=tri:c2=tri[a]"
        )
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = (
            f"[0:v]{nv}[v0];"
            f"[1:v]{nv},trim=0:{b},setpts=PTS-STARTPTS[v1];"
            f"[v0][v1]xfade=transition=fade:duration={b}:offset={off}[v]"
        )
        maps = ["-map", "[v]"]

    cmd = [FFMPEG, "-y", "-i", src, "-i", src, "-filter_complex", fc, *maps]
    cmd += _video_out_tail() + [out_path]
    _run(cmd, what=f"loop crossfade (blend={b}s)")


def _freeze_match(src: str, out_path: str, *, blend: float, total: float,
                  has_audio: bool) -> None:
    """Hold the last frame briefly, then blend it toward the head.

    ``tpad=stop_mode=clone`` clones the final frame for ``blend`` seconds so the
    very end is static; we then xfade that frozen tail into the (normalized) head
    of a second copy. This keeps almost all the original motion and only eases
    the seam — ideal when the ending is already near-still.
    """
    b = f"{blend:.4f}"
    # Stream A: normalize, then clone the last frame for `blend` seconds, so its
    # length becomes total + blend. Stream B: just the head (first `blend`s).
    # The dissolve starts at offset = total (where the freeze begins) and runs
    # for `blend`s, so output length = (total + blend) + blend - blend
    # = total + blend (a brief, intentional hold added at the seam).
    off = f"{float(total):.4f}"
    nv = _normalize_vf()

    if has_audio:
        fc = (
            f"[0:v]{nv},tpad=stop_mode=clone:stop_duration={b}[v0];"
            f"[1:v]{nv},trim=0:{b},setpts=PTS-STARTPTS[v1];"
            f"[v0][v1]xfade=transition=fade:duration={b}:offset={off}[v];"
            # Pad the first copy's audio with silence under the freeze, then
            # crossfade into the head of the second copy so there is no click.
            f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"apad=pad_dur={b}[a0];"
            f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"atrim=0:{b},asetpts=PTS-STARTPTS[a1];"
            f"[a0][a1]acrossfade=d={b}:c1=tri:c2=tri[a]"
        )
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = (
            f"[0:v]{nv},tpad=stop_mode=clone:stop_duration={b}[v0];"
            f"[1:v]{nv},trim=0:{b},setpts=PTS-STARTPTS[v1];"
            f"[v0][v1]xfade=transition=fade:duration={b}:offset={off}[v]"
        )
        maps = ["-map", "[v]"]

    cmd = [FFMPEG, "-y", "-i", src, "-i", src, "-filter_complex", fc, *maps]
    cmd += _video_out_tail() + [out_path]
    _run(cmd, what=f"loop freeze_match (blend={b}s)")


def _passthrough(src: str, out_path: str, *, has_audio: bool) -> None:
    """Re-encode to spec without any loop processing (very short clips)."""
    if has_audio:
        cmd = [FFMPEG, "-y", "-i", src, "-vf", _normalize_vf()]
        cmd += _video_out_tail() + [out_path]
    else:
        cmd = [
            FFMPEG, "-y",
            "-i", src,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf", _normalize_vf(),
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
        ]
        cmd += _video_out_tail() + [out_path]
    _run(cmd, what="loop passthrough (re-encode to spec)")


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def make_seamless(
    in_path: str,
    out_path: str,
    mode: str = "crossfade",
    dur: float = DEFAULT_DUR,
) -> str:
    """Render ``in_path`` to ``out_path`` so it loops seamlessly; return ``out_path``.

    Args:
        in_path: source MP4 (already graded/captioned).
        out_path: destination MP4 (overwritten).
        mode: ``"crossfade"`` (dissolve tail into head) or ``"freeze_match"``
            (hold/blend the last frame toward the head). Unknown values fall back
            to ``"crossfade"``.
        dur: blend length in seconds. Auto-clamped to ``min(dur, 0.5*length)``
            (and floored at ~2 frames) so it is always valid.

    Returns:
        ``out_path`` on success.

    Raises:
        LoopError: on bad arguments or if every ffmpeg strategy fails.
    """
    if dur is None or float(dur) <= 0:
        raise LoopError(f"dur must be > 0, got {dur!r}")
    mode = (mode or "crossfade").lower()
    if mode not in VALID_MODES:
        mode = "crossfade"

    in_path = os.path.abspath(in_path)
    if not os.path.isfile(in_path):
        raise LoopError(f"input not found: {in_path!r}")
    out_path = os.path.abspath(out_path)
    if in_path == out_path:
        raise LoopError("input and output paths are identical; choose a distinct -o")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    info = _probe(in_path)
    total = info["duration"]
    has_audio = info["has_audio"]

    # Clamp the blend so 0 < blend <= 0.5*length, floored at ~2 frames.
    blend = min(float(dur), _MAX_FRAC * total)
    blend = max(blend, _MIN_BLEND_S)

    # Too short to loop meaningfully — emit a valid, spec-compliant passthrough.
    if total < _MIN_LOOPABLE_S or blend >= total:
        print(
            f"[loop] clip is {total:.2f}s — too short to loop; "
            f"re-encoding to spec without a blend.",
            file=sys.stderr,
        )
        _passthrough(in_path, out_path, has_audio=has_audio)
        _report(mode, out_path)
        return out_path

    # freeze_match keeps a tighter window so most motion survives.
    if mode == "freeze_match":
        blend = min(blend, max(_MIN_BLEND_S, min(0.25, 0.25 * total)))

    # Strategy 1: the requested blend.
    try:
        if mode == "freeze_match":
            _freeze_match(in_path, out_path, blend=blend, total=total,
                          has_audio=has_audio)
        else:
            _crossfade(in_path, out_path, blend=blend, total=total,
                       has_audio=has_audio)
        _report(mode, out_path)
        return out_path
    except LoopError as first_err:
        # Strategy 2: minimal fallback — plain tail-to-head crossfade at the
        # clamped <= 0.5*length blend, regardless of the requested mode.
        fb = min(DEFAULT_DUR, _MAX_FRAC * total)
        fb = max(fb, _MIN_BLEND_S)
        try:
            _crossfade(in_path, out_path, blend=fb, total=total,
                       has_audio=has_audio)
            print(
                f"[loop] primary {mode} blend failed; used fallback "
                f"crossfade ({fb:.3f}s).",
                file=sys.stderr,
            )
            _report("crossfade(fallback)", out_path)
            return out_path
        except LoopError as second_err:
            raise LoopError(
                "make_seamless: both the primary and fallback strategies "
                f"failed.\n--- primary ({mode}) ---\n{first_err}\n"
                f"--- fallback (crossfade) ---\n{second_err}"
            ) from second_err


def _report(label: str, out_path: str) -> None:
    info = _probe(out_path)
    print(
        f"[loop] {label} -> {out_path} "
        f"({info['duration']:.2f}s, has_audio={info['has_audio']})",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# self-test / CLI
# --------------------------------------------------------------------------- #
def _make_test_clip(path: str, seconds: float = 6.0) -> List[str]:
    """Synthesize a 6s 1080x1920 testsrc + 440Hz sine clip; return the command."""
    cmd = [
        FFMPEG, "-y",
        "-f", "lavfi", "-i",
        f"testsrc=size={TARGET_W}x{TARGET_H}:rate={FPS}:duration={seconds}",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-shortest",
        *_video_out_tail(), path,
    ]
    _run(cmd, what="build test clip")
    return cmd


def _probe_report(path: str) -> dict:
    """Detailed probe for printing: duration/size/fps/codecs/has_audio."""
    proc = _run(
        [
            FFPROBE, "-v", "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
            "-of", "json", path,
        ],
        what="ffprobe report",
    )
    data = json.loads(proc.stdout or "{}")
    rep: dict[str, Any] = {
        "duration": round(float(data.get("format", {}).get("duration", 0.0)), 3),
        "width": None, "height": None, "fps": None,
        "vcodec": None, "acodec": None, "has_audio": False,
    }
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and rep["width"] is None:
            rep["width"] = s.get("width")
            rep["height"] = s.get("height")
            rep["vcodec"] = s.get("codec_name")
            rfr = s.get("r_frame_rate", "0/1")
            try:
                num, den = rfr.split("/")
                rep["fps"] = round(float(num) / float(den), 3) if float(den) else None
            except (ValueError, ZeroDivisionError):
                rep["fps"] = None
        elif s.get("codec_type") == "audio":
            rep["has_audio"] = True
            rep["acodec"] = s.get("codec_name")
    return rep


def _self_test() -> int:
    """Build a 6s clip, crossfade-loop it, and assert the output is valid."""
    print("# engine/loop.py — self-test\n")
    if not (shutil.which(FFMPEG) and shutil.which(FFPROBE)):
        print("ERROR: ffmpeg/ffprobe not found on PATH")
        return 1

    workdir = tempfile.mkdtemp(prefix="loop_selftest_")
    src = os.path.join(workdir, "test_src.mp4")
    out = os.path.join(workdir, "test_loop.mp4")
    print(f"workdir: {workdir}\n")

    print("1) build 6s testsrc + 440Hz sine clip")
    build_cmd = _make_test_clip(src, seconds=6.0)
    print(f"   $ {_fmt_cmd(build_cmd)}")
    src_rep = _probe_report(src)
    print(f"   src probe: {json.dumps(src_rep)}\n")

    print('2) make_seamless(mode="crossfade", dur=0.5)')
    result = make_seamless(src, out, mode="crossfade", dur=0.5)
    print(f"   wrote: {result}")
    # Surface the exact ffmpeg command used (re-derive for the record).
    print(f"   (encode tail: {_fmt_cmd(_video_out_tail())})\n")

    print("3) ffprobe on output")
    rep = _probe_report(out)
    print(f"   out probe: {json.dumps(rep)}\n")

    # The seamless-loop crossfade preserves total length (full clip A + head B),
    # so the output should be ~= the source duration.
    dur_ok = abs(rep["duration"] - src_rep["duration"]) <= 0.35
    checks = [
        ("output exists & non-empty", os.path.isfile(out) and os.path.getsize(out) > 0),
        ("1080x1920", rep["width"] == TARGET_W and rep["height"] == TARGET_H),
        ("h264 video", rep["vcodec"] == "h264"),
        ("has audio (aac)", rep["has_audio"] and rep["acodec"] == "aac"),
        ("~30 fps", rep["fps"] is not None and abs(rep["fps"] - FPS) < 1.0),
        ("duration ~= source", dur_ok),
    ]
    ok = True
    print("   checks:")
    for name, passed in checks:
        print(f"     {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed

    print("\n4) loop_score() spot-checks")
    cases = [
        ("buy now, learn fast, buy now", True),
        ("welcome to counza, let's go", False),
        (["start", "middle", "end", "start"], True),
        ([{"word": "Hello"}, {"word": "world"}, {"word": "hello"}], True),
        ("the quick brown fox the", False),  # single-token echo is a stop word
    ]
    for inp, expected in cases:
        got = loop_score(inp)
        flag = "PASS" if got == expected else "FAIL"
        ok = ok and (got == expected)
        shown = inp if isinstance(inp, str) else json.dumps(inp)
        print(f"     {flag}  loop_score({shown}) -> {got} (want {expected})")

    print(f"\nRESULT: {'ALL PASS' if ok else 'FAILURES PRESENT'}")
    print(f"artifact (inspect/keep): {out}")
    return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loop.py",
        description="Make a vertical short loop seamlessly (end flows into start).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", nargs="?", help="finished MP4 to loop")
    p.add_argument("-o", "--output", help="output .mp4")
    p.add_argument("--mode", default="crossfade", choices=VALID_MODES,
                   help="loop strategy")
    p.add_argument("--dur", type=float, default=DEFAULT_DUR,
                   help="blend length in seconds (auto-clamped to <= 0.5*length)")
    p.add_argument("--self-test", action="store_true",
                   help="build a synthetic clip and verify the loop pipeline")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if not args.input or not args.output:
        build_parser().error(
            "input and -o/--output are required unless --self-test is given"
        )
    try:
        out = make_seamless(args.input, args.output, mode=args.mode, dur=args.dur)
    except LoopError as exc:
        print(f"\n[loop] ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(_probe_report(out), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
