#!/usr/bin/env python3
"""
engine/fanout.py
================

The multi-account / multi-clip **fan-out** engine: turn ONE source video into
MANY branded vertical shorts in a single run.

Given a source and a ``jobs.json`` describing N clips and M accounts, this
orchestrator produces up to N x M outputs by invoking the existing
``engine/build_short.py`` once per ``(clip, account)`` pair and collecting the
results. It is a *thin orchestrator*: it never re-implements cutting, grading,
captioning, or loudness — all of that stays in ``build_short.py`` (which it shells
out to via the project venv). fanout's only jobs are:

  1. Expand ``clips x accounts`` into a job matrix.
  2. For each job, materialize a per-job *ranges EDL* (so build_short cuts just
     that clip) and pick the account's brand kit / profile / CTA / keyword.
  3. Run build_short.py for the job, writing ``<out-dir>/<account>/<clip>.mp4``.
  4. Optionally post-process a job into a seamless loop (see "Loop handling").
  5. Record every output + its params (and any error) in
     ``<out-dir>/manifest.json``.

It is **robust**: a single failed job is logged and recorded in the manifest,
but the run continues. The process exits non-zero only if at least one job
failed (so CI can gate on it), and always prints ``N succeeded / M failed``.

Loop handling
-------------
Per-account ``"loop": true`` requests a seamlessly-looping output. If a future
``build_short.py`` grows a ``--loop`` flag, fanout passes it through directly
(detected at runtime by parsing ``build_short.py --help``). Until then, fanout
post-processes the rendered clip with ``engine/loop.make_seamless`` (crossfade by
default). Either way the account just sets ``loop: true`` and gets a looping clip;
the mechanism is an implementation detail.

jobs.json schema
----------------
.. code-block:: jsonc

    {
      // OPTIONAL. Sub-spans of the source to cut, each becomes one clip family.
      // Omit (or pass []) to use the WHOLE source as a single clip named "full".
      "clips": [
        { "start": 0, "end": 30, "name": "hook" },
        { "start": 45.5, "end": 72, "name": "payoff" }
      ],

      // REQUIRED. One entry per brand/account to render every clip for.
      "accounts": [
        {
          "name": "counza",                 // REQUIRED — output subfolder name
          "brandkit": "counza",             // brand kit (default: account name)
          "profile": "founder_edtech",      // edit profile (default: founder_edtech)
          "cta": "Comment PROFILE for a free review", // CTA line override (optional)
          "keyword": "PROFILE",             // CTA keyword override (optional)
          "loop": true                      // seamless-loop the output (optional)
        },
        {
          "name": "founder",
          "brandkit": "founder",
          "profile": "founder_edtech",
          "cta": "Follow for more",
          "keyword": ""
        }
      ]
    }

Per-account optional loop tuning (used only by the fallback loop path):
``"loop_mode"`` ("crossfade"|"freeze_match", default "crossfade") and
``"loop_duration"`` (blend seconds, default 0.5). These map to
``engine/loop.make_seamless(mode=..., dur=...)``; unrecognized modes there fall
back to "crossfade".

CLI
---
    fanout.py --source IN.mp4 --jobs jobs.json --out-dir OUT/
    fanout.py --source IN.mp4 --transcript words.json --jobs jobs.json --out-dir OUT/

``--transcript`` (optional) is forwarded to every build_short invocation so the
source is transcribed at most once by the caller instead of once per job. If
omitted, build_short transcribes per job (cached after the first by WhisperX).

Output layout
-------------
    OUT/
      manifest.json                 # every job: params, status, output, error
      <account>/<clip>.mp4          # the rendered short
      <account>/<clip>.edl.json     # build_short's extended-EDL artifact
      _edls/<account>__<clip>.ranges.json   # the per-job ranges EDL fanout fed in
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Resolve sibling modules / tools. fanout lives in engine/ next to build_short
# and loop; resolve them by path so cwd never matters.
# --------------------------------------------------------------------------- #
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

BUILD_SHORT = os.path.join(_ENGINE_DIR, "build_short.py")

# Render via the project venv (has ffmpeg/torch/etc). Honour an override, then
# the documented venv, then whatever interpreter is running fanout.
VENV_PY = os.environ.get(
    "VIDEO_USE_PY", "/Users/vanshgupta/Developer/video-use/.venv/bin/python"
)


class FanoutError(RuntimeError):
    """Fatal, run-level error (bad jobs.json, missing source, etc.)."""


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def _python_exe() -> str:
    """Interpreter used to run build_short.py."""
    return VENV_PY if os.path.isfile(VENV_PY) else sys.executable


def _fmt_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _slug(text: str, *, fallback: str = "item") -> str:
    """Filesystem-safe slug (keep alnum / dash / underscore, collapse rest)."""
    s = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(text).strip())
    s = s.strip("_")
    return s or fallback


def _tail(text: Optional[str], n: int = 25) -> str:
    if not text:
        return ""
    return "\n".join(text.rstrip().splitlines()[-n:])


# --------------------------------------------------------------------------- #
# jobs.json parsing / validation
# --------------------------------------------------------------------------- #
def load_jobs(jobs_path: str) -> Dict[str, Any]:
    """Read + validate jobs.json into {'clips': [...], 'accounts': [...]}.

    ``clips`` is optional (defaults to a single whole-source clip named "full").
    ``accounts`` is required and must be a non-empty list, each with a ``name``.
    Unknown keys are preserved/ignored. Raises FanoutError on anything fatal.
    """
    if not os.path.isfile(jobs_path):
        raise FanoutError(f"--jobs not found: {jobs_path!r}")
    with open(jobs_path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise FanoutError(f"--jobs {jobs_path!r} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FanoutError(f"--jobs {jobs_path!r} must be a JSON object")

    # ---- accounts (required) ----
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise FanoutError(
            f"--jobs {jobs_path!r} must have a non-empty 'accounts' array"
        )
    norm_accounts: List[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for i, acc in enumerate(accounts):
        if not isinstance(acc, dict):
            raise FanoutError(f"accounts[{i}] must be an object, got {acc!r}")
        name = acc.get("name")
        if not name or not isinstance(name, str):
            raise FanoutError(f"accounts[{i}] missing a string 'name'")
        slug = _slug(name, fallback=f"account{i}")
        if slug in seen_names:
            raise FanoutError(
                f"accounts[{i}] name {name!r} collides with another account "
                f"(slug {slug!r}); account names must be unique"
            )
        seen_names.add(slug)
        norm_accounts.append({
            "name": name,
            "slug": slug,
            "brandkit": acc.get("brandkit") or name,
            "profile": acc.get("profile") or "founder_edtech",
            "cta": acc.get("cta"),
            "keyword": acc.get("keyword"),
            "loop": bool(acc.get("loop", False)),
            "loop_mode": acc.get("loop_mode", "crossfade"),
            "loop_duration": acc.get("loop_duration", 0.5),
        })

    # ---- clips (optional) ----
    clips_in = data.get("clips")
    norm_clips: List[Dict[str, Any]] = []
    if clips_in in (None, []):
        norm_clips = [{"name": "full", "slug": "full", "start": None, "end": None}]
    elif isinstance(clips_in, list):
        seen_clip: set[str] = set()
        for i, clip in enumerate(clips_in):
            if not isinstance(clip, dict):
                raise FanoutError(f"clips[{i}] must be an object, got {clip!r}")
            if "start" not in clip or "end" not in clip:
                raise FanoutError(f"clips[{i}] needs both 'start' and 'end'")
            try:
                start = float(clip["start"])
                end = float(clip["end"])
            except (TypeError, ValueError) as exc:
                raise FanoutError(f"clips[{i}] start/end must be numbers: {exc}") from exc
            if end <= start or start < 0:
                raise FanoutError(
                    f"clips[{i}] invalid span start={start} end={end} "
                    f"(need 0 <= start < end)"
                )
            name = clip.get("name") or f"clip{i+1}"
            slug = _slug(name, fallback=f"clip{i+1}")
            if slug in seen_clip:
                raise FanoutError(
                    f"clips[{i}] name {name!r} collides (slug {slug!r}); "
                    f"clip names must be unique"
                )
            seen_clip.add(slug)
            norm_clips.append({"name": name, "slug": slug, "start": start, "end": end})
    else:
        raise FanoutError(f"--jobs 'clips' must be an array or omitted")

    return {"clips": norm_clips, "accounts": norm_accounts}


# --------------------------------------------------------------------------- #
# build_short.py capability probe
# --------------------------------------------------------------------------- #
def build_short_supports_loop() -> bool:
    """True iff ``build_short.py --help`` advertises a ``--loop`` option.

    Lets fanout pass ``--loop`` straight through once the Integrate phase adds it
    to build_short, without fanout needing a code change. Failure to probe is
    treated as "no --loop" (we fall back to engine/loop), never fatal.
    """
    try:
        proc = subprocess.run(
            [_python_exe(), BUILD_SHORT, "--help"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return "--loop" in (proc.stdout or "")


# --------------------------------------------------------------------------- #
# Per-job ranges EDL
# --------------------------------------------------------------------------- #
def write_ranges_edl(
    *, source: str, clip: Dict[str, Any], account: Dict[str, Any], edl_dir: str
) -> Optional[str]:
    """Write a per-job extended-EDL (single range = this clip) and return path.

    Returns None for a whole-source clip (no ``--ranges`` needed — build_short
    treats the absence as the trivial full-source range). When start/end are
    present we emit a one-range EDL keyed to the source basename, matching the
    schema build_short.load_ranges() consumes (it reads the top-level ``ranges``
    array; ``source`` per range is advisory here since build_short cuts the file
    passed via --source).
    """
    if clip.get("start") is None or clip.get("end") is None:
        return None
    os.makedirs(edl_dir, exist_ok=True)
    edl = {
        "_engine": "fanout.py per-job ranges",
        "account": account["name"],
        "sources": {os.path.basename(source): os.path.abspath(source)},
        "ranges": [{
            "source": os.path.basename(source),
            "start": round(float(clip["start"]), 3),
            "end": round(float(clip["end"]), 3),
            "beat": clip["name"],
        }],
    }
    path = os.path.join(edl_dir, f"{account['slug']}__{clip['slug']}.ranges.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(edl, fh, indent=2)
    return path


# --------------------------------------------------------------------------- #
# One job
# --------------------------------------------------------------------------- #
def run_job(
    *,
    source: str,
    transcript: Optional[str],
    clip: Dict[str, Any],
    account: Dict[str, Any],
    out_dir: str,
    edl_dir: str,
    loop_passthrough: bool,
) -> Dict[str, Any]:
    """Render one (clip x account) and return its manifest record.

    Never raises for a per-job failure: any error is captured in the returned
    record's ``status``/``error`` so the run can continue. (It may raise only for
    a programming error, which would be a real bug.)
    """
    acc_dir = os.path.join(out_dir, account["slug"])
    os.makedirs(acc_dir, exist_ok=True)
    out_path = os.path.join(acc_dir, f"{clip['slug']}.mp4")

    record: Dict[str, Any] = {
        "account": account["name"],
        "clip": clip["name"],
        "output": out_path,
        "params": {
            "brandkit": account["brandkit"],
            "profile": account["profile"],
            "cta": account["cta"],
            "keyword": account["keyword"],
            "loop": account["loop"],
            "clip_start": clip.get("start"),
            "clip_end": clip.get("end"),
        },
        "status": "pending",
        "error": None,
        "duration_s": None,
    }

    # 1) per-job ranges EDL (None for whole-source).
    try:
        ranges_path = write_ranges_edl(
            source=source, clip=clip, account=account, edl_dir=edl_dir
        )
    except Exception as exc:  # noqa: BLE001 — record + skip, don't abort run
        record["status"] = "failed"
        record["error"] = f"could not write per-job EDL: {exc}"
        print(f"  [FAIL] {account['name']}/{clip['name']}: {record['error']}",
              file=sys.stderr)
        return record
    record["ranges_edl"] = ranges_path

    # 2) assemble the build_short command.
    cmd = [_python_exe(), BUILD_SHORT, "--source", source, "-o", out_path,
           "--brandkit", account["brandkit"], "--profile", account["profile"]]
    if transcript:
        cmd += ["--transcript", transcript]
    if ranges_path:
        cmd += ["--ranges", ranges_path]
    if account["cta"] is not None:
        cmd += ["--cta", account["cta"]]
    if account["keyword"] is not None:
        cmd += ["--keyword", account["keyword"]]

    do_fallback_loop = False
    if account["loop"]:
        if loop_passthrough:
            cmd += ["--loop"]  # build_short owns the loop in this case
        else:
            do_fallback_loop = True  # post-process below
    record["command"] = _fmt_cmd(cmd)

    # 3) run build_short.
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        record["status"] = "failed"
        record["error"] = f"could not launch build_short.py: {exc}"
        print(f"  [FAIL] {account['name']}/{clip['name']}: {record['error']}",
              file=sys.stderr)
        return record

    if proc.returncode != 0:
        record["status"] = "failed"
        record["error"] = (
            f"build_short.py exited {proc.returncode}\n{_tail(proc.stderr)}"
        )
        print(f"  [FAIL] {account['name']}/{clip['name']}: build_short exit "
              f"{proc.returncode}", file=sys.stderr)
        return record

    if not os.path.isfile(out_path):
        record["status"] = "failed"
        record["error"] = "build_short.py exited 0 but produced no output file"
        print(f"  [FAIL] {account['name']}/{clip['name']}: {record['error']}",
              file=sys.stderr)
        return record

    # build_short writes a sidecar extended-EDL next to the output.
    sidecar = os.path.splitext(out_path)[0] + ".edl.json"
    if os.path.isfile(sidecar):
        record["build_edl"] = sidecar

    # 4) fallback loop post-process (only when build_short lacks --loop).
    if do_fallback_loop:
        try:
            import loop  # local import so a loop.py issue can't break non-loop jobs
            tmp_looped = os.path.splitext(out_path)[0] + ".looped.mp4"
            loop.make_seamless(
                out_path, tmp_looped,
                mode=account["loop_mode"],
                dur=float(account["loop_duration"]),
            )
            os.replace(tmp_looped, out_path)  # atomic swap into final name
            record["loop_applied"] = account["loop_mode"]
        except Exception as exc:  # noqa: BLE001
            # The base clip rendered fine; loop is an enhancement. Record a
            # partial success rather than discarding a good render.
            record["status"] = "succeeded_no_loop"
            record["error"] = f"loop post-process failed: {exc}"
            record["duration_s"] = round(time.time() - t0, 2)
            print(f"  [WARN] {account['name']}/{clip['name']}: rendered OK but "
                  f"loop failed ({exc})", file=sys.stderr)
            return record

    record["status"] = "succeeded"
    record["duration_s"] = round(time.time() - t0, 2)
    print(f"  [ OK ] {account['name']}/{clip['name']} -> {out_path} "
          f"({record['duration_s']}s)", file=sys.stderr)
    return record


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def fanout(args: argparse.Namespace) -> int:
    source = os.path.abspath(args.source)
    if not os.path.isfile(source):
        raise FanoutError(f"--source not found: {source!r}")
    if not os.path.isfile(BUILD_SHORT):
        raise FanoutError(f"build_short.py not found next to fanout.py: {BUILD_SHORT!r}")

    transcript = None
    if args.transcript:
        transcript = os.path.abspath(args.transcript)
        if not os.path.isfile(transcript):
            raise FanoutError(f"--transcript not found: {transcript!r}")

    spec = load_jobs(args.jobs)
    clips, accounts = spec["clips"], spec["accounts"]

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    edl_dir = os.path.join(out_dir, "_edls")

    n_jobs = len(clips) * len(accounts)
    loop_passthrough = build_short_supports_loop()
    any_loop = any(a["loop"] for a in accounts)

    print(f"[fanout] source: {source}", file=sys.stderr)
    print(f"[fanout] {len(clips)} clip(s) x {len(accounts)} account(s) "
          f"= {n_jobs} job(s)", file=sys.stderr)
    if any_loop:
        print(f"[fanout] loop handling: "
              f"{'build_short --loop (passthrough)' if loop_passthrough else 'engine/loop fallback'}",
              file=sys.stderr)

    records: List[Dict[str, Any]] = []
    # Iterate account-major so each account's folder fills in order.
    for account in accounts:
        for clip in clips:
            print(f"[fanout] >>> {account['name']} / {clip['name']}", file=sys.stderr)
            records.append(run_job(
                source=source, transcript=transcript, clip=clip, account=account,
                out_dir=out_dir, edl_dir=edl_dir, loop_passthrough=loop_passthrough,
            ))

    succeeded = [r for r in records if r["status"] in ("succeeded", "succeeded_no_loop")]
    failed = [r for r in records if r["status"] not in ("succeeded", "succeeded_no_loop")]

    manifest = {
        "source": source,
        "transcript": transcript,
        "out_dir": out_dir,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "loop_passthrough": loop_passthrough,
        "counts": {
            "total": len(records),
            "succeeded": len(succeeded),
            "failed": len(failed),
        },
        "clips": clips,
        "accounts": [
            {k: a[k] for k in ("name", "brandkit", "profile", "loop")}
            for a in accounts
        ],
        "outputs": records,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"\n[fanout] manifest -> {manifest_path}")
    print(f"[fanout] {len(succeeded)} succeeded / {len(failed)} failed")
    # Non-zero exit iff a job hard-failed, so callers/CI can gate on it. A
    # render that succeeded but whose loop enhancement failed does NOT fail the
    # run (the clip is usable); it is counted under succeeded.
    return 0 if not failed else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fanout.py",
        description="Fan ONE source video out into MANY branded shorts by "
                    "running engine/build_short.py once per (clip x account).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "jobs.json: { \"clips\": [{\"start\":0,\"end\":30,\"name\":\"hook\"}], "
            "\"accounts\": [{\"name\":\"counza\",\"brandkit\":\"counza\","
            "\"profile\":\"founder_edtech\",\"cta\":\"Comment PROFILE...\","
            "\"keyword\":\"PROFILE\",\"loop\":true}] }  "
            "('clips' optional -> whole source; 'accounts' required)."
        ),
    )
    p.add_argument("--source", required=True, help="source video (required)")
    p.add_argument("--transcript", default=None,
                   help="WhisperX word-level JSON, forwarded to every job so the "
                        "source is transcribed once instead of per job "
                        "(optional; build_short transcribes per job if omitted)")
    p.add_argument("--jobs", required=True,
                   help="jobs.json describing clips x accounts (required)")
    p.add_argument("--out-dir", required=True,
                   help="output directory; outputs land in <out-dir>/<account>/"
                        "<clip>.mp4 and a manifest.json is written here (required)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return fanout(args)
    except FanoutError as exc:
        print(f"\n[fanout] ERROR: {exc}", file=sys.stderr)
        return 2  # 2 = run-level/config error; 1 = some jobs failed; 0 = all ok


if __name__ == "__main__":
    raise SystemExit(main())
