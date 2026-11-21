#!/usr/bin/env python3
"""List the candidates for the workflows.

Reads every candidates/*.json in the data folder, checks it, and prints the job matrices the
workflows fan out over, as lines for $GITHUB_OUTPUT:

  linkedin_searches  one entry per candidate and place: the hourly LinkedIn search workers
  linkedin           one entry per candidate with the hourly LinkedIn report switched on
  fast               one entry per candidate with the fast alert switched on
  sites              one entry per candidate with the career-site scan switched on
  welcome            one entry per candidate whose free trial has just started (the welcome mail)

A candidate whose free trial has ended is left out of every list.

Only candidate ids are printed (the log is public); names and addresses stay in the files.
A file that cannot be read or fails the checks is skipped with a warning, so one bad file never
stops the other candidates.

Usage:  python tools/matrix.py <folder holding candidates/>  [--only id1,id2]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_match_runner as core  # noqa: E402


def read_candidates(folder: Path, only: List[str] | None = None) -> List[Dict[str, Any]]:
    profiles: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted((folder / "candidates").glob("*.json")):
        try:
            profile = core.load_profile(path)
        except ValueError as exc:
            print(f"::warning::{exc}", file=sys.stderr)
            continue
        if profile["id"] != path.stem.lower():
            print(f"::warning::candidate file {path.name}: its id ({profile['id']}) is not its file name; skipped.", file=sys.stderr)
            continue
        if profile["id"] in seen or not profile["enabled"] or (only and profile["id"] not in only):
            continue
        if core.trial_is_over(profile):
            print(f"::notice::The free trial of candidate {profile['id']} has ended; they are not scanned.", file=sys.stderr)
            continue
        seen.add(profile["id"])
        profiles.append(profile)
    return profiles


def matrices(profiles: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {"linkedin_searches": [], "linkedin": [], "fast": [], "sites": [], "welcome": []}
    for profile in profiles:
        entry = {"id": profile["id"]}
        if profile["linkedin"]["enabled"]:
            out["linkedin"].append(entry)
            for place in core.linkedin_places(profile):
                slug = re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")
                out["linkedin_searches"].append({**entry, "place": place, "slug": slug})
        if profile["fast_alert"]["enabled"]:
            out["fast"].append(entry)
        if profile["career_sites"]["enabled"]:
            out["sites"].append(entry)
        if core.welcome_is_due(profile):
            out["welcome"].append(entry)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="List the candidates as job matrices for the workflows")
    parser.add_argument("folder", help="Folder that holds candidates/*.json (the checked-out data repository)")
    parser.add_argument("--only", default="", help="Comma-separated candidate ids; empty = every enabled candidate")
    args = parser.parse_args()
    only = [x.strip().lower() for x in args.only.split(",") if x.strip()]
    profiles = read_candidates(Path(args.folder), only)
    for name, entries in matrices(profiles).items():
        print(f"{name}={json.dumps(entries, separators=(',', ':'))}")
        print(f"{name}_count={len(entries)}")
    print(f"::notice::{len(profiles)} candidate(s) to scan.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
