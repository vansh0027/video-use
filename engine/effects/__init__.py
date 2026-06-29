"""Short-form effects library for the content engine.

Each submodule is a collection of **pure** ffmpeg-filter / argument builders —
they return strings (and small plan dicts) and render nothing themselves. The
caller splices the result into its own ffmpeg invocation, exactly like
``engine/transforms.py`` and ``engine/audio_polish.py``.

Submodules
----------
* :mod:`engine.effects.sfx`         — synthesize whoosh/pop/riser/impact cues
                                       with lavfi (no asset files needed) and
                                       build the audio-mix args to place them.
* :mod:`engine.effects.transitions` — xfade presets (dissolve, fade-through-
                                       black), whip-pan, zoom. 90% of cuts
                                       should stay HARD cuts.
* :mod:`engine.effects.speed_ramp`  — setpts/atempo constant speed changes and
                                       low-energy dead-air speed-ups.
* :mod:`engine.effects.broll`       — full-frame cutaway inserts that KEEP the
                                       A-roll audio (say-it-show-it).

All of the above are stdlib-only and side-effect-free except where a function
*writes a synthesized cue wav* (clearly documented and opt-in).

See ``engine/effects/INTEGRATION.md`` for exactly how build_short.py / fanout.py
should call these and where each hooks into the render pipeline.
"""

from __future__ import annotations

__all__ = ["sfx", "transitions", "speed_ramp", "broll"]
