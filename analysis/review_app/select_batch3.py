"""Select Batch 3: 25 traces from depth searches and close negatives.

Each search is a deterministic filter over tool calls and tool results, used as
a retrieval signal for one candidate mode from axial coding round 2. A match is
not a label: every pick is reviewed like any other trace. Some searches look
for likely close negatives (the same situation handled well) so each mode's
boundary gets tested. Within a search, matches are drawn at random with a
fixed seed; a conversation is used at most once.

    uv run python -m analysis.review_app.select_batch3            # dry run
    uv run python -m analysis.review_app.select_batch3 --write    # save to the manifest
"""

from __future__ import annotations

import argparse
import random
import re
from pathlib import Path
from typing import Any, Callable

from analysis.review_app import loader, manifest
from observability.instrument import load_env

SEED = 20261031
BATCH = "batch3_depth"


def tools(turn: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in turn["steps"] if s["kind"] == "tool"]


def called(turn: dict[str, Any], *names: str) -> bool:
    return any(s["name"] in names for s in tools(turn))


def results(turn: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [s.get("result") or {} for s in tools(turn) if s["name"] == name]


def orders(turn: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for s in tools(turn):
        r = s.get("result") or {}
        if isinstance(r.get("order"), dict):
            found.append(r["order"])
        found += [o for o in r.get("orders") or [] if isinstance(o, dict)]
    return found


def intent(conv: dict[str, Any]) -> str | None:
    return (conv["scenario_intent"].get("tuple") or {}).get("intent")


def bad_data(turn: dict[str, Any]) -> bool:
    for s in tools(turn):
        r = s.get("result") or {}
        for p in r.get("products") or r.get("results") or []:
            if isinstance(p, dict) and ((p.get("price_usd") or 0) < 0 or p.get("title") == ""):
                return True
    for o in orders(turn):
        shipped, delivered, ordered = o.get("shipped_at"), o.get("delivered_at"), o.get("ordered_at")
        if shipped and delivered and delivered < shipped:
            return True
        if ordered and shipped and ordered[:4] != shipped[:4]:
            return True
    return False


WINDOW_WORDS = re.compile(r"\breturn|window|days? to|too late|still (send|return)", re.I)
PUSHBACK = re.compile(r"are you sure|that'?s (wrong|not right)|but (i|it|the)|you said|no,|actually", re.I)

Predicate = Callable[[dict[str, Any], int, dict[str, Any]], bool]

SEARCHES: list[tuple[str, int, str, Predicate]] = [
    (
        "return_window_without_order_facts", 3,
        "return-window question where lookup_store or an order lookup is missing",
        lambda c, i, t: WINDOW_WORDS.search(t["user"]) is not None
        and not (called(t, "lookup_store") and called(t, "get_order", "find_order")),
    ),
    (
        "return_window_without_order_facts (close negative)", 2,
        "return-window question where both lookup_store and an order lookup ran",
        lambda c, i, t: WINDOW_WORDS.search(t["user"]) is not None
        and called(t, "lookup_store") and called(t, "get_order", "find_order"),
    ),
    (
        "order_search_gives_up", 4,
        "find_order returned no orders and no list_my_orders call followed",
        lambda c, i, t: any(r.get("orders") == [] for r in results(t, "find_order"))
        and not called(t, "list_my_orders"),
    ),
    (
        "order_search_gives_up (close negative)", 1,
        "find_order returned no orders, then a later find_order or list_my_orders found orders",
        lambda c, i, t: any(r.get("orders") == [] for r in results(t, "find_order"))
        and any(r.get("orders") for r in results(t, "find_order") + results(t, "list_my_orders")),
    ),
    (
        "missing_required_escalation", 2,
        "dispute request with no escalate_to_human call",
        lambda c, i, t: intent(c) == "dispute" and not called(t, "escalate_to_human"),
    ),
    (
        "missing_required_escalation (close negative)", 2,
        "any request where escalate_to_human ran",
        lambda c, i, t: called(t, "escalate_to_human"),
    ),
    (
        "unflagged_bad_tool_data", 4,
        "tool result with a negative price, empty title, or inconsistent order dates",
        lambda c, i, t: bad_data(t),
    ),
    (
        "unexplained_decision", 3,
        "an order lookup returned refund_eligible=false and no escalation followed",
        lambda c, i, t: any(o.get("refund_eligible") is False for o in orders(t))
        and not called(t, "escalate_to_human", "cancel_order"),
    ),
    (
        "singleton: decides an action from the wrong order field", 3,
        "cancellation request on an order whose status is placed",
        lambda c, i, t: intent(c) == "cancellation"
        and any(o.get("status") == "placed" for o in orders(t)),
    ),
    (
        "singleton: abandons tool data to agree with the user", 2,
        "follow-up turn where the user pushes back",
        lambda c, i, t: i > 0 and PUSHBACK.search(t["user"]) is not None,
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["langfuse", "export"], default="langfuse")
    parser.add_argument("--state-dir", type=Path, default=manifest.DEFAULT_STATE_DIR)
    parser.add_argument("--write", action="store_true", help="save the picks to the manifest")
    args = parser.parse_args()

    load_env()
    state = manifest.load(args.state_dir)
    if any(i["batch"] == BATCH for i in state["items"]):
        raise SystemExit("Batch 3 is already in the manifest; refusing to select it twice.")
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

    rng = random.Random(SEED)
    taken: set[str] = set()
    picks = []
    for label, n, signal, match in SEARCHES:
        pool = [
            (c, i, t) for c in conversations if c["session_id"] not in taken
            for i, t in enumerate(c["turns"]) if match(c, i, t)
        ]
        by_conv: dict[str, list] = {}
        for c, i, t in pool:
            by_conv.setdefault(c["session_id"], []).append((c, i, t))
        chosen = rng.sample(sorted(by_conv), min(n, len(by_conv)))
        if len(chosen) < n:
            print(f"warning: {label}: only {len(chosen)} of {n} matches")
        for sid in chosen:
            c, i, t = rng.choice(by_conv[sid])
            taken.add(sid)
            picks.append(
                {
                    "trace_id": t["trace_id"],
                    "session_id": sid,
                    "scenario_id": c["scenario_id"],
                    "reason": f"depth search for {label}: {signal} "
                    f"(seed {SEED}; {len(by_conv)} matching conversations)",
                    "_show": f"{label[:52]:52} {c['scenario_id']}  turn {i + 1}/{len(c['turns'])}  "
                    f"{c['role']:8} {', '.join(s['name'] for s in tools(t)) or 'no tools'}",
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
