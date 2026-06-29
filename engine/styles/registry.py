"""Reel-style loader for the content engine.

A *style* is a JSON bundle of aesthetic defaults — grade preset, caption preset
(+ overrides), punch-in setting, pacing (cut cadence), default transition, and
music/SFX intensity — that the engine layers over a brandkit + edit profile to
produce a distinct "look". Styles live next to this module in
``engine/styles/presets/<name>.json``.

This loader mirrors ``engine/profiles/loader.py`` deliberately: tiny, stdlib
only, no dependency on the video toolchain, so it imports cleanly from any
helper or test.

Style schema (all keys documented in each preset JSON)
------------------------------------------------------
    name           str    — the style id (matches the filename).
    description    str    — one-line human summary.
    grade          {preset: <grade.py preset>, ...}   subtle|neutral_punch|warm_cinematic|none
    captions       {preset: <captions_animated preset>, overrides: {...}}
                          — preset is karaoke_line|oneword_pop|clean_twoline;
                            overrides merge into brandkit.caption_style.
    punch_in       {pct: int, frames: int} | {on: false}
    pacing         {cut_cadence_s, max_static_shot_s, style: hard|montage|slow}
    transition     {default: hard_cut|dissolve|fade_through_black|whip_pan|zoom,
                    frequency: 0..1}   — how often the non-hard transition fires.
    sfx            {intensity: off|light|medium|heavy, cues: [...]}
    music          {intensity: off|light|medium|heavy, duck_db: int}
    notes          str    — editorial guidance for the agent.

Usage
-----
    from engine.styles.registry import load_style, available_styles
    style = load_style("clean_premium")
    style["captions"]["preset"]    # -> "clean_twoline"
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["load_style", "available_styles", "STYLES_DIR"]

# Directory holding the style preset JSONs (this file's dir + presets/).
STYLES_DIR = Path(__file__).resolve().parent / "presets"


def _style_path(name: str) -> Path:
    """Resolve a style name to its JSON path.

    Accepts a bare name (``"dark_sizzle"``), a name with ``.json``, or an
    absolute/relative path to a JSON file.
    """
    if not name or not str(name).strip():
        raise ValueError("style name must be a non-empty string")

    candidate = Path(name)
    if candidate.suffix == ".json" and (candidate.is_absolute() or candidate.exists()):
        return candidate

    stem = candidate.name[:-5] if candidate.name.endswith(".json") else candidate.name
    return STYLES_DIR / f"{stem}.json"


def load_style(name: str) -> dict:
    """Load a reel style by name and return it as a ``dict``.

    Parameters
    ----------
    name:
        Style name without extension (e.g. ``"hormozi_punch"``), with ``.json``,
        or a full path to a style JSON file.

    Returns
    -------
    dict
        The parsed style object.

    Raises
    ------
    FileNotFoundError
        If no matching style exists. The message lists the available styles.
    ValueError
        If the file is not valid JSON or not a JSON object.
    """
    path = _style_path(name)
    if not path.is_file():
        known = ", ".join(available_styles()) or "(none found)"
        raise FileNotFoundError(
            f"style {name!r} not found at {path}. Available styles: {known}"
        )

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"style {path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"style {path} must contain a JSON object, got {type(data).__name__}"
        )
    return data


def available_styles() -> list[str]:
    """Return the sorted names (without extension) of all styles on disk."""
    return sorted(p.stem for p in STYLES_DIR.glob("*.json"))


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        print(json.dumps(load_style(sys.argv[1]), indent=2))
    else:
        names = available_styles()
        print(f"available styles ({len(names)}): {', '.join(names)}\n")
        for n in names:
            s = load_style(n)
            cap = s.get("captions", {}).get("preset", "?")
            grade = s.get("grade", {}).get("preset", "?")
            trans = s.get("transition", {}).get("default", "?")
            sfx = s.get("sfx", {}).get("intensity", "?")
            print(f"  {n:16s} grade={grade:14s} caption={cap:13s} "
                  f"transition={trans:18s} sfx={sfx}")
