#!/usr/bin/env python3
"""
engine/effects/sfx.py
=====================

SFX layer — synthesized short-form audio cues (whoosh / pop / riser / impact),
placed at cut/text event timestamps and mixed under the program audio.

No external asset files are required: each cue is generated from an ffmpeg
``lavfi`` graph (``sine`` / ``aevalsrc`` + envelope fades), written once to a
small wav, and reused. If the user *does* have their own SFX folder, pass it to
:func:`resolve_cue_path` and a matching ``<name>.wav`` there wins over the
synthesized one.

Two halves, both pure-*ish*:

  * **Generators** — :func:`cue_lavfi_args` returns the ffmpeg input args that
    synthesize a single cue (no process). :func:`write_cue` actually runs ffmpeg
    to materialize the wav (the one documented side effect).
  * **Placement** — :func:`build_sfx_mix` takes a list of event timestamps + a
    cue library and returns a ``-filter_complex`` spec (graph string + the extra
    ``-i`` inputs + the output label) that lays each cue at its timestamp and
    mixes everything under the program audio with :func:`amix`.

Design / hard-rule alignment
----------------------------
* SFX are placed by **delaying** a cue input to its event time (``adelay``) and
  mixing with ``amix`` — the program audio is input ``0`` and is never replaced,
  only summed under (cues sit a few dB below voice via ``volume``).
* The cue envelope uses short ``afade`` in/out so a placed cue can never click.
* Cues are deliberately SHORT (60–600 ms). A whoosh sits on a transition; a pop
  marks a one-word caption beat; a riser leads into a hard cut; an impact lands
  on the cut frame.
* This module emits the *graph only*; the final ``loudnorm`` still runs once at
  the very end of the pipeline (see audio_polish.loudnorm_af), AFTER this mix.

Usage
-----
    from engine.effects import sfx

    # 1) materialize the cue library once (synthesized wavs):
    lib = sfx.build_cue_library("/tmp/sfx_cache")        # {name: wav_path}

    # 2) plan placements from event timestamps:
    plan = sfx.build_sfx_mix(
        events=[{"t": 1.2, "cue": "whoosh"}, {"t": 3.4, "cue": "pop"}],
        library=lib,
        gain_db=-12.0,
    )
    # plan.inputs        -> ["-i", "/tmp/.../whoosh.wav", "-i", ".../pop.wav"]
    # plan.filtergraph   -> "[1:a]adelay=...|...,volume=-12dB[c0];... [aout]"
    # plan.out_label     -> "[aout]"

Run directly to print specs and synthesize + probe a real cue wav:

    python engine/effects/sfx.py            # print specs
    python engine/effects/sfx.py --render <dir>   # synthesize wavs + probe
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "CUES",
    "cue_lavfi_args",
    "write_cue",
    "build_cue_library",
    "resolve_cue_path",
    "build_sfx_mix",
    "SfxMix",
    "INTENSITY_GAIN_DB",
]

# Standard 48 kHz stereo, the engine's working audio rate.
_SR = 48000

# Intensity -> default mix gain in dB (how far cues sit *under* the program).
# "off" yields no cues at all (build_sfx_mix returns an empty plan).
INTENSITY_GAIN_DB: Dict[str, Optional[float]] = {
    "off": None,
    "light": -16.0,
    "medium": -12.0,
    "heavy": -8.0,
}


# --------------------------------------------------------------------------- #
# Cue synthesis recipes
# --------------------------------------------------------------------------- #
# Each recipe is a self-contained lavfi expression that produces ONE cue of
# ``dur`` seconds at 48 kHz stereo. We keep them parametric on duration so the
# same name can be stretched for a longer riser, etc. Every recipe ends shaped
# by an afade in/out so the rendered wav never clicks.

def _whoosh(dur: float) -> str:
    """Airy noise swish: filtered white noise swept by a volume ramp.

    ``anoisesrc`` (pink) -> bandpass that opens up -> fast fade in, fade out.
    Reads as the "swipe" under a whip-pan or quick cut.
    """
    return (
        f"anoisesrc=color=pink:amplitude=0.6:duration={dur:.3f}:sample_rate={_SR},"
        "highpass=f=300,lowpass=f=6000,"
        f"afade=t=in:st=0:d={dur*0.45:.3f},"
        f"afade=t=out:st={dur*0.5:.3f}:d={dur*0.5:.3f},"
        "aformat=channel_layouts=stereo"
    )


def _pop(dur: float) -> str:
    """Tight tonal blip: a short high sine with a near-instant decay.

    Marks a one-word caption beat / keyword pop. Very short by default.
    """
    return (
        f"sine=frequency=880:duration={dur:.3f}:sample_rate={_SR},"
        f"afade=t=in:st=0:d=0.004,"
        f"afade=t=out:st={dur*0.25:.3f}:d={dur*0.75:.3f},"
        "aformat=channel_layouts=stereo"
    )


def _riser(dur: float) -> str:
    """Rising tension sweep: a sine whose frequency ramps up over the cue.

    ``aevalsrc`` with a time-dependent frequency term — leads INTO a hard cut.
    """
    # Frequency ramps 220 Hz -> 1320 Hz across the cue. Phase = 2*pi*∫f dt; with
    # a linear f(t)=f0+k*t the integral is f0*t + k*t^2/2. We bake that into the
    # eval expression so the pitch glides smoothly with no zipper.
    f0 = 220.0
    k = (1320.0 - f0) / max(dur, 1e-3)
    phase = f"(2*PI*({f0}*t+{k/2.0:.4f}*t*t))"
    return (
        f"aevalsrc='0.5*sin({phase})':d={dur:.3f}:s={_SR}:c=stereo,"
        f"afade=t=in:st=0:d={dur*0.5:.3f},"
        f"afade=t=out:st={dur*0.85:.3f}:d={dur*0.15:.3f}"
    )


def _impact(dur: float) -> str:
    """Low boom that lands on the cut frame: a low sine with a fast attack.

    Sub-heavy ``sine`` at 70 Hz, instant attack, exponential-ish decay via a
    long fade-out. Use sparingly — one per hard punch-in, not per cut.
    """
    return (
        f"sine=frequency=70:duration={dur:.3f}:sample_rate={_SR},"
        f"afade=t=in:st=0:d=0.003,"
        f"afade=t=out:st={dur*0.15:.3f}:d={dur*0.85:.3f},"
        "aformat=channel_layouts=stereo"
    )


# Cue name -> (recipe_fn, default_duration_s). build_cue_library() walks this.
CUES: Dict[str, Any] = {
    "whoosh": (_whoosh, 0.45),
    "pop":    (_pop,    0.12),
    "riser":  (_riser,  0.60),
    "impact": (_impact, 0.40),
}


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #
def cue_lavfi_args(name: str, dur: Optional[float] = None) -> List[str]:
    """Return the ffmpeg input args that synthesize cue ``name`` (no render).

    The result is the ``-f lavfi -i <graph>`` pair you would hand to ffmpeg to
    *generate* the cue on the fly. :func:`write_cue` uses this to materialize a
    wav; you can also splice it directly as an extra input if you prefer to
    synthesize inline (no cache).

    Args:
        name: one of :data:`CUES` (``whoosh`` / ``pop`` / ``riser`` / ``impact``).
        dur: override duration in seconds; defaults to the cue's natural length.

    Returns:
        ``["-f", "lavfi", "-i", "<lavfi-graph>"]``.

    Raises:
        KeyError: if ``name`` is unknown.
        ValueError: if ``dur`` is non-positive.
    """
    if name not in CUES:
        raise KeyError(f"unknown cue {name!r}. Known: {', '.join(sorted(CUES))}")
    recipe, default_dur = CUES[name]
    d = float(default_dur if dur is None else dur)
    if d <= 0:
        raise ValueError(f"cue duration must be > 0, got {d}")
    return ["-f", "lavfi", "-i", recipe(d)]


def write_cue(
    name: str,
    out_dir: str,
    dur: Optional[float] = None,
    ffmpeg: str = "ffmpeg",
) -> str:
    """Synthesize cue ``name`` to ``out_dir/<name>.wav`` and return its path.

    This is the one documented **side effect** in the module: it shells out to
    ffmpeg. The wav is 48 kHz stereo PCM s16. Existing files are overwritten so
    a cache refresh is a re-call.

    Raises:
        RuntimeError: if ffmpeg fails (stderr tail included).
    """
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{name}.wav")
    exe = shutil.which(ffmpeg) or ffmpeg
    cmd = [exe, "-hide_banner", "-y", *cue_lavfi_args(name, dur),
           "-ar", str(_SR), "-ac", "2", "-c:a", "pcm_s16le", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not os.path.exists(out_path):
        tail = "\n".join((proc.stderr or "").splitlines()[-12:])
        raise RuntimeError(f"ffmpeg failed to synthesize cue {name!r}:\n{tail}")
    return out_path


def build_cue_library(
    out_dir: str,
    names: Optional[Sequence[str]] = None,
    ffmpeg: str = "ffmpeg",
) -> Dict[str, str]:
    """Materialize every cue (or a subset) to ``out_dir``; return ``{name: path}``.

    Call once per render; the returned mapping is what :func:`build_sfx_mix`
    expects as its ``library``.
    """
    names = list(names) if names else list(CUES)
    return {n: write_cue(n, out_dir, ffmpeg=ffmpeg) for n in names}


def resolve_cue_path(
    name: str,
    library: Dict[str, str],
    user_sfx_dir: Optional[str] = None,
    require_exists: bool = True,
) -> str:
    """Resolve a cue name to a wav path, preferring a user-supplied file.

    If ``user_sfx_dir`` contains ``<name>.wav`` (or ``.mp3`` / ``.m4a``), that
    path wins; otherwise the synthesized ``library[name]`` is used.

    Args:
        require_exists: if True (default), the resolved path must exist on disk
            — the normal render path, where the library has been materialized.
            Set False to plan a graph against a library whose wavs are not yet
            written (e.g. dry-run / unit tests).

    Raises:
        KeyError: if the cue is in neither the user dir nor the library (or, when
            ``require_exists``, none of the candidates exist).
    """
    if user_sfx_dir:
        for ext in (".wav", ".mp3", ".m4a", ".aac"):
            cand = os.path.join(user_sfx_dir, f"{name}{ext}")
            if os.path.isfile(cand):
                return cand
    if name in library:
        path = library[name]
        if not require_exists or os.path.isfile(path):
            return path
    raise KeyError(
        f"no cue {name!r}: not in user dir {user_sfx_dir!r} nor library "
        f"({', '.join(sorted(library)) or 'empty'})"
    )


# --------------------------------------------------------------------------- #
# Placement / mix
# --------------------------------------------------------------------------- #
@dataclass
class SfxMix:
    """A ready-to-splice SFX mix plan.

    Attributes:
        inputs: extra ffmpeg input args (``-i path`` pairs) for the cue wavs, in
            order. The PROGRAM audio is assumed to be input ``0`` already on the
            command line; the first cue is therefore input ``1``.
        filtergraph: a ``-filter_complex`` body that delays each cue to its
            event time, attenuates it, mixes all cues + program audio, and exits
            on :attr:`out_label`. Empty string when there are no cues.
        out_label: the audio output label to ``-map`` (``[aout]``). When there
            are no cues this is ``[0:a]`` (the untouched program audio).
        n_cues: how many cue inputs are present.
    """
    inputs: List[str] = field(default_factory=list)
    filtergraph: str = ""
    out_label: str = "[0:a]"
    n_cues: int = 0


def _delay_ms(t: float) -> int:
    return max(0, int(round(float(t) * 1000.0)))


def build_sfx_mix(
    events: Sequence[Dict[str, Any]],
    library: Dict[str, str],
    gain_db: float = -12.0,
    user_sfx_dir: Optional[str] = None,
    program_input: int = 0,
    first_cue_input: int = 1,
    require_exists: bool = True,
) -> SfxMix:
    """Plan an SFX mix that lays each event's cue at its timestamp under the program.

    Args:
        events: list of ``{"t": <seconds>, "cue": <name>}`` dicts. ``t`` is on
            the OUTPUT timeline (the final concatenated video), not a source
            clip. An optional per-event ``"gain_db"`` overrides ``gain_db``.
        library: ``{name: wav_path}`` from :func:`build_cue_library`.
        gain_db: default attenuation applied to every cue before the mix
            (negative = quieter). See :data:`INTENSITY_GAIN_DB`.
        user_sfx_dir: optional folder of user wavs that override synthesized cues.
        program_input: ffmpeg input index of the program audio (default 0).
        first_cue_input: ffmpeg input index the first cue wav will occupy
            (default 1 — i.e. the cue ``-i`` args come right after the program).

    Returns:
        An :class:`SfxMix`. When ``events`` is empty the plan is a no-op that
        maps the program audio straight through (``out_label == "[0:a]"``).

    Notes:
        The caller is responsible for placing the cue ``-i`` inputs on the
        command line *in the same order* as :attr:`SfxMix.inputs`, starting at
        ``first_cue_input``. ``amix`` normalizes by input count, so we boost the
        summed program path back up with the program kept at unity and cues
        pre-attenuated; ``normalize=0`` keeps the voice at full level.
    """
    valid = [e for e in (events or []) if e.get("cue") and e.get("t") is not None]
    if not valid:
        return SfxMix(out_label=f"[{program_input}:a]")

    inputs: List[str] = []
    graph_parts: List[str] = []
    cue_labels: List[str] = []

    idx = first_cue_input
    for n, ev in enumerate(valid):
        path = resolve_cue_path(str(ev["cue"]), library, user_sfx_dir,
                                require_exists=require_exists)
        inputs += ["-i", path]
        g = float(ev.get("gain_db", gain_db))
        g_str = f"{g:.3f}".rstrip("0").rstrip(".")
        delay = _delay_ms(ev["t"])
        lbl = f"[c{n}]"
        # adelay needs one value per channel (stereo) -> "d|d". Then attenuate.
        graph_parts.append(
            f"[{idx}:a]adelay={delay}|{delay},volume={g_str}dB{lbl}"
        )
        cue_labels.append(lbl)
        idx += 1

    # Mix the program audio (kept at unity) with all pre-attenuated cues.
    # normalize=0 so the voice level is not pulled down by the cue count.
    n_inputs = len(cue_labels) + 1
    mix_in = f"[{program_input}:a]" + "".join(cue_labels)
    graph_parts.append(
        f"{mix_in}amix=inputs={n_inputs}:normalize=0:"
        f"duration=first:dropout_transition=0[aout]"
    )

    return SfxMix(
        inputs=inputs,
        filtergraph=";".join(graph_parts),
        out_label="[aout]",
        n_cues=len(cue_labels),
    )


# --------------------------------------------------------------------------- #
# Convenience: derive events from cut/caption timestamps
# --------------------------------------------------------------------------- #
def events_from_cuts(
    cut_times: Sequence[float],
    cue: str = "whoosh",
    skip_first: bool = True,
) -> List[Dict[str, Any]]:
    """Map a list of cut timestamps to SFX events (one cue per cut).

    ``skip_first`` drops a cue on the very first boundary (t≈0) where there is
    no incoming cut to underline.
    """
    out: List[Dict[str, Any]] = []
    for i, t in enumerate(cut_times or []):
        if skip_first and i == 0:
            continue
        out.append({"t": float(t), "cue": cue})
    return out


# --------------------------------------------------------------------------- #
# CLI / smoke
# --------------------------------------------------------------------------- #
def _demo(render_dir: Optional[str]) -> int:
    print("# engine/effects/sfx.py — specs\n")
    for name in CUES:
        print(f"cue_lavfi_args({name!r}):")
        print(f"  {shlex.join(cue_lavfi_args(name))}\n")

    events = [{"t": 1.20, "cue": "whoosh"}, {"t": 3.40, "cue": "pop"},
              {"t": 5.00, "cue": "impact"}]
    fake_lib = {n: f"/cache/{n}.wav" for n in CUES}
    plan = build_sfx_mix(events, fake_lib, gain_db=-12.0, require_exists=False)
    print("build_sfx_mix(3 events):")
    print(f"  inputs     = {shlex.join(plan.inputs)}")
    print(f"  out_label  = {plan.out_label}  (n_cues={plan.n_cues})")
    print(f"  graph      = {plan.filtergraph}\n")

    print("events_from_cuts([0,2.5,4.1]):")
    print(f"  {events_from_cuts([0, 2.5, 4.1])}\n")

    if render_dir:
        print(f"# --render: synthesizing cues into {render_dir}\n")
        lib = build_cue_library(render_dir)
        for name, path in lib.items():
            size = os.path.getsize(path)
            # probe duration to prove it's a valid wav
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, check=False,
            )
            dur = (probe.stdout or "").strip() or "?"
            print(f"  OK  {name:8s} {size:6d} bytes  dur={dur}s  {path}")

        # End-to-end: mix the real cues under a 6s silent program track.
        print("\n# --render: building a real SFX mix under a silent program\n")
        mix_out = os.path.join(render_dir, "sfx_mix.m4a")
        plan = build_sfx_mix(events, lib, gain_db=-12.0)
        exe = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [
            exe, "-hide_banner", "-y",
            # input 0 = program audio (6s of silence as a stand-in)
            "-f", "lavfi", "-i", f"anullsrc=r={_SR}:cl=stereo:d=6",
            *plan.inputs,
            "-filter_complex", plan.filtergraph,
            "-map", plan.out_label,
            "-c:a", "aac", mix_out,
        ]
        print(f"  ffmpeg: {shlex.join(cmd)}\n")
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not os.path.exists(mix_out):
            tail = "\n".join((proc.stderr or "").splitlines()[-15:])
            print(f"  MIX FAILED:\n{tail}")
            return 1
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_name,channels,sample_rate",
             "-of", "default=nw=1", mix_out],
            capture_output=True, text=True, check=False,
        )
        print(f"  OK wrote {mix_out} ({os.path.getsize(mix_out)} bytes)")
        for line in (probe.stdout or "").strip().splitlines():
            print(f"    {line}")
    return 0


if __name__ == "__main__":
    rd = None
    if "--render" in sys.argv:
        i = sys.argv.index("--render")
        rd = sys.argv[i + 1] if i + 1 < len(sys.argv) else "/tmp/sfx_cache"
    raise SystemExit(_demo(rd))
