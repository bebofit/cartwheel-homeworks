"""Select Batch 4: 15 traces drawn uniformly at random as a final check.

Batch 4 is drawn after the taxonomy has stabilized, with no filters, so it
shows whether the modes cover traces that no search was aimed at. Each pick is
one random trace from a random conversation that is not already in the review
set; a conversation never spans two batches.

    uv run python -m analysis.review_app.select_batch4            # dry run
    uv run python -m analysis.review_app.select_batch4 --write    # save to the manifest
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from analysis.review_app import loader, manifest
from observability.instrument import load_env

SEED = 20261101
BATCH = "batch4_final_uniform"
N = 15


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["langfuse", "export"], default="langfuse")
    parser.add_argument("--state-dir", type=Path, default=manifest.DEFAULT_STATE_DIR)
    parser.add_argument("--write", action="store_true", help="save the picks to the manifest")
    args = parser.parse_args()

    load_env()
    state = manifest.load(args.state_dir)
    if any(i["batch"] == BATCH for i in state["items"]):
        raise SystemExit("Batch 4 is already in the manifest; refusing to select it twice.")
    records, source_note = loader.load_records(args.source)
    excluded = {e["scenario_id"] for e in state["excluded"]}
    used = {i["session_id"] for i in state["items"]}
    records = [
        r for r in records
        if str(r["meta"].get("scenario_id", "")).startswith("support-")
        and r["meta"]["scenario_id"] not in excluded
        and r["meta"].get("session_id") not in used
    ]
    conversations = sorted(
        loader.build_conversations(records, loader.load_scenarios()).values(),
        key=lambda c: c["session_id"],
    )
    pool_traces = sum(len(c["turns"]) for c in conversations)

    rng = random.Random(SEED)
    picks = []
    for conv in rng.sample(conversations, min(N, len(conversations))):
        index = rng.randrange(len(conv["turns"]))
        turn = conv["turns"][index]
        tools = ", ".join(s["name"] for s in turn["steps"] if s["kind"] == "tool") or "no tools"
        picks.append(
            {
                "trace_id": turn["trace_id"],
                "session_id": conv["session_id"],
                "scenario_id": conv["scenario_id"],
                "reason": f"final uniform check: seeded random draw (seed {SEED}) from "
                f"{len(conversations)} unsampled conversations ({pool_traces} traces)",
                "_show": f"{conv['scenario_id']}  turn {index + 1}/{len(conv['turns'])}  {conv['role']:8} {tools}",
            }
        )

    print(f"source: {source_note}; unsampled conversations: {len(conversations)}\n")
    for p in picks:
        print(p["_show"])
    print(f"\n{len(picks)} picks")
    if not args.write:
        print("dry run: nothing saved. Re-run with --write to save these picks.")
        return
    manifest.add_items(state, BATCH, [{k: v for k, v in p.items() if k != "_show"} for p in picks])
    manifest.save(args.state_dir, {k: v for k, v in state.items() if k != "batches"})
    print(f"saved {len(picks)} picks to {manifest.path(args.state_dir)}")


if __name__ == "__main__":
    main()
