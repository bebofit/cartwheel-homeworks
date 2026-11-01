"""Load Cartwheel traces and group them into conversations for human review.

Cartwheel records one Langfuse trace per user turn. The review interface shows
a whole conversation (grouped by ``cartwheel.session_id``) but keeps every
turn's own trace identifier, because notes and labels attach to a single trace.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from analysis.helpers.normalization import normalize_trace

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORT_PATH = REPO_ROOT / "traces" / "support_traces.json"
SCENARIOS_PATH = REPO_ROOT / "scenarios" / "support_scenarios.jsonl"

RETRIEVAL_TOOLS = {"search_help_center", "get_policy"}


def load_records(source: str) -> tuple[list[dict[str, Any]], str]:
    """Return normalized trace records and a note describing where they came from.

    ``source="langfuse"`` reads the live project and falls back to the Module 1
    export only when Langfuse fails; the returned note carries the reason so the
    interface can show it and the interface comparison can record it.
    """
    if source == "langfuse":
        try:
            from analysis.helpers import langfuse_io

            records = langfuse_io.fetch_traces()
            if records:
                return records, f"Langfuse ({len(records)} traces)"
            reason = "Langfuse returned no Module 1 scenario traces"
        except Exception as exc:  # network or configuration failure
            reason = f"Langfuse unavailable: {exc}"
        records = _load_export()
        return records, f"Export {EXPORT_PATH.name} ({len(records)} traces). {reason}"
    records = _load_export()
    return records, f"Export {EXPORT_PATH.name} ({len(records)} traces), chosen with --source export"


def _load_export() -> list[dict[str, Any]]:
    raw = json.loads(EXPORT_PATH.read_text())
    records = [normalize_trace(t) for t in raw.get("traces", [])]
    return [r for r in records if r["meta"].get("scenario_id")]


def load_scenarios() -> dict[str, dict[str, Any]]:
    if not SCENARIOS_PATH.exists():
        return {}
    out = {}
    for line in SCENARIOS_PATH.read_text().splitlines():
        if line.strip():
            item = json.loads(line)
            out[item["id"]] = item
    return out


def export_project_path() -> str | None:
    """Return ``/project/<id>`` from the export, used to build Langfuse links."""
    if not EXPORT_PATH.exists():
        return None
    with EXPORT_PATH.open() as fh:
        head = fh.read(200_000)
    marker = '"htmlPath": "'
    start = head.find(marker)
    if start < 0:
        return None
    path = head[start + len(marker) : head.find('"', start + len(marker))]
    return path.split("/traces/")[0] or None


def _parts_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                for part in item.get("parts") or []:
                    if (
                        isinstance(part, dict)
                        and part.get("type", "text") == "text"
                        and part.get("content")
                    ):
                        out.append(str(part["content"]))
        return "\n".join(out)
    return json.dumps(value, ensure_ascii=False)


def _system_prompt(generation_input: Any) -> str | None:
    if not isinstance(generation_input, dict):
        return None
    messages = generation_input.get("messages") or []
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        return _parts_text([messages[0]])
    return None


def tool_badge(name: str, result: Any) -> dict[str, str]:
    """Summarize a tool result in one line, so status is visible without opening JSON."""
    if not isinstance(result, dict):
        return {"tone": "neutral", "text": "result"}
    if result.get("ok") is False:
        return {"tone": "error", "text": f"error · {result.get('error', 'unknown')}"}
    if "status" in result:
        status = str(result["status"])
        tone = "warn" if status == "queued_for_approval" else "ok"
        return {"tone": tone, "text": f"ok · {status}"}
    if "ticket_id" in result:
        return {
            "tone": "ok",
            "text": f"ticket #{result['ticket_id']} · SLA {result.get('sla_hours', '?')}h",
        }
    if isinstance(result.get("order"), dict):
        order = result["order"]
        return {
            "tone": "ok",
            "text": f"order {order.get('order_id')} · {order.get('status')}"
            f" · refund_eligible={order.get('refund_eligible')}",
        }
    for key in ("results", "orders", "products"):
        if isinstance(result.get(key), list):
            return {"tone": "ok", "text": f"{len(result[key])} {key}"}
    if "body" in result:
        return {"tone": "ok", "text": f"policy {result.get('policy_id', '')}"}
    if "store_id" in result:
        return {"tone": "ok", "text": f"store {result.get('name')} (id {result['store_id']})"}
    return {"tone": "ok", "text": "ok"}


def _turn(record: dict[str, Any]) -> dict[str, Any]:
    observations = sorted(
        record.get("observations") or [], key=lambda o: o.get("start_time") or ""
    )
    steps: list[dict[str, Any]] = []
    system_prompt = None
    for obs in observations:
        if obs.get("type") == "GENERATION":
            if system_prompt is None:
                system_prompt = _system_prompt(obs.get("input"))
            for item in obs.get("output") or []:
                if not isinstance(item, dict):
                    continue
                for part in item.get("parts") or []:
                    if not isinstance(part, dict) or part.get("type") != "text":
                        continue
                    text = part.get("content")
                    if text and text.strip():
                        steps.append({"kind": "narration", "text": text})
        elif obs.get("type") == "TOOL":
            name = str(obs.get("name"))
            steps.append(
                {
                    "kind": "tool",
                    "name": name,
                    "retrieval": name in RETRIEVAL_TOOLS,
                    "arguments": obs.get("input"),
                    "result": obs.get("output"),
                    "badge": tool_badge(name, obs.get("output")),
                }
            )
    final = _parts_text(record.get("output"))
    # The last model call's text is the final reply; show it once, as the reply.
    if steps and steps[-1]["kind"] == "narration" and steps[-1]["text"].strip() == final.strip():
        steps.pop()
    return {
        "trace_id": record["trace_id"],
        "timestamp": record.get("timestamp"),
        "user": _parts_text(record.get("input")),
        "steps": steps,
        "final": final,
        "system_prompt": system_prompt,
    }


def build_conversations(
    records: list[dict[str, Any]], scenarios: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Group trace records by session and order each conversation's turns in time."""
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        session = record["meta"].get("session_id") or record["trace_id"]
        by_session[session].append(record)

    conversations = {}
    for session, group in by_session.items():
        group.sort(key=lambda r: r.get("timestamp") or "")
        turns = [_turn(r) for r in group]
        first = group[0]
        scenario_id = first["meta"].get("scenario_id")
        scenario = scenarios.get(scenario_id) or {}
        conversations[session] = {
            "session_id": session,
            "scenario_id": scenario_id,
            "role": first["meta"].get("role"),
            "user_id": first.get("metadata", {}).get("cartwheel.user_id"),
            "prompt_version": first["meta"].get("prompt_version"),
            "tools": sorted({s["name"] for t in turns for s in t["steps"] if s["kind"] == "tool"}),
            "trace_ids": [t["trace_id"] for t in turns],
            "system_prompt": next((t["system_prompt"] for t in turns if t["system_prompt"]), None),
            "turns": [{k: v for k, v in t.items() if k != "system_prompt"} for t in turns],
            "scenario_intent": {
                key: scenario.get(key)
                for key in ("scenario_group", "data_quality_case_id", "tuple", "expected")
                if scenario.get(key) is not None
            },
        }
    return conversations


def summary(conversation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: conversation[key]
        for key in ("session_id", "scenario_id", "role", "tools", "trace_ids")
    }
