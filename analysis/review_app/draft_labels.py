"""Draft Part E labels: one present/absent judgment per sampled trace and final mode.

Drafts are not labels. A draft is ``present`` only when an accepted annotation on
that trace supports the mode in ``patterns.json``; every other pair is drafted
``absent``. Deterministic signals then flag pairs for the reviewer: a signal that
fires on a pair drafted absent (a possible later failure, which open coding's
first-failure rule would not have noted), and known conflicts recorded in the
taxonomy. Nothing is written to Langfuse or ``labels/``.

Reads conversations from the running review server:

    uv run python -m analysis.review_app.draft_labels            # dry run
    uv run python -m analysis.review_app.draft_labels --write    # save label_drafts.json
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analysis.review_app.manifest import DEFAULT_STATE_DIR, SCHEMA
from analysis.review_app.search_unverified_match import get, hits_in
from analysis.server import _read_json, _write_json

POLICY_TOOLS = {"get_policy", "search_help_center"}
POLICY_CLAIM = re.compile(
    r"\b(\d+)\s*(?:-\s*\d+\s*)?(?:business\s+)?[- ]?days?\b|\b(restocking fee|in transit)\b",
    re.I,
)
WINDOW_CLAIM = re.compile(r"return window|\b\d+[- ]day\b|within \w+ days|still (?:eligible|within)|too late|missed", re.I)
ESCALATION_NEED = re.compile(
    r"\bdispute|chargeback|fraud|unauthori[sz]ed|bank account|payout account|password|"
    r"change (?:my|the) (?:email|address|name)|delete (?:my )?account",
    re.I,
)
NO_ORDER = re.compile(r"(?:couldn't|could not|can't|cannot|unable to|wasn't able to) (?:find|locate)|no (?:matching )?orders?", re.I)


def tools_until(conv: dict[str, Any], turn_index: int) -> list[dict[str, Any]]:
    return [s for t in conv["turns"][: turn_index + 1] for s in t["steps"] if s["kind"] == "tool"]


def bad_values(result: Any) -> list[str]:
    found: list[str] = []
    prices: dict[Any, set[Any]] = {}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, (int, float)) and ("price" in k or "total" in k) and v < 0:
                    found.append(f"{k}={v}")
                if k == "title" and isinstance(v, str) and not v.strip():
                    found.append("empty title")
            if x.get("shipped_at") and x.get("delivered_at") and x["delivered_at"] < x["shipped_at"]:
                found.append(f"delivered {x['delivered_at']} before shipped {x['shipped_at']}")
            pid = x.get("product_id")
            price = x.get("price_usd", x.get("price_cents"))
            if pid is not None and price is not None and "order_id" not in x:
                prices.setdefault(pid, set()).add(price)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(result)
    found += [f"product {pid} at {len(p)} prices" for pid, p in prices.items() if len(p) > 1]
    return found


def signals(conv: dict[str, Any], i: int) -> dict[str, str]:
    """Deterministic retrieval signals per mode for turn ``i``; a hint, never a label."""
    turn = conv["turns"][i]
    reply, said = turn["final"], " ".join(t["user"] for t in conv["turns"][: i + 1])
    upto = tools_until(conv, i)
    names = {s["name"] for s in upto}
    this_turn = [s for s in turn["steps"] if s["kind"] == "tool"]
    out: dict[str, str] = {}

    tool_text = json.dumps([s.get("result") for s in upto]).lower()
    claims = [
        m.group(0) for m in POLICY_CLAIM.finditer(reply)
        if not (m.group(1) and re.search(rf"\b{m.group(1)}\b", tool_text)) and not (m.group(2) and m.group(2).lower() in tool_text)
    ]
    if claims and not names & POLICY_TOOLS:
        out["ungrounded_policy_answer"] = f"reply states {claims[0]!r}, not in any tool result; no get_policy or search_help_center so far"
    if ESCALATION_NEED.search(said) and "escalate_to_human" not in names:
        out["missing_required_escalation"] = "user raised a dispute or account change; no escalate_to_human so far"
    if WINDOW_CLAIM.search(reply) and not ({"lookup_store"} <= names and names & {"get_order", "find_order"}):
        out["return_window_without_order_facts"] = "reply judges a return window without both lookup_store and an order lookup"
    bad = [b for s in this_turn for b in bad_values(s.get("result"))]
    if bad:
        out["unflagged_bad_tool_data"] = "tool result this turn has " + "; ".join(sorted(set(bad))[:3])
    finds = [s for s in this_turn if s["name"] == "find_order"]
    if finds and not any((s.get("result") or {}).get("orders") for s in finds) and "list_my_orders" not in {
        s["name"] for s in this_turn
    } and NO_ORDER.search(reply):
        out["concludes_no_order_after_one_failed_search"] = "find_order returned nothing, no list_my_orders, reply says no order"
    if any(h["turn"] is turn for h in hits_in(conv, 2)):
        out["unverified_order_match"] = "search 2 rule matches this turn"
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8030")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--write", action="store_true", help="save analysis/state/label_drafts.json")
    args = parser.parse_args()

    items = _read_json(args.state_dir / "sample_manifest.json", {"items": []})["items"]
    patterns = _read_json(args.state_dir / "patterns.json", {"modes": []})
    modes = [m["name"] for m in patterns["modes"]]
    annotations = {
        a["id"]: a for a in _read_json(args.state_dir / "annotations.json", {"annotations": []})["annotations"]
        if a.get("status") == "active"
    }
    support: dict[tuple[str, str], list[str]] = {}
    close_neg: set[tuple[str, str]] = set()
    for m in patterns["modes"]:
        for s in m["supporting"]:
            if s.get("annotation_id") in annotations:
                support.setdefault((s["trace_id"], m["name"]), []).append(s["annotation_id"])
        for s in m.get("close_negatives", []):
            close_neg.add((s["trace_id"], m["name"]))
    conflicts = {
        ("support-0033", "unverified_order_match"): "accepted in search 1; the revised definition excludes it (no order presented as the user's)",
        ("support-0156", "unverified_order_match"): "accepted in search 1; the revised definition excludes it (no order presented as the user's)",
        ("support-0200", "unflagged_bad_tool_data"): "your note says 'bad data', but the taxonomy lists 0200 as a close negative",
        ("support-0164", "missing_required_escalation"): "spec gap: ESC-2 says escalate account changes; cw-account-security says use account settings",
        ("support-0165", "missing_required_escalation"): "spec gap: ESC-2 says escalate account changes; cw-account-security says use account settings",
        ("support-0167", "missing_required_escalation"): "spec gap: ESC-2 says escalate account changes; cw-account-security says use account settings",
        ("support-0077", "ungrounded_policy_answer"): "spec gap (premise verification); no mode yet covers it",
    }

    convs: dict[str, dict[str, Any]] = {}
    drafts = []
    for item in items:
        sid = item["session_id"]
        conv = convs.get(sid) or convs.setdefault(sid, get(args.server, "/api/conversation?id=" + sid))
        i = conv["trace_ids"].index(item["trace_id"])
        sig = signals(conv, i)
        notes = [a for a in annotations.values() if a["trace_id"] == item["trace_id"] and a["kind"] == "note"]
        for mode in modes:
            key = (item["trace_id"], mode)
            present = key in support
            flags = []
            if sig.get(mode) and not present:
                flags.append("signal: " + sig[mode])
            if (item["scenario_id"], mode) in conflicts:
                flags.append("conflict: " + conflicts[(item["scenario_id"], mode)])
            drafts.append({
                "trace_id": item["trace_id"],
                "session_id": sid,
                "scenario_id": item["scenario_id"],
                "batch": item["batch"],
                "turn": i + 1,
                "mode": mode,
                "draft": 1 if present else 0,
                "basis": (
                    "accepted annotation " + ", ".join(support[key]) if present
                    else "close negative in the taxonomy" if key in close_neg
                    else "annotated failure belongs to another mode or singleton" if notes
                    else "reviewed with no failure" if any(
                        a["trace_id"] == item["trace_id"] and a["kind"] == "no_failure" for a in annotations.values())
                    else "no annotation"
                ),
                "flags": flags,
                "status": "needs_review" if flags else "draft",
            })

    present = Counter(d["mode"] for d in drafts if d["draft"])
    flagged = [d for d in drafts if d["flags"]]
    print(f"{len(items)} traces x {len(modes)} modes = {len(drafts)} drafts\n")
    for mode in modes:
        f = sum(1 for d in flagged if d["mode"] == mode)
        print(f"  {mode:45s} present {present[mode]:3d} / {len(items)}   flagged {f}")
    print(f"\n{len(flagged)} pairs flagged for review:")
    for d in flagged:
        print(f"  {d['scenario_id']} t{d['turn']}  {d['mode']:45s} draft={d['draft']}  {' | '.join(d['flags'])[:170]}")
    if not args.write:
        print("\ndry run: nothing saved. Re-run with --write to save analysis/state/label_drafts.json.")
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_json(args.state_dir / "label_drafts.json", {"schema": SCHEMA, "created_at": now, "modes": modes, "drafts": drafts})
    print(f"\nsaved {len(drafts)} drafts to {args.state_dir / 'label_drafts.json'}")


if __name__ == "__main__":
    main()
