"""Pack labeled chats for the Homework 5 judge, then split those labels once."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from analysis.helpers import split_labels
from analysis.helpers.normalization import normalize_trace

ROOT = Path(__file__).resolve().parents[1]
TRACE_SOURCE = ROOT / "traces" / "support_traces.json"
INPUTS_PATH = ROOT / "analysis" / "state" / "hw5_trace_inputs.json"
POLICY_DIR = ROOT / "data" / "policies"
REFERENCE_POLICIES = (
    "cw-refunds",
    "cw-returns",
    "cw-shipping",
    "cw-store-overrides",
)


def _live_labels(mode: str) -> list[dict]:
    path = ROOT / "analysis" / "state" / "hw5_labels" / f"{mode}.jsonl"
    live: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("superseded_by"):
            continue
        if row.get("label") not in (0, 1):
            raise ValueError(f"label for {row.get('trace_id')} must be 0 or 1")
        live[row["trace_id"]] = row
    return list(live.values())


def _policy_references() -> list[dict]:
    """Platform policies used to decide this failure mode.

    These are reference copies. A fact in one of them supports the reply only
    when a get_policy or search_help_center result in the chat also states it.
    """
    messages = []
    for policy_id in REFERENCE_POLICIES:
        text = (POLICY_DIR / f"{policy_id}.md").read_text(encoding="utf-8")
        body = text.split("---", 2)[-1].strip()
        messages.append(
            {
                "role": "policy_reference",
                "text": (
                    f"Reference copy of {policy_id}. This is not a tool result. "
                    "It supports the reply only when the same fact also appears in "
                    "a get_policy or search_help_center result in the conversation.\n\n"
                    f"{body}"
                ),
            }
        )
    return messages


def _clean(message: dict) -> dict | None:
    role = message.get("role")
    if role == "tool_call":
        return {
            "role": role,
            "name": message.get("name"),
            "arguments": message.get("arguments"),
        }
    if role == "tool_result":
        return {
            "role": role,
            "name": message.get("name"),
            "content": message.get("content"),
        }
    if role in {"user", "assistant"} and str(message.get("text") or "").strip():
        return {"role": role, "text": message["text"]}
    return None


def prepare_inputs(mode: str = "ungrounded_policy_answer") -> list[dict]:
    """Save one judge input for every labeled conversation.

    The input keeps the labeled trace id and the whole conversation: the
    customer's words, the replies, earlier turns, tool calls, and tool results.
    Human labels and review notes stay out of the file.
    """
    raw = json.loads(TRACE_SOURCE.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw["traces"]
    turns = [normalize_trace(row) for row in raw]
    by_id = {turn["trace_id"]: turn for turn in turns}
    by_scenario: dict[str, list] = defaultdict(list)
    for turn in turns:
        scenario_id = turn.get("meta", {}).get("scenario_id")
        if scenario_id:
            by_scenario[scenario_id].append(turn)
    for group in by_scenario.values():
        group.sort(key=lambda turn: turn.get("timestamp") or "")

    references = _policy_references()
    records = []
    seen_scenarios: dict[str, str] = {}
    for row in _live_labels(mode):
        trace_id = row["trace_id"]
        turn = by_id.get(trace_id)
        if turn is None:
            raise ValueError(f"labeled trace {trace_id} is not in {TRACE_SOURCE}")
        scenario_id = turn.get("meta", {}).get("scenario_id")
        group = by_scenario.get(scenario_id, [turn]) if scenario_id else [turn]
        if scenario_id:
            previous = seen_scenarios.get(scenario_id)
            if previous:
                raise ValueError(
                    f"scenario {scenario_id} is labeled twice: {previous} and {trace_id}"
                )
            seen_scenarios[scenario_id] = trace_id
        messages = []
        for item in group:
            for message in item["trace"]:
                cleaned = _clean(message)
                if cleaned:
                    messages.append(cleaned)
        if not any(message["role"] == "assistant" for message in messages):
            raise ValueError(f"trace {trace_id} has no assistant reply")
        messages.extend(references)
        records.append({"trace_id": trace_id, "trace": messages})

    label_ids = {row["trace_id"] for row in _live_labels(mode)}
    packed_ids = [record["trace_id"] for record in records]
    if len(packed_ids) != len(set(packed_ids)):
        raise ValueError("packed trace ids are not unique")
    if set(packed_ids) != label_ids:
        raise ValueError("packed traces do not match the labeled traces")
    for record in records:
        if set(record) != {"trace_id", "trace"}:
            raise ValueError(f"unexpected fields on {record['trace_id']}")

    INPUTS_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return records


def split_data(mode: str) -> dict[str, list[str]]:
    """Split labels once: 20% train, 40% development, 40% test."""
    records = json.loads(INPUTS_PATH.read_text(encoding="utf-8"))
    return split_labels(
        mode,
        fractions=(0.20, 0.40, 0.40),
        seed=7,
        min_per_class=10,
        eligible_trace_ids=[record["trace_id"] for record in records],
    )


def run_development(
    mode: str,
    prompt_path: str,
    judge_model: str = "gpt-4o-mini",
    batch_size: int = 1,
) -> dict:
    """Register a prompt and score it on the development chats.

    Saves the confusion counts, rates, and intervals to
    analysis/report/dev-<judge_id>.json. The handout names gpt-4o-mini.
    Pass judge_model when that provider has no credits.
    """
    import os

    from analysis.helpers import judge_alignment, register_judge, run_judge
    from observability.instrument import load_env

    load_env()
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(INPUTS_PATH.resolve())
    prompt_file = Path(prompt_path)
    record = register_judge(
        mode=mode,
        prompt_text=prompt_file.read_text(encoding="utf-8"),
        judge_model=judge_model,
    )
    judge_id = record["judge_id"]
    run_judge(judge_id, split="dev", batch_size=batch_size)
    development = judge_alignment(judge_id, split="dev")
    labels = {row["trace_id"]: row["label"] for row in _live_labels(mode)}
    dev_ids = json.loads((ROOT / "analysis/state/splits.json").read_text())[mode]["dev"]
    development["class_counts"] = {
        "pass": sum(labels[trace_id] == 1 for trace_id in dev_ids),
        "fail": sum(labels[trace_id] == 0 for trace_id in dev_ids),
    }
    report_path = ROOT / "analysis" / "report" / f"dev-{judge_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(development, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return development


def run_test(judge_id: str, batch_size: int = 10) -> dict:
    """Freeze one prompt, score the held-out chats once, and save the metrics.

    Call this once for the selected version. If it stops partway through,
    rerun run_judge and judge_alignment with the same judge id. Do not freeze
    or register again.
    """
    import os

    from analysis.helpers import freeze_judge, judge_alignment, run_judge
    from observability.instrument import load_env

    load_env()
    os.environ["CARTWHEEL_JUDGE_TRACE_SOURCE"] = str(INPUTS_PATH.resolve())
    freeze_judge(judge_id)
    run_judge(judge_id, split="test", batch_size=batch_size)
    test = judge_alignment(judge_id, split="test")
    mode = judge_id.rsplit("-v", 1)[0]
    labels = {row["trace_id"]: row["label"] for row in _live_labels(mode)}
    test_ids = json.loads((ROOT / "analysis/state/splits.json").read_text())[mode]["test"]
    test["class_counts"] = {
        "pass": sum(labels[trace_id] == 1 for trace_id in test_ids),
        "fail": sum(labels[trace_id] == 0 for trace_id in test_ids),
    }
    report_path = ROOT / "analysis" / "report" / f"test-{judge_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(test, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return test
