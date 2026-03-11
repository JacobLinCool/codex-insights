#!/usr/bin/env python3
"""Prepare compact per-session evidence for semantic facet classification."""

from __future__ import annotations

import argparse
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from _shared import ScriptError, dump_json, iter_jsonl, load_json, redact_text

VALID_PRIVACY = {"redacted", "raw", "none"}


def load_session_id_filter(session_ids: str, session_ids_file: Path | None) -> set[str] | None:
    values: set[str] = set()
    if session_ids.strip():
        for raw in session_ids.split(","):
            sid = raw.strip()
            if sid:
                values.add(sid)
    if session_ids_file is not None:
        for raw in session_ids_file.read_text(encoding="utf-8").splitlines():
            sid = raw.strip()
            if sid:
                values.add(sid)
    return values if values else None


def maybe_text(value: str, privacy: str, max_len: int = 240) -> str:
    if privacy == "none":
        return ""
    if privacy == "raw":
        text = " ".join((value or "").split())
        return text[: max_len - 1] + "…" if len(text) > max_len else text
    return redact_text(value or "", max_len=max_len)


def maybe_path(value: str, privacy: str) -> str:
    if privacy == "none":
        return ""
    if privacy == "raw":
        return value or ""
    return "<PATH>"



def summarize_response_times(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0}
    sorted_values = sorted(values)

    def percentile(p: float) -> float:
        idx = int((len(sorted_values) - 1) * p)
        return round(sorted_values[idx], 3)

    return {
        "count": len(sorted_values),
        "avg": round(statistics.mean(sorted_values), 3),
        "p50": percentile(0.50),
        "p90": percentile(0.90),
        "max": round(sorted_values[-1], 3),
    }



def build_evidence_row(manifest_row: Dict[str, Any], meta: Dict[str, Any], privacy: str) -> Dict[str, Any]:
    rollout_path = Path(manifest_row["rollout_path"]).resolve()
    if not rollout_path.exists():
        raise ScriptError(f"Rollout path not found: {rollout_path}")

    first_user_message = ""
    last_user_message = ""
    last_agent_message = ""
    task_complete_message = ""
    event_type_counts: Counter[str] = Counter()

    completion_flags = {
        "has_task_complete": False,
        "has_item_completed": False,
        "has_turn_aborted": False,
        "has_context_compacted": False,
    }

    for row in iter_jsonl(rollout_path):
        top_type = row.get("type")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        payload_type = payload.get("type")

        if top_type == "event_msg" and payload_type:
            event_type_counts[f"event_msg.{payload_type}"] += 1
            if payload_type == "user_message":
                message = payload.get("message")
                if isinstance(message, str):
                    if not first_user_message and message.strip():
                        first_user_message = message.strip()
                    last_user_message = message.strip()
            elif payload_type == "agent_message":
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    last_agent_message = message.strip()
            elif payload_type == "task_complete":
                completion_flags["has_task_complete"] = True
                message = payload.get("last_agent_message")
                if isinstance(message, str) and message.strip():
                    task_complete_message = message.strip()
            elif payload_type == "item_completed":
                completion_flags["has_item_completed"] = True
            elif payload_type == "turn_aborted":
                completion_flags["has_turn_aborted"] = True
            elif payload_type == "context_compacted":
                completion_flags["has_context_compacted"] = True

        elif top_type == "response_item" and payload_type:
            event_type_counts[f"response_item.{payload_type}"] += 1

    response_times = meta.get("user_response_times") if isinstance(meta.get("user_response_times"), list) else []
    response_times = [float(v) for v in response_times if isinstance(v, (int, float)) and v >= 0]

    tool_counts = meta.get("tool_counts") if isinstance(meta.get("tool_counts"), dict) else {}
    top_tools = sorted(tool_counts.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:8]

    evidence = {
        "session_id": meta["session_id"],
        "privacy_mode": privacy,
        "thread_source": {
            "project_path": maybe_path(str(meta.get("project_path") or ""), privacy),
            "scope": manifest_row.get("scope"),
            "rollout_path": maybe_path(str(rollout_path), privacy),
            "rollout_file": rollout_path.name,
            "is_archived_rollout": bool(
                manifest_row.get("rollout_summary", {}).get("is_archived_rollout", False)
            ),
        },
        "text_signals": {
            "first_prompt": maybe_text(first_user_message, privacy, max_len=260),
            "last_user_message": maybe_text(last_user_message, privacy, max_len=220),
            "last_agent_message": maybe_text(last_agent_message, privacy, max_len=220),
            "task_complete_message": maybe_text(task_complete_message, privacy, max_len=220),
        },
        "activity_signals": {
            "duration_minutes": meta["duration_minutes"],
            "user_message_count": meta["user_message_count"],
            "assistant_message_count": meta["assistant_message_count"],
            "input_tokens": meta["input_tokens"],
            "output_tokens": meta["output_tokens"],
            "cached_input_tokens": meta["cached_input_tokens"],
            "files_modified_patch_only": meta["files_modified_patch_only"],
            "lines_added_patch_only": meta["lines_added_patch_only"],
            "lines_removed_patch_only": meta["lines_removed_patch_only"],
            "user_interruptions": meta["user_interruptions"],
            "response_time_summary": summarize_response_times(response_times),
        },
        "tool_signals": {
            "top_tools": [{"name": k, "count": int(v)} for k, v in top_tools],
            "tool_errors": int(meta["tool_errors"]),
            "tool_error_categories": meta.get("tool_error_categories", {}),
            "uses_mcp": bool(meta.get("uses_mcp", False)),
            "uses_task_agent": bool(meta.get("uses_task_agent", False)),
            "uses_web_search": bool(meta.get("uses_web_search", False)),
            "uses_web_fetch": bool(meta.get("uses_web_fetch", False)),
            "git_commits": int(meta.get("git_commits", 0)),
            "git_pushes": int(meta.get("git_pushes", 0)),
        },
        "event_signals": {
            "event_type_counts": dict(sorted(event_type_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "completion_flags": completion_flags,
        },
        "evidence_notes": [
            "text signals are redacted by default",
            "activity/tool metrics come from deterministic SessionMetaV1 outputs",
            "no raw transcript persistence in redacted mode",
        ],
    }

    return evidence



def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare redacted evidence for facet classification")
    parser.add_argument("--manifest", required=True, type=Path, help="manifest.jsonl path")
    parser.add_argument("--meta-dir", required=True, type=Path, help="Directory containing SessionMetaV1 files")
    parser.add_argument("--privacy", default="redacted", choices=sorted(VALID_PRIVACY), help="Privacy mode")
    parser.add_argument("--out-dir", required=True, type=Path, help="Output directory for evidence/*.json")
    parser.add_argument("--session-ids", default="", help="Comma-separated session IDs to process")
    parser.add_argument("--session-ids-file", type=Path, help="Optional newline-delimited session ID file")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N sessions (0=all)")
    args = parser.parse_args()

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = list(iter_jsonl(args.manifest.resolve()))
    session_filter = load_session_id_filter(
        args.session_ids,
        args.session_ids_file.resolve() if args.session_ids_file else None,
    )
    if session_filter is not None:
        rows = [
            row
            for row in rows
            if isinstance(row.get("session_id"), str) and row.get("session_id") in session_filter
        ]
    if args.limit > 0:
        rows = rows[: args.limit]

    processed = 0
    for row in rows:
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ScriptError("Manifest row missing session_id")

        meta_path = args.meta_dir.resolve() / f"{session_id}.json"
        if not meta_path.exists():
            raise ScriptError(f"Session meta missing for {session_id}: {meta_path}")

        meta = load_json(meta_path)
        evidence = build_evidence_row(row, meta, args.privacy)
        dump_json(out_dir / f"{session_id}.json", evidence)
        processed += 1

    print(f"Wrote evidence files: {processed} -> {out_dir}")


if __name__ == "__main__":
    main()
