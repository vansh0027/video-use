"""Reel-style preset system for the content engine.

A *style* is a named bundle of look-and-feel decisions that turns the engine's
"one kind of reel" into a menu of distinct looks. Where a *profile*
(engine/profiles/) controls EDITING defaults (target length, trim thresholds)
and a *brandkit* (engine/brandkits/) controls IDENTITY (colors, fonts, CTA
copy), a *style* controls the AESTHETIC: which grade preset, which caption
preset (+ overrides), how much punch-in, how fast the cuts, the default
transition, and how loud the music/SFX layer sits.

Styles live as JSON in ``engine/styles/presets/<name>.json`` and load through the
tiny stdlib loader in :mod:`engine.styles.registry`.

    from engine.styles.registry import load_style, available_styles
    style = load_style("dark_sizzle")
    style["grade"]["preset"]        # -> "warm_cinematic"

See ``engine/styles/INTEGRATION.md`` for how build_short.py / fanout.py consume
a style and merge it over the brandkit + profile.
"""

from __future__ import annotations

__all__ = ["registry"]
