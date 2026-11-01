"""Select Batch 1: 15 uniformly sampled traces and 15 cluster representatives.

Sampling is per trace (a turn); the rest of a sampled conversation is shown as
context. Clustering uses only what is in the trace (role, turn position, tool
path, tool error codes, message lengths) and never a judgment of the outcome.

    uv run python -m analysis.review_app.select_batch1            # dry run
    uv run python -m analysis.review_app.select_batch1 --write    # save to the manifest
"""

from __future__ import annotations

import argparse
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

from analysis.helpers.selection import _kmeans, _standardize
from analysis.review_app import loader, manifest
from observability.instrument import load_env

SEED = 20261029
N_UNIFORM = 15
N_CLUSTER = 15
N_CLUSTERS = 15
ROLES = ("shopper", "merchant", "support")
TOOLS = (
    "search_help_center", "get_policy", "search_products", "get_order", "list_my_orders",
    "find_order", "lookup_store", "issue_refund", "cancel_order", "escalate_to_human",
)


def trace_rows(conversations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for conv in conversations.values():
        n = len(conv["turns"])
        for i, turn in enumerate(conv["turns"]):
            tools = [s for s in turn["steps"] if s["kind"] == "tool"]
            names = {s["name"] for s in tools}
            rows.append(
                {
                    "trace_id": turn["trace_id"],
                    "session_id": conv["session_id"],
                    "scenario_id": conv["scenario_id"],
                    "role": conv["role"],
                    "turn": i + 1,
                    "turns": n,
                    "tools": sorted(names),
                    "tool_calls": len(tools),
                    "tool_error": any(s["badge"]["tone"] == "error" for s in tools),
                    "user_len": len(turn["user"]),
                    "reply_len": len(turn["final"]),
                }
            )
    rows.sort(key=lambda r: r["trace_id"])
    return rows


def features(row: dict[str, Any]) -> list[float]:
    return [
        *(float(row["role"] == r) for r in ROLES),
        float(row["turn"] > 1),
        float(row["turns"]),
        *(float(t in row["tools"]) for t in TOOLS),
        float(row["tool_calls"]),
        float(row["tool_error"]),
        math.log1p(row["user_len"]),
        math.log1p(row["reply_len"]),
    ]


def describe(members: list[dict[str, Any]]) -> str:
    n = len(members)
    role, count = Counter(m["role"] for m in members).most_common(1)[0]
    tool_share = Counter(t for m in members for t in m["tools"])
    common = [t for t, c in tool_share.most_common() if c / n >= 0.5]
    followups = sum(m["turn"] > 1 for m in members) / n
    errors = sum(m["tool_error"] for m in members) / n
    parts = [f"{count / n:.0%} {role}", f"tools: {' + '.join(common) or 'mixed or none'}"]
    if followups >= 0.5:
        parts.append(f"{followups:.0%} follow-up turns")
    if errors >= 0.3:
        parts.append(f"{errors:.0%} with a tool error")
    return f"{n} traces; " + "; ".join(parts)


def select(rows: list[dict[str, Any]], used_sessions: set[str]) -> list[tuple[str, dict, str]]:
    rng = random.Random(SEED)
    uniform = rng.sample(rows, N_UNIFORM)
    picks = [("batch1_uniform", r, f"uniform random draw (seed {SEED}) from {len(rows)} eligible traces") for r in uniform]
    used = used_sessions | {r["session_id"] for r in uniform}

    vectors = _standardize([features(r) for r in rows])
    assign = _kmeans(vectors, k=N_CLUSTERS, seed=SEED)
    clusters: dict[int, list[int]] = {}
    for i, c in enumerate(assign):
        clusters.setdefault(c, []).append(i)
    ranked = {}
    for c, idx in clusters.items():
        centre = [sum(vectors[i][d] for i in idx) / len(idx) for d in range(len(vectors[0]))]
        dist = lambda i: math.dist(vectors[i], centre)  # noqa: E731
        ranked[c] = sorted(idx, key=dist)

    order = sorted(clusters, key=lambda c: -len(clusters[c]))
    base, extra = divmod(N_CLUSTER, len(order))
    quota = {c: base + (rank < extra) for rank, c in enumerate(order)}
    chosen: list[tuple[int, dict]] = []
    for c in order:
        for i in ranked[c]:
            if sum(1 for cc, _ in chosen if cc == c) >= quota[c]:
                break
            if rows[i]["session_id"] in used:
                continue
            chosen.append((c, rows[i]))
            used.add(rows[i]["session_id"])
    for c, row in chosen:
        desc = describe([rows[i] for i in clusters[c]])
        picks.append(("batch1_cluster", row, f"closest to centre of cluster {c} ({desc})"))
    return picks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["langfuse", "export"], default="langfuse")
    parser.add_argument("--state-dir", type=Path, default=manifest.DEFAULT_STATE_DIR)
    parser.add_argument("--write", action="store_true", help="save the picks to the manifest")
    args = parser.parse_args()

    load_env()
    state = manifest.load(args.state_dir)
    if any(i["batch"].startswith("batch1") for i in state["items"]):
        raise SystemExit("Batch 1 is already in the manifest; refusing to select it twice.")
    records, source_note = loader.load_records(args.source)
    excluded = {e["scenario_id"] for e in state["excluded"]}
    records = [
        r for r in records
        if str(r["meta"].get("scenario_id", "")).startswith("support-")
        and r["meta"]["scenario_id"] not in excluded
    ]
    rows = trace_rows(loader.build_conversations(records, {}))
    used = {i["session_id"] for i in state["items"]}
    picks = select([r for r in rows if r["session_id"] not in used], used)

    print(f"source: {source_note}; eligible traces: {len(rows)}\n")
    for batch, row, reason in picks:
        print(
            f"{batch:15} {row['scenario_id']}  turn {row['turn']}/{row['turns']}  "
            f"{row['role']:8} {', '.join(row['tools']) or 'no tools'}"
        )
    print("\nclusters:")
    for reason in sorted({r for b, _, r in picks if b == "batch1_cluster"}):
        print(" ", reason)

    if not args.write:
        print("\ndry run: nothing saved. Re-run with --write to save these picks.")
        return
    for batch in ("batch1_uniform", "batch1_cluster"):
        manifest.add_items(
            state,
            batch,
            [
                {k: row[k] for k in ("trace_id", "session_id", "scenario_id")} | {"reason": reason}
                for b, row, reason in picks
                if b == batch
            ],
        )
    manifest.save(args.state_dir, {k: v for k, v in state.items() if k != "batches"})
    print(f"\nsaved {len(picks)} picks to {manifest.path(args.state_dir)}")


if __name__ == "__main__":
    main()
