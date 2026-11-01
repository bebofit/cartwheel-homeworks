"""Sample manifest: which traces are in the review set, in which batch, and why.

The manifest records traces, because a trace is the unit that is reviewed and
labeled. A conversation never spans two batches, so no trace can be counted
toward more than one batch even when its conversation has several turns.

Record traces excluded from the review set (for example, the traces used to
evaluate the default Langfuse view) so later selection skips them:

    uv run python -m analysis.review_app.manifest exclude \
        --scenarios support-0090,support-0036 --reason "Part A interface trial"
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.server import _read_json, _write_json

SCHEMA = "review_app.v1"
DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / "state"

BATCHES: dict[str, dict[str, Any]] = {
    "batch1_uniform": {"label": "Batch 1 · uniform random", "target": 15},
    "batch1_cluster": {"label": "Batch 1 · cluster representatives", "target": 15},
    "batch2_dimension": {"label": "Batch 2 · product dimension", "target": 30},
    "batch3_depth": {"label": "Batch 3 · depth searches and close negatives", "target": 25},
    "batch4_final_uniform": {"label": "Batch 4 · final uniform check", "target": 15},
}


def path(state_dir: Path) -> Path:
    return state_dir / "sample_manifest.json"


def load(state_dir: Path) -> dict[str, Any]:
    data = _read_json(path(state_dir), None)
    if not isinstance(data, dict):
        data = {"schema": SCHEMA, "excluded": [], "items": []}
    data["batches"] = BATCHES
    return data


def save(state_dir: Path, manifest: dict[str, Any]) -> None:
    _write_json(path(state_dir), manifest)


def exclude(manifest: dict[str, Any], scenario_ids: list[str], reason: str) -> None:
    known = {e["scenario_id"] for e in manifest["excluded"]}
    sampled = {i["scenario_id"] for i in manifest["items"]}
    for sid in scenario_ids:
        if sid in sampled:
            raise ValueError(f"{sid} is already in the review set")
        if sid not in known:
            manifest["excluded"].append({"scenario_id": sid, "reason": reason})


def add_items(manifest: dict[str, Any], batch: str, entries: list[dict[str, Any]]) -> None:
    """Add traces to a batch, rejecting anything that would double count.

    Each entry needs ``trace_id``, ``session_id``, ``scenario_id``, and a
    ``reason`` describing why it was selected.
    """
    if batch not in BATCHES:
        raise ValueError(f"unknown batch {batch!r}; expected one of {sorted(BATCHES)}")
    excluded = {e["scenario_id"] for e in manifest["excluded"]}
    traces = {i["trace_id"]: i for i in manifest["items"]}
    session_batch = {i["session_id"]: i["batch"] for i in manifest["items"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for entry in entries:
        for key in ("trace_id", "session_id", "scenario_id", "reason"):
            if not entry.get(key):
                raise ValueError(f"entry is missing {key}: {entry}")
        if entry["scenario_id"] in excluded:
            raise ValueError(f"{entry['scenario_id']} is excluded from the review set")
        if entry["trace_id"] in traces:
            raise ValueError(
                f"trace {entry['trace_id']} is already in {traces[entry['trace_id']]['batch']}"
            )
        other = session_batch.get(entry["session_id"])
        if other and other != batch:
            raise ValueError(
                f"conversation {entry['scenario_id']} already belongs to {other}"
            )
        item = {**entry, "batch": batch, "added_at": now}
        manifest["items"].append(item)
        traces[item["trace_id"]] = item
        session_batch[item["session_id"]] = batch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("exclude", help="exclude scenarios from the review set")
    ex.add_argument("--scenarios", required=True)
    ex.add_argument("--reason", required=True)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args()

    manifest = load(args.state_dir)
    if args.cmd == "exclude":
        ids = [s.strip() for s in args.scenarios.split(",") if s.strip()]
        exclude(manifest, ids, args.reason)
    save(args.state_dir, {k: v for k, v in manifest.items() if k != "batches"})
    print(f"{path(args.state_dir)}: {len(manifest['items'])} items, {len(manifest['excluded'])} excluded")


if __name__ == "__main__":
    main()
