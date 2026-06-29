#!/usr/bin/env python3
"""
engine/audio_polish.py
======================

Audio-polish filter-chain builders for the Counza content engine.

These functions return **ffmpeg audio-filter strings** (and one filtergraph
snippet for music ducking). They do *no* rendering themselves — the caller
splices the returned string into an `-af` / `-filter_complex` invocation. This
keeps the module pure, testable, and trivially composable with the existing
`render.py` / `grade.py` helpers.

Every filter token used here has been confirmed present in the project ffmpeg
(`ffmpeg-full` 8.x, built with libass). See the `_FILTER_REQUIREMENTS` list and
`verify_filters()` for a runtime self-check.

Hard-rule alignment
-------------------
* 30 ms fades at *every* segment boundary  -> `boundary_afades()`
* Final loudness target -14 LUFS / -1 dBTP -> `loudnorm_af()`

Usage
-----
    from engine.audio_polish import polish_af, boundary_afades, loudnorm_af

    af = ",".join([
        polish_af(),                 # clean the voice
        boundary_afades(seg_dur),    # 30 ms in/out fades
        loudnorm_af(),               # normalize to broadcast loudness
    ])
    subprocess.run(["ffmpeg", "-i", src, "-af", af, out])

Run directly to print the chains and (optionally) self-verify:

    python engine/audio_polish.py            # print chains
    python engine/audio_polish.py --verify   # print + probe ffmpeg filters
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys

__all__ = [
    "polish_af",
    "boundary_afades",
    "loudnorm_af",
    "music_duck",
    "verify_filters",
    "FADE_DUR",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hard Rule: 30 ms fades at every segment boundary.
FADE_DUR: float = 0.03

# Filter tokens this module emits. Used by verify_filters() to confirm the
# local ffmpeg build actually provides them before any render is attempted.
_FILTER_REQUIREMENTS: tuple[str, ...] = (
    "highpass",
    "afftdn",
    "deesser",
    "acompressor",
    "afade",
    "loudnorm",
    "sidechaincompress",
    "aformat",
    "amix",
    "volume",
)


# ---------------------------------------------------------------------------
# Voice cleanup
# ---------------------------------------------------------------------------

def polish_af() -> str:
    """Return a voice-cleanup audio-filter chain for talking-head footage.

    Chain (left to right):
      * ``highpass=f=80``      - roll off rumble / HVAC / mic-stand thumps below 80 Hz.
      * ``afftdn=nf=-25``      - FFT broadband denoise, noise floor -25 dB (gentle,
                                 preserves consonants; bump magnitude for noisier rooms).
      * ``deesser``            - tame sibilant "sss"/"shh" peaks (defaults are sane).
      * ``acompressor=...``    - even out level: threshold -18 dB, 3:1 ratio,
                                 20 ms attack, 250 ms release, +2 dB makeup.

    All four tokens are confirmed present in the project ffmpeg build. Returns a
    plain string suitable for ``-af`` (or as one node in a larger ``-af`` list).
    """
    return (
        "highpass=f=80,"
        "afftdn=nf=-25,"
        "deesser,"
        "acompressor=threshold=-18dB:ratio=3:attack=20:release=250:makeup=2"
    )


# ---------------------------------------------------------------------------
# Segment-boundary fades  (Hard Rule)
# ---------------------------------------------------------------------------

def boundary_afades(dur: float) -> str:
    """Return 30 ms in/out fades for a segment of length ``dur`` seconds.

    Hard Rule: every segment boundary gets a 30 ms fade so concatenated cuts
    never click/pop. Produces::

        afade=t=in:st=0:d=0.03,afade=t=out:st=<dur-0.03>:d=0.03

    Args:
        dur: Segment duration in seconds. Must be > ``2 * FADE_DUR`` (0.06 s) so
             the in- and out-fades do not overlap; raises ``ValueError`` otherwise.
    """
    dur = float(dur)
    if dur <= 2 * FADE_DUR:
        raise ValueError(
            f"segment duration {dur:.3f}s is too short for two {FADE_DUR*1000:.0f}ms "
            f"fades (need > {2*FADE_DUR:.3f}s)"
        )
    out_start = dur - FADE_DUR
    # Trim trailing zeros for a clean, deterministic string (0.030000 -> 0.03).
    out_start_str = f"{out_start:.6f}".rstrip("0").rstrip(".")
    fade = f"{FADE_DUR:.6f}".rstrip("0").rstrip(".")
    return (
        f"afade=t=in:st=0:d={fade},"
        f"afade=t=out:st={out_start_str}:d={fade}"
    )


# ---------------------------------------------------------------------------
# Loudness normalization
# ---------------------------------------------------------------------------

def loudnorm_af() -> str:
    """Return EBU R128 loudness normalization to the Counza delivery target.

    ``loudnorm=I=-14:TP=-1:LRA=11`` — integrated -14 LUFS (matches YouTube /
    podcast / social conventions), true-peak ceiling -1 dBTP, loudness range 11.

    This is a single-pass loudnorm string. For the most accurate result, ffmpeg
    supports a two-pass workflow (measure, then feed ``measured_*`` back in); the
    single pass used here is the standard one-shot form and is what the pipeline
    applies as the final node in the audio chain.
    """
    return "loudnorm=I=-14:TP=-1:LRA=11"


# ---------------------------------------------------------------------------
# Background music bed with sidechain ducking  (optional, for integration)
# ---------------------------------------------------------------------------

def music_duck(music_path: str, duck_db: float = -20.0) -> str:
    """Return a ``-filter_complex`` snippet that lays ``music_path`` under the
    voice and **sidechain-ducks** it whenever the voice is present.

    This returns the *filtergraph body only* — the caller is responsible for the
    two inputs and for mapping the output label.

    Expected inputs (caller must provide, in this order)::

        ffmpeg -i <voice/video>  -i <music_path>  -filter_complex "<this string>" \\
               -map 0:v? -map "[aout]"  ...

      * input ``0`` — the program whose **audio** is the voice (label ``[0:a]``).
      * input ``1`` — the music file (label ``[1:a]``).

    Graph (labels in/out):
      * ``[1:a]`` music -> volume trim -> ``[mraw]``
      * ``[0:a]`` voice is duplicated with ``asplit`` into:
            - ``[vmain]`` — the voice that reaches the mix untouched.
            - ``[vkey]``  — the sidechain *key* that tells the compressor when to duck.
      * ``[mraw]`` (main) + ``[vkey]`` (sidechain) -> ``sidechaincompress`` ->
        ``[mduck]`` — music gain-reduced while the voice speaks.
      * ``[vmain]`` + ``[mduck]`` -> ``amix`` -> ``loudnorm`` -> ``[aout]``.

    Args:
        music_path: Path to the music file. Only used to validate/normalize the
            argument; the caller still passes it as ``-i``. Kept in the signature
            so callers self-document which bed this graph is for.
        duck_db: How far to pull the music down *before* mixing, in dB
            (negative = quieter). The sidechain compressor then ducks further
            only while the voice is active. Default -20 dB.

    Returns:
        A filtergraph string ending in the ``[aout]`` label.
    """
    if not music_path:
        raise ValueError("music_path must be a non-empty path")
    # duck_db is a pre-mix attenuation: convert dB to a linear gain for `volume`.
    # volume accepts dB directly (e.g. volume=-20dB), which is clearer than linear.
    duck_db = float(duck_db)
    db_str = f"{duck_db:.3f}".rstrip("0").rstrip(".")
    return (
        # Pre-attenuate the music bed.
        f"[1:a]volume={db_str}dB[mraw];"
        # Split the voice: one copy mixes in, one copy keys the sidechain.
        "[0:a]asplit=2[vmain][vkey];"
        # Duck the music using the voice as the sidechain key.
        "[mraw][vkey]sidechaincompress="
        "threshold=0.05:ratio=8:attack=20:release=300:makeup=1[mduck];"
        # Mix ducked music under the voice, then normalize the bus.
        "[vmain][mduck]amix=inputs=2:duration=first:dropout_transition=2[mixed];"
        "[mixed]loudnorm=I=-14:TP=-1:LRA=11[aout]"
    )


# ---------------------------------------------------------------------------
# Runtime filter verification
# ---------------------------------------------------------------------------

def verify_filters(ffmpeg: str = "ffmpeg") -> dict[str, bool]:
    """Probe the local ffmpeg and report which required filters are available.

    Returns a dict mapping each filter token in ``_FILTER_REQUIREMENTS`` to a
    bool. Does not raise on a missing filter — the caller decides how strict to
    be (the pipeline can, e.g., drop ``deesser`` and continue).

    If the ffmpeg binary cannot be found/run, every entry is ``False``.
    """
    exe = shutil.which(ffmpeg) or ffmpeg
    try:
        out = subprocess.run(
            [exe, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {name: False for name in _FILTER_REQUIREMENTS}

    available = set()
    for line in out.splitlines():
        # `-filters` rows look like: " T. afftdn   A->A   Denoise ..."
        parts = line.split()
        if len(parts) >= 2:
            available.add(parts[1])
    return {name: (name in available) for name in _FILTER_REQUIREMENTS}


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------

def _demo_dur() -> float:
    return 5.0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    do_verify = "--verify" in argv

    print("# engine/audio_polish.py — filter chains\n")

    print("polish_af():")
    print(f"  {polish_af()}\n")

    d = _demo_dur()
    print(f"boundary_afades({d}):")
    print(f"  {boundary_afades(d)}\n")

    print("loudnorm_af():")
    print(f"  {loudnorm_af()}\n")

    print("music_duck('bed.m4a', duck_db=-20):")
    print(f"  {music_duck('bed.m4a', duck_db=-20)}\n")

    # Show how the voice chain composes for a real segment.
    composed = ",".join([polish_af(), boundary_afades(d), loudnorm_af()])
    print(f"composed voice chain (segment of {d}s):")
    print(f"  -af {shlex.quote(composed)}\n")

    if do_verify:
        print("verify_filters():")
        results = verify_filters()
        for name, ok in results.items():
            print(f"  {'OK ' if ok else 'MISSING'}  {name}")
        missing = [n for n, ok in results.items() if not ok]
        if missing:
            print(f"\n  WARNING: missing filters: {', '.join(missing)}")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
