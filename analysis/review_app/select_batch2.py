"""Select Batch 2: 30 traces spread across one product dimension, the request type.

The request type is the scenario's ``tuple.intent`` (what the user asked for),
chosen before any outcome was reviewed. The split is near equal so rare request
types with specific requirements (disputes, account changes, out-of-scope
requests) are covered. Within each type, conversations are drawn at random and
one trace is drawn from each.

    uv run python -m analysis.review_app.select_batch2            # dry run
    uv run python -m analysis.review_app.select_batch2 --write    # save to the manifest
"""

from __future__ import annotations

import argparse
import random
from collections import defaultdict
from pathlib import Path

from analysis.review_app import loader, manifest
from observability.instrument import load_env

SEED = 20261030
BATCH = "batch2_dimension"
ALLOCATION = {
    "refund": 4,
    "order_status": 4,
    "return_eligibility": 4,
    "product_search": 3,
    "cancellation": 3,
    "policy_question": 3,
    "dispute": 3,
    "out_of_scope": 3,
    "account_change": 3,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["langfuse", "export"], default="langfuse")
    parser.add_argument("--state-dir", type=Path, default=manifest.DEFAULT_STATE_DIR)
    parser.add_argument("--write", action="store_true", help="save the picks to the manifest")
    args = parser.parse_args()

    load_env()
    state = manifest.load(args.state_dir)
    if any(i["batch"] == BATCH for i in state["items"]):
        raise SystemExit("Batch 2 is already in the manifest; refusing to select it twice.")
    records, source_note = loader.load_records(args.source)
    excluded = {e["scenario_id"] for e in state["excluded"]}
    used = {i["session_id"] for i in state["items"]}
    scenarios = loader.load_scenarios()
    records = [
        r for r in records
        if str(r["meta"].get("scenario_id", "")).startswith("support-")
        and r["meta"]["scenario_id"] not in excluded
        and r["meta"].get("session_id") not in used
    ]
    conversations = loader.build_conversations(records, scenarios)

    by_intent: dict[str, list[dict]] = defaultdict(list)
    for conv in sorted(conversations.values(), key=lambda c: c["session_id"]):
        intent = (conv["scenario_intent"].get("tuple") or {}).get("intent")
        by_intent[intent].append(conv)

    rng = random.Random(SEED)
    picks = []
    for intent, n in ALLOCATION.items():
        pool = by_intent.get(intent, [])
        traces_in_type = sum(len(c["turns"]) for c in pool)
        for conv in rng.sample(pool, min(n, len(pool))):
            index = rng.randrange(len(conv["turns"]))
            turn = conv["turns"][index]
            picks.append(
                {
                    "trace_id": turn["trace_id"],
                    "session_id": conv["session_id"],
                    "scenario_id": conv["scenario_id"],
                    "reason": f"request type {intent}: seeded random draw (seed {SEED}) from "
                    f"{len(pool)} eligible conversations ({traces_in_type} traces)",
                    "_show": f"{intent:19} {conv['scenario_id']}  turn {index + 1}/{len(conv['turns'])}  "
                    f"{conv['role']:8} {', '.join(sorted({s['name'] for s in turn['steps'] if s['kind'] == 'tool'})) or 'no tools'}",
                }
            )

    print(f"source: {source_note}; eligible conversations: {len(conversations)}\n")
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
