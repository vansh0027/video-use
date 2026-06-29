"""Edit-profile loader for the content engine.

A *profile* is a JSON file of editing defaults (target length, trim thresholds,
caption/audio/CTA behaviour) that the engine consults when turning raw footage
into a cut. Profiles live next to this module in ``engine/profiles/<name>.json``.

Usage
-----
    from loader import load_profile          # when profiles/ is on sys.path
    # or
    from engine.profiles.loader import load_profile

    prof = load_profile("founder_edtech")
    prof["target_seconds"]                   # -> [20, 30]

The loader is intentionally tiny and dependency-free (stdlib only) so it can be
imported from any helper or test without pulling in the video toolchain.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["load_profile", "available_profiles", "PROFILES_DIR"]

# Directory that holds the profile JSON files (this file's own directory).
PROFILES_DIR = Path(__file__).resolve().parent


def _profile_path(name: str) -> Path:
    """Resolve a profile name to its JSON path.

    Accepts a bare name (``"founder_edtech"``), a name with the ``.json``
    suffix, or an absolute/relative path to a JSON file.
    """
    if not name or not str(name).strip():
        raise ValueError("profile name must be a non-empty string")

    candidate = Path(name)
    # An explicit path (absolute, or any path that already exists) wins.
    if candidate.suffix == ".json" and (candidate.is_absolute() or candidate.exists()):
        return candidate

    stem = candidate.name[:-5] if candidate.name.endswith(".json") else candidate.name
    return PROFILES_DIR / f"{stem}.json"


def load_profile(name: str) -> dict:
    """Load an edit profile by name and return it as a ``dict``.

    Parameters
    ----------
    name:
        Profile name without extension (e.g. ``"founder_edtech"``), with the
        ``.json`` suffix, or a full path to a profile JSON file.

    Returns
    -------
    dict
        The parsed profile object.

    Raises
    ------
    FileNotFoundError
        If no matching profile file exists. The message lists the profiles that
        *are* available to make the typo obvious.
    ValueError
        If the file is not valid JSON or does not contain a JSON object.
    """
    path = _profile_path(name)
    if not path.is_file():
        known = ", ".join(available_profiles()) or "(none found)"
        raise FileNotFoundError(
            f"profile {name!r} not found at {path}. Available profiles: {known}"
        )

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:  # noqa: PERF203 - clearer error site
        raise ValueError(f"profile {path} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"profile {path} must contain a JSON object, got {type(data).__name__}"
        )
    return data


def available_profiles() -> list[str]:
    """Return the sorted names (without extension) of all profiles on disk."""
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


if __name__ == "__main__":
    import sys

    requested = sys.argv[1] if len(sys.argv) > 1 else "founder_edtech"
    print(json.dumps(load_profile(requested), indent=2))
