"""Review server for Homework 4 open coding.

Shows Cartwheel conversations grouped by ``cartwheel.session_id`` with every
turn's trace identifier preserved, and stores free-form annotations under the
state directory. Run from the repository root:

    uv run python -m analysis.review_app.server
    uv run python -m analysis.review_app.server --source export
    uv run python -m analysis.review_app.server \
        --scenarios support-0090,support-0036 --state-dir /tmp/cartwheel-review-trial

Annotations are append-only in effect: an edited note keeps its earlier text
in ``history``, and a deleted note is marked ``status: deleted`` rather than
removed, so the path from observation to category stays inspectable.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from analysis.review_app import loader, manifest
from analysis.review_app.manifest import DEFAULT_STATE_DIR, SCHEMA
from analysis.server import _read_json, _write_json
from observability.instrument import load_env

HERE = Path(__file__).resolve().parent

LOCK = threading.Lock()
APP: dict[str, Any] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _annotations_path() -> Path:
    return APP["state_dir"] / "annotations.json"


def _suggestions_path() -> Path:
    return APP["state_dir"] / "suggestions.json"


def read_suggestions() -> list[dict[str, Any]]:
    data = _read_json(_suggestions_path(), {"suggestions": []})
    return list(data.get("suggestions", []))


DECISIONS = {"accepted", "edited", "rejected", "pending"}


def decide_suggestion(body: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Record the reviewer's decision on one agent suggestion.

    Suggestions are never removed; the decision and any earlier decision are
    kept on the suggestion so accepted and rejected drafts stay inspectable.
    """
    if not isinstance(body, dict) or body.get("status") not in DECISIONS:
        return None, f"expected {{id, status}} with status in {sorted(DECISIONS)}"
    suggestions = read_suggestions()
    match = next((s for s in suggestions if s.get("id") == body.get("id")), None)
    if match is None:
        return None, f"unknown suggestion: {body.get('id')}"
    if match.get("status", "pending") != "pending":
        match.setdefault("history", []).append(
            {k: match.get(k) for k in ("status", "decided_at", "final_note", "reason")}
        )
    match["status"] = body["status"]
    match["decided_at"] = _now() if body["status"] != "pending" else None
    match["final_note"] = body.get("final_note")
    match["reason"] = body.get("reason")
    _write_json(_suggestions_path(), {"schema": SCHEMA, "suggestions": suggestions})
    return suggestions, None


def _label_drafts_path() -> Path:
    return APP["state_dir"] / "label_drafts.json"


def decide_labels(body: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Record the reviewer's present/absent decision on label drafts.

    ``{"trace_id", "mode", "label": 0|1, "note"}`` decides one pair;
    ``{"confirm_unflagged": true}`` accepts every undecided, unflagged draft as
    drafted. A changed decision keeps the earlier one in ``history``.
    """
    data = _read_json(_label_drafts_path(), None)
    if not data:
        return None, "no label_drafts.json; run analysis.review_app.draft_labels --write first"
    now = _now()
    if isinstance(body, dict) and body.get("confirm_unflagged") is True:
        for d in data["drafts"]:
            if not d.get("decision") and not d["flags"]:
                d["decision"] = {"label": d["draft"], "at": now, "how": "confirmed unflagged draft in bulk"}
                d["status"] = "decided"
    elif isinstance(body, dict) and body.get("label") in (0, 1):
        match = next(
            (d for d in data["drafts"] if d["trace_id"] == body.get("trace_id") and d["mode"] == body.get("mode")), None
        )
        if match is None:
            return None, f"unknown pair: {body.get('trace_id')} / {body.get('mode')}"
        if match.get("decision"):
            match.setdefault("history", []).append(match["decision"])
        match["decision"] = {"label": body["label"], "at": now, "how": "reviewer decided in the Labels tab", "note": body.get("note")}
        match["status"] = "decided"
    else:
        return None, "expected {trace_id, mode, label: 0|1} or {confirm_unflagged: true}"
    _write_json(_label_drafts_path(), data)
    return data, None


def check_state_dir(state_dir: Path) -> None:
    """Refuse to start on an annotations file this app did not write."""
    path = state_dir / "annotations.json"
    if not path.exists():
        return
    data = _read_json(path, None)
    if not (isinstance(data, dict) and data.get("schema") == SCHEMA):
        raise SystemExit(
            f"{path} was not written by this review app (it may hold the course "
            "demo or earlier work). Move it aside deliberately, or pass a "
            "different --state-dir."
        )


def read_annotations() -> list[dict[str, Any]]:
    data = _read_json(_annotations_path(), {"annotations": []})
    return list(data.get("annotations", []))


def merge_annotations(
    existing: list[dict[str, Any]], incoming: list[Any]
) -> list[dict[str, Any]]:
    """Merge a client save into the stored list without losing history."""
    now = _now()
    old = {a["id"]: a for a in existing}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in incoming:
        if not isinstance(item, dict) or not item.get("id") or item["id"] in seen:
            continue
        ann = dict(item)
        prev = old.get(ann["id"])
        if prev is None:
            ann["created_at"] = ann.get("created_at") or now
            ann["history"] = []
        else:
            history = list(prev.get("history") or [])
            if prev.get("note") != ann.get("note"):
                history.append({"note": prev.get("note"), "replaced_at": now})
            if prev.get("status", "active") != ann.get("status", "active"):
                history.append({"status": prev.get("status", "active"), "changed_at": now})
            ann["created_at"] = prev.get("created_at")
            ann["history"] = history
        merged.append(ann)
        seen.add(ann["id"])
    for ann_id, prev in old.items():
        if ann_id in seen:
            continue
        if prev.get("status") != "deleted":
            prev = {
                **prev,
                "status": "deleted",
                "history": [
                    *(prev.get("history") or []),
                    {"status": prev.get("status", "active"), "changed_at": now},
                ],
            }
        merged.append(prev)
    return merged


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        return

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/meta":
            self._json(
                {
                    "source": APP["source_note"],
                    "state_dir": str(APP["state_dir"]),
                    "langfuse_trace_base": APP["langfuse_trace_base"],
                    "instance": APP["instance"],
                }
            )
        elif url.path == "/api/conversations":
            convs = sorted(APP["conversations"].values(), key=lambda c: c["scenario_id"] or "")
            self._json([loader.summary(c) for c in convs])
        elif url.path == "/api/conversation":
            session = parse_qs(url.query).get("id", [""])[0]
            conv = APP["conversations"].get(session)
            self._json(conv if conv else {"error": "unknown session"}, 200 if conv else 404)
        elif url.path == "/api/annotations":
            with LOCK:
                self._json(read_annotations())
        elif url.path == "/api/manifest":
            self._json(manifest.load(APP["state_dir"]))
        elif url.path == "/api/patterns":
            self._json(_read_json(APP["state_dir"] / "patterns.json", {"modes": []}))
        elif url.path == "/api/suggestions":
            with LOCK:
                self._json(read_suggestions())
        elif url.path == "/api/labels":
            with LOCK:
                self._json(_read_json(_label_drafts_path(), {"modes": [], "drafts": []}))
        else:
            self._json({"error": f"unknown path: {url.path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/api/annotations", "/api/suggestions/decide", "/api/labels/decide"):
            self._json({"error": "cannot POST here"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            incoming = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError:
            incoming = None
        if path == "/api/suggestions/decide":
            with LOCK:
                suggestions, error = decide_suggestion(incoming)
            self._json({"error": error} if error else suggestions, 400 if error else 200)
            return
        if path == "/api/labels/decide":
            with LOCK:
                labels, error = decide_labels(incoming)
            self._json({"error": error} if error else labels, 400 if error else 200)
            return
        if not isinstance(incoming, list):
            self._json({"error": "expected a JSON list of annotations"}, 400)
            return
        with LOCK:
            merged = merge_annotations(read_annotations(), incoming)
            _write_json(_annotations_path(), {"schema": SCHEMA, "annotations": merged})
        self._json(merged)


def _langfuse_trace_base() -> str | None:
    host = os.environ.get("LANGFUSE_HOST", "").rstrip("/")
    project = loader.export_project_path()
    return f"{host}{project}/traces/" if host and project else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["langfuse", "export"], default="langfuse")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument(
        "--scenarios", help="comma-separated scenario ids to load (default: all)"
    )
    parser.add_argument(
        "--scenario-prefix",
        default="support-",
        help="load only scenario ids with this prefix, which skips pilot and smoke runs",
    )
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    load_env()
    state_dir = args.state_dir.resolve()
    check_state_dir(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    records, source_note = loader.load_records(args.source)
    records = [
        r for r in records if str(r["meta"].get("scenario_id", "")).startswith(args.scenario_prefix)
    ]
    if args.scenarios:
        wanted = {s.strip() for s in args.scenarios.split(",") if s.strip()}
        records = [r for r in records if r["meta"].get("scenario_id") in wanted]
    conversations = loader.build_conversations(records, loader.load_scenarios())

    APP.update(
        state_dir=state_dir,
        source_note=source_note,
        conversations=conversations,
        langfuse_trace_base=_langfuse_trace_base(),
        instance=uuid.uuid4().hex,
    )
    turns = sum(len(c["turns"]) for c in conversations.values())
    print(f"source: {source_note}")
    print(f"loaded {len(conversations)} conversations, {turns} traces")
    print(f"state: {state_dir}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"review interface on http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
