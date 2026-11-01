"""Search for more instances of ``unverified_order_match``.

A deterministic filter used as a retrieval signal, not a label. A hit is a
turn where the agent presents or acts on an order that came only from
``list_my_orders`` (whose results carry no product or store names), the user
never gave that order id, and no ``find_order`` result tied it to the user's
description. Each hit is saved as a pending search suggestion with facts from
``data/cartwheel.db`` and no verdict; the reviewer accepts or rejects it in the
review interface.

Reads conversations from the running review server, so start it first:

    uv run python -m analysis.review_app.search_unverified_match --run 1            # dry run
    uv run python -m analysis.review_app.search_unverified_match --run 1 --write    # save suggestions
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.review_app.manifest import DEFAULT_STATE_DIR, SCHEMA
from analysis.server import _read_json, _write_json

MODE = "unverified_order_match"
DB = Path(__file__).resolve().parents[2] / "data" / "cartwheel.db"
ACTIONS = {"cancel_order", "issue_refund", "get_order"}

RULES = {
    1: "list_my_orders ran; the reply names, or a later tool call acts on, one of its orders; the user "
    "never gave that order id; no find_order result returned it.",
    2: "as run 1, but an order also counts as presented when the reply gives its date or total without the "
    "id; skipped when the reply lists several orders or asks the user to choose or confirm, or when the "
    "user said 'today' or 'this morning' and the order was placed on the world date.",
}
WORLD_DATE = "2026-07-01"
ASKS = re.compile(r"which (order|one)|specify|confirm (if|whether|that)|let me know the specific", re.I)
SAME_DAY = re.compile(r"\btoday\b|this morning|before we opened", re.I)


def spelled(iso: str | None) -> list[str]:
    if not iso:
        return []
    d = datetime.strptime(iso, "%Y-%m-%d")
    return [f"{d:%B} {d.day}, {d.year}", f"{d:%B} {d.day}"]


def described(order: dict[str, Any], reply: str) -> bool:
    dates = [s for k in ("ordered_at", "shipped_at", "delivered_at") for s in spelled(order.get(k))]
    total = order.get("total_usd")
    amounts = [f"${total:.2f}", f"${total:g}"] if isinstance(total, (int, float)) else []
    return any(x in reply for x in dates + amounts)


def get(base: str, path: str) -> Any:
    with urllib.request.urlopen(base + path) as r:
        return json.load(r)


def product_facts(product_id: int) -> str:
    with sqlite3.connect(DB) as db:
        row = db.execute(
            "select p.title, s.name from products p join stores s on s.id = p.store_id where p.id = ?",
            (product_id,),
        ).fetchone()
    return f"{row[0]} from {row[1]}" if row else f"product {product_id} (not in the database)"


def hits_in(conv: dict[str, Any], run: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    said = ""
    matched: set[int] = set()
    for turn in conv["turns"]:
        said += " " + turn["user"]
        listed: dict[int, dict[str, Any]] = {}
        acted: set[int] = set()
        for step in turn["steps"]:
            if step["kind"] != "tool":
                continue
            result = step.get("result") or {}
            orders = [o for o in result.get("orders") or [] if isinstance(o, dict)]
            if step["name"] == "find_order":
                matched |= {o["order_id"] for o in orders}
            elif step["name"] == "list_my_orders":
                listed.update({o["order_id"]: o for o in orders})
            elif step["name"] in ACTIONS:
                oid = (step.get("arguments") or {}).get("order_id")
                if isinstance(oid, int):
                    acted.add(oid)
        turn_hits = []
        for oid, order in listed.items():
            named = re.search(rf"\b{oid}\b", turn["final"]) is not None
            by_details = run >= 2 and not named and described(order, turn["final"])
            if not (named or by_details or oid in acted):
                continue
            if re.search(rf"\b{oid}\b", said) or oid in matched:
                continue
            if run >= 2 and SAME_DAY.search(said) and order.get("ordered_at") == WORLD_DATE:
                continue
            how = "named in the reply" if named else "described by date or total" if by_details else "acted on by a later tool call"
            turn_hits.append(
                {
                    "turn": turn,
                    "order_id": oid,
                    "how": how,
                    "facts": product_facts(order["product_id"]),
                    "ordered_at": order.get("ordered_at"),
                }
            )
        presented = [h for h in turn_hits if h["how"] != "acted on by a later tool call"]
        if run >= 2 and not acted & {h["order_id"] for h in turn_hits} and (
            len(presented) > 1 or ASKS.search(turn["final"])
        ):
            continue
        found += turn_hits
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=int, choices=sorted(RULES), required=True)
    parser.add_argument("--server", default="http://127.0.0.1:8030")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--write", action="store_true", help="save hits as pending search suggestions")
    args = parser.parse_args()

    manifest = _read_json(args.state_dir / "sample_manifest.json", {"items": [], "excluded": []})
    sampled = {i["trace_id"] for i in manifest["items"]}
    excluded = {e["scenario_id"] for e in manifest["excluded"]}
    patterns = _read_json(args.state_dir / "patterns.json", {"modes": []})
    mode = next(m for m in patterns["modes"] if m["name"] == MODE)
    known = {s["trace_id"] for s in mode["supporting"]}
    earlier = {
        s["trace_id"]: s.get("status") for s in _read_json(args.state_dir / "suggestions.json", {"suggestions": []})["suggestions"]
        if s.get("source") == "search" and s.get("mode") == MODE and s.get("search_run") != args.run
    }

    by_trace: dict[str, dict[str, Any]] = {}
    for summary in get(args.server, "/api/conversations"):
        if summary["scenario_id"] in excluded:
            continue
        conv = get(args.server, "/api/conversation?id=" + summary["session_id"])
        for h in hits_in(conv, args.run):
            entry = by_trace.setdefault(h["turn"]["trace_id"], {"conv": conv, "turn": h["turn"], "orders": []})
            entry["orders"].append(h)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = []
    print(f"search {args.run} for {MODE}: {RULES[args.run]}\n")
    for tid, entry in by_trace.items():
        conv, turn, orders = entry["conv"], entry["turn"], entry["orders"]
        tag = ("already a positive" if tid in known
               else f"decided in an earlier search: {earlier[tid]}" if tid in earlier
               else "sampled" if tid in sampled else "outside sample")
        listing = "; ".join(
            f"order {h['order_id']} ({h['how']}; ordered {h['ordered_at']}) is {h['facts']}" for h in orders
        )
        print(f"{conv['scenario_id']}  turn {conv['turns'].index(turn) + 1}/{len(conv['turns'])}  "
              f"[{tag}]  {listing}")
        if tid in known or tid in earlier:
            continue
        ids = ", ".join(str(h["order_id"]) for h in orders)
        new.append(
            {
                "id": f"search{args.run}-{tid[:12]}",
                "source": "search",
                "search_run": args.run,
                "mode": MODE,
                "rule": RULES[args.run],
                "purpose": "retrieval signal; the reviewer decides whether the mode applies",
                "created_at": now,
                "trace_id": tid,
                "session_id": conv["session_id"],
                "scenario_id": conv["scenario_id"],
                "kind": "note",
                "field": "turn",
                "quote": "",
                "note": f"{MODE}: order {ids} is presented or acted on as the user's item, "
                f"and no tool result shows its product or store.",
                "evidence": f"User said: \"{' '.join(turn['user'].split())[:220]}\". In data/cartwheel.db: "
                f"{listing}. These came from list_my_orders, which returns no product names. "
                f"Trace is {'in the sample' if tid in sampled else 'outside the sample (context or unsampled)'}.",
                "status": "pending",
            }
        )
    print(f"\n{len(by_trace)} traces hit, {len(new)} new suggestions")
    if not args.write:
        print("dry run: nothing saved. Re-run with --write to save the suggestions.")
        return
    path = args.state_dir / "suggestions.json"
    data = _read_json(path, {"schema": SCHEMA, "suggestions": []})
    have = {s["id"] for s in data["suggestions"]}
    added = [s for s in new if s["id"] not in have]
    data["suggestions"] += added
    _write_json(path, data)
    print(f"saved {len(added)} search suggestions to {path}")


if __name__ == "__main__":
    main()
