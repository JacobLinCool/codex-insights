#!/usr/bin/env python3
"""Extract SessionMetaV1 records from codex rollout manifests."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from _shared import (
    ScriptError,
    classify_tool_error,
    dump_json,
    iter_jsonl,
    parse_apply_patch_stats,
    parse_exit_code_from_output,
    parse_function_arguments,
    parse_iso8601_utc,
    redact_text,
    validate_session_meta_v1,
)

TASK_TOOL_NAMES = {
    "Task",
    "TaskCreate",
    "TaskUpdate",
    "TaskOutput",
    "TaskStop",
    "TaskList",
}

FAILED_OUTPUT_PREFIXES = (
    "exec_command failed:",
    "write_stdin failed:",
    "shell_command failed:",
    "js_repl failed:",
)


def load_session_id_filter(session_ids: str, session_ids_file: Optional[Path]) -> Optional[set[str]]:
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



def _extract_git_command_texts(tool_name: str, args: Dict[str, Any]) -> List[str]:
    texts: List[str] = []
    if tool_name in {"exec_command", "shell_command"}:
        for key in ["cmd", "command", "text"]:
            value = args.get(key)
            if isinstance(value, str):
                texts.append(value)
    elif tool_name == "write_stdin":
        chars = args.get("chars")
        if isinstance(chars, str):
            texts.append(chars)
    return texts



def _detect_git_activity(command_text: str) -> Dict[str, int]:
    lowered = command_text.lower()
    return {
        "commits": 1 if re.search(r"\bgit\s+commit\b", lowered) else 0,
        "pushes": 1 if re.search(r"\bgit\s+push\b", lowered) else 0,
    }



def extract_meta_from_rollout(manifest_row: Dict[str, Any]) -> Dict[str, Any]:
    session_id = manifest_row["session_id"]
    rollout_path = Path(manifest_row["rollout_path"]).resolve()

    if not rollout_path.exists():
        raise ScriptError(f"Rollout path not found for session {session_id}: {rollout_path}")

    session_meta_payload: Optional[Dict[str, Any]] = None
    first_event_ts: Optional[datetime] = None
    last_event_ts: Optional[datetime] = None

    first_prompt_raw = ""
    user_message_count = 0
    assistant_message_count = 0
    user_message_timestamps: List[datetime] = []
    assistant_message_timestamps: List[datetime] = []

    tool_counts: Counter[str] = Counter()
    tool_errors = 0
    tool_error_categories: Counter[str] = Counter()

    uses_mcp = False
    uses_task_agent = False
    uses_web_search = False
    uses_web_fetch = False

    git_commits = 0
    git_pushes = 0

    files_modified_patch_only: set[str] = set()
    lines_added_patch_only = 0
    lines_removed_patch_only = 0

    user_interruptions = 0

    max_input_tokens = 0
    max_output_tokens = 0
    max_cached_input_tokens = 0

    model_turn_counts: Counter[str] = Counter()

    call_id_to_tool_name: Dict[str, str] = {}

    for row in iter_jsonl(rollout_path):
        ts = parse_iso8601_utc(row["timestamp"])
        if first_event_ts is None:
            first_event_ts = ts
        last_event_ts = ts

        top_type = row.get("type")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}

        if top_type == "session_meta":
            session_meta_payload = payload
            continue

        if top_type == "event_msg":
            payload_type = payload.get("type")

            if payload_type == "user_message":
                msg = payload.get("message")
                if isinstance(msg, str):
                    if not first_prompt_raw and msg.strip():
                        first_prompt_raw = msg.strip()
                user_message_count += 1
                user_message_timestamps.append(ts)
                continue

            if payload_type == "agent_message":
                assistant_message_count += 1
                assistant_message_timestamps.append(ts)
                continue

            if payload_type == "turn_aborted" and payload.get("reason") == "interrupted":
                user_interruptions += 1
                continue

            if payload_type == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    total = info.get("total_token_usage")
                    if isinstance(total, dict):
                        if isinstance(total.get("input_tokens"), int):
                            max_input_tokens = max(max_input_tokens, total["input_tokens"])
                        if isinstance(total.get("output_tokens"), int):
                            max_output_tokens = max(max_output_tokens, total["output_tokens"])
                        if isinstance(total.get("cached_input_tokens"), int):
                            max_cached_input_tokens = max(
                                max_cached_input_tokens, total["cached_input_tokens"]
                            )
                continue

        if top_type == "turn_context":
            if isinstance(payload, dict):
                model_name = payload.get("model")
                if isinstance(model_name, str) and model_name.strip():
                    model_turn_counts[model_name.strip()] += 1
            continue

        if top_type != "response_item":
            continue

        payload_type = payload.get("type")

        if payload_type == "function_call":
            tool_name = str(payload.get("name") or "<unknown_function>")
            tool_counts[tool_name] += 1

            if tool_name.startswith("mcp__"):
                uses_mcp = True
            if tool_name in TASK_TOOL_NAMES:
                uses_task_agent = True

            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                call_id_to_tool_name[call_id] = tool_name

            args = parse_function_arguments(str(payload.get("arguments") or ""))
            for command_text in _extract_git_command_texts(tool_name, args):
                git = _detect_git_activity(command_text)
                git_commits += git["commits"]
                git_pushes += git["pushes"]
            continue

        if payload_type == "custom_tool_call":
            tool_name = str(payload.get("name") or "<unknown_custom_tool>")
            tool_counts[tool_name] += 1
            if tool_name.startswith("mcp__"):
                uses_mcp = True

            if tool_name == "apply_patch":
                patch_text = str(payload.get("input") or "")
                files, added, removed = parse_apply_patch_stats(patch_text)
                files_modified_patch_only.update(files)
                lines_added_patch_only += added
                lines_removed_patch_only += removed
            continue

        if payload_type == "web_search_call":
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            action_type = str(action.get("type") or "unknown")
            tool_counts[f"web_search_call.{action_type}"] += 1
            if action_type == "search":
                uses_web_search = True
            if action_type in {"open_page", "find_in_page"}:
                uses_web_fetch = True
            continue

        if payload_type in {"function_call_output", "custom_tool_call_output"}:
            output = payload.get("output")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)

            exit_code = parse_exit_code_from_output(output)
            failed = False
            if exit_code is not None:
                failed = exit_code != 0
            elif output.lower().startswith(FAILED_OUTPUT_PREFIXES):
                failed = True

            if failed:
                tool_errors += 1
                category = classify_tool_error(output, exit_code)
                tool_error_categories[category] += 1
            continue

    if first_event_ts is None or last_event_ts is None:
        raise ScriptError(f"Rollout has no events: {rollout_path}")

    if session_meta_payload and isinstance(session_meta_payload.get("timestamp"), str):
        start_time_dt = parse_iso8601_utc(session_meta_payload["timestamp"])
    else:
        start_time_dt = first_event_ts
    end_time_dt = last_event_ts

    duration_minutes = int(max(0.0, (end_time_dt - start_time_dt).total_seconds()) // 60)

    user_response_times: List[float] = []
    for idx in range(1, len(user_message_timestamps)):
        current_user_ts = user_message_timestamps[idx]
        prior_assistant = [t for t in assistant_message_timestamps if t < current_user_ts]
        if not prior_assistant:
            continue
        delta_seconds = (current_user_ts - prior_assistant[-1]).total_seconds()
        if delta_seconds >= 0:
            user_response_times.append(round(delta_seconds, 3))

    git_info = session_meta_payload.get("git") if isinstance(session_meta_payload, dict) else None
    git_sha = None
    git_branch = None
    if isinstance(git_info, dict):
        git_sha = git_info.get("commit_hash")
        git_branch = git_info.get("branch")

    thread_info = manifest_row.get("thread") if isinstance(manifest_row.get("thread"), dict) else {}
    if git_sha is None:
        git_sha = thread_info.get("git_sha")
    if git_branch is None:
        git_branch = thread_info.get("git_branch")

    meta: Dict[str, Any] = {
        "session_id": session_id,
        "project_path": str(
            (session_meta_payload or {}).get("cwd")
            or thread_info.get("cwd")
            or manifest_row.get("rollout_summary", {}).get("session_meta_cwd")
            or ""
        ),
        "start_time": start_time_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "end_time": end_time_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration_minutes": duration_minutes,
        "user_message_count": user_message_count,
        "assistant_message_count": assistant_message_count,
        "tool_counts": dict(sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "tool_errors": tool_errors,
        "tool_error_categories": dict(
            sorted(tool_error_categories.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "uses_mcp": uses_mcp,
        "uses_task_agent": uses_task_agent,
        "uses_web_search": uses_web_search,
        "uses_web_fetch": uses_web_fetch,
        "input_tokens": max_input_tokens,
        "output_tokens": max_output_tokens,
        "cached_input_tokens": max_cached_input_tokens,
        "first_prompt_redacted": redact_text(first_prompt_raw, max_len=240),
        "user_interruptions": user_interruptions,
        "user_response_times": user_response_times,
        "message_hours": [ts.hour for ts in user_message_timestamps],
        "git_commits": git_commits,
        "git_pushes": git_pushes,
        "git_branch": git_branch,
        "git_sha": git_sha,
        "files_modified_patch_only": len(files_modified_patch_only),
        "lines_added_patch_only": lines_added_patch_only,
        "lines_removed_patch_only": lines_removed_patch_only,
        "model_turn_counts": dict(
            sorted(model_turn_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "models_used": [
            name for name, _ in sorted(model_turn_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "primary_model": (
            sorted(model_turn_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            if model_turn_counts
            else None
        ),
        "derivation_notes": [
            "token counters use max(total_token_usage) across token_count events",
            "file and line metrics are patch-only from apply_patch custom tool calls",
            "tool error categories derive from command outputs and non-zero exit codes",
            "model usage derives from turn_context.model events in rollout",
        ],
    }

    validate_session_meta_v1(meta)
    return meta



def main() -> None:
    parser = argparse.ArgumentParser(description="Extract SessionMetaV1 files from manifest")
    parser.add_argument("--manifest", required=True, type=Path, help="manifest.jsonl path")
    parser.add_argument("--out-dir", required=True, type=Path, help="Directory for meta/*.json")
    parser.add_argument("--session-ids", default="", help="Comma-separated session IDs to process")
    parser.add_argument("--session-ids-file", type=Path, help="Optional newline-delimited session ID file")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N sessions (0=all)")
    args = parser.parse_args()

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    processed = 0
    manifest_rows = list(iter_jsonl(args.manifest.resolve()))
    session_filter = load_session_id_filter(args.session_ids, args.session_ids_file.resolve() if args.session_ids_file else None)
    if session_filter is not None:
        manifest_rows = [
            row
            for row in manifest_rows
            if isinstance(row.get("session_id"), str) and row.get("session_id") in session_filter
        ]
    if args.limit > 0:
        manifest_rows = manifest_rows[: args.limit]

    for row in manifest_rows:
        meta = extract_meta_from_rollout(row)
        out_path = out_dir / f"{meta['session_id']}.json"
        dump_json(out_path, meta)
        processed += 1

    print(f"Wrote SessionMetaV1 files: {processed} -> {out_dir}")


if __name__ == "__main__":
    main()
