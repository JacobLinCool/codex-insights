#!/usr/bin/env python3
"""Aggregate SessionMetaV1 + FacetV1 into StatsV1."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from _shared import (
    ScriptError,
    dump_json,
    format_iso8601_z,
    load_json,
    parse_iso8601_utc,
    load_jsonl,
    response_time_bucket,
    sorted_counter_dict,
    validate_facet_v1,
    validate_session_meta_v1,
    validate_stats_v1,
)

OUTCOME_FOCUS = ["fully_achieved", "mostly_achieved", "not_achieved"]
SWITCH_BUCKET_LABELS = {
    "none_0": "0",
    "low_1_2": "1-2",
    "medium_3_5": "3-5",
    "high_6_plus": "6+",
}


def load_records(directory: Path) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        data = load_json(path)
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ScriptError(f"Missing session_id in {path}")
        rows[session_id] = data
    return rows


def load_manifest_records(path: Path) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for row in load_jsonl(path):
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ScriptError(f"Invalid session_id in manifest row: {row}")
        rows[session_id] = row
    return rows


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int((len(ordered) - 1) * p)
    return float(ordered[idx])


def average(values: List[float]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def duration_buckets(values: List[int]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for minutes in values:
        if minutes < 5:
            counter["<5m"] += 1
        elif minutes < 15:
            counter["5-15m"] += 1
        elif minutes < 30:
            counter["15-30m"] += 1
        elif minutes < 60:
            counter["30-60m"] += 1
        elif minutes < 120:
            counter["1-2h"] += 1
        else:
            counter[">2h"] += 1
    return sorted_counter_dict(dict(counter))


def archive_latency_buckets(values_hours: List[float]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for hours in values_hours:
        minutes = float(hours) * 60.0
        if minutes < 10:
            counter["<10m"] += 1
        elif minutes < 30:
            counter["10-30m"] += 1
        elif minutes < 60:
            counter["30-60m"] += 1
        elif hours < 6:
            counter["1-6h"] += 1
        elif hours < 24:
            counter["6-24h"] += 1
        elif hours < 72:
            counter["1-3d"] += 1
        else:
            counter[">3d"] += 1
    return sorted_counter_dict(dict(counter))


def utc_day_from_iso8601(ts: str) -> str:
    return parse_iso8601_utc(ts).astimezone(timezone.utc).strftime("%Y-%m-%d")


def utc_day_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def project_label(project_path: str) -> str:
    text = (project_path or "").strip().rstrip("/")
    if not text:
        return "<unknown>"
    return Path(text).name or "<unknown>"


def normalize_source(source: Any) -> str:
    if not isinstance(source, str) or not source.strip():
        return "<unknown>"
    text = source.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return "<invalid_json_source>"
        if isinstance(obj, dict):
            subagent = obj.get("subagent")
            if isinstance(subagent, str) and subagent.strip():
                return f"subagent:{subagent.strip()}"
            return "<json_source>"
    return text


def parse_sandbox_policy(policy: Any) -> tuple[str, str]:
    if not isinstance(policy, str) or not policy.strip():
        return "<unknown>", "unknown"
    text = policy.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return "<invalid_json>", "unknown"
    if not isinstance(obj, dict):
        return "<invalid_json>", "unknown"

    sandbox_type = str(obj.get("type") or "<unknown>")
    network_access = obj.get("network_access")
    if network_access is True:
        network_mode = "enabled"
    elif network_access is False:
        network_mode = "disabled"
    else:
        network_mode = "unspecified"

    return sandbox_type, network_mode


def normalize_cli_major_minor(version: Any) -> str:
    if not isinstance(version, str) or not version.strip():
        return "<unknown>"
    text = version.strip()
    parts = text.split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return "<unknown>"


def classify_cli_release_channel(version: Any) -> str:
    if not isinstance(version, str) or not version.strip():
        return "<unknown>"
    text = version.strip().lower()
    if "-alpha" in text:
        return "alpha"
    if "-beta" in text:
        return "beta"
    if "-rc" in text:
        return "rc"
    return "stable"


def context_switch_bucket(switch_events: int) -> str:
    if switch_events <= 0:
        return "none_0"
    if switch_events <= 2:
        return "low_1_2"
    if switch_events <= 5:
        return "medium_3_5"
    return "high_6_plus"


def compute_parallel_session_stats(meta_rows: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    intervals: List[tuple[str, Any, Any, int]] = []
    for session_id, meta in meta_rows.items():
        start = parse_iso8601_utc(meta["start_time"])
        end = parse_iso8601_utc(meta["end_time"])
        if end < start:
            end = start
        msg_count = int(meta["user_message_count"]) + int(meta["assistant_message_count"])
        intervals.append((session_id, start, end, msg_count))

    intervals.sort(key=lambda item: item[1])

    overlap_pairs = 0
    involved: set[str] = set()

    for i in range(len(intervals)):
        sid_i, start_i, end_i, _ = intervals[i]
        for j in range(i + 1, len(intervals)):
            sid_j, start_j, end_j, _ = intervals[j]
            if start_j >= end_i:
                break
            if end_j > start_i:
                overlap_pairs += 1
                involved.add(sid_i)
                involved.add(sid_j)

    sweep_events: List[tuple[Any, int]] = []
    for _, start, end, _ in intervals:
        if end <= start:
            continue
        sweep_events.append((start, 1))
        sweep_events.append((end, -1))
    sweep_events.sort(key=lambda ev: (ev[0], ev[1]))

    current = 0
    max_concurrent = 0
    overlap_seconds = 0.0
    idx = 0
    while idx < len(sweep_events):
        t = sweep_events[idx][0]
        while idx < len(sweep_events) and sweep_events[idx][0] == t:
            current += sweep_events[idx][1]
            if current > max_concurrent:
                max_concurrent = current
            idx += 1
        if idx < len(sweep_events):
            next_t = sweep_events[idx][0]
            if current >= 2:
                overlap_seconds += max(0.0, (next_t - t).total_seconds())

    message_total = sum(item[3] for item in intervals)
    message_involved = sum(item[3] for item in intervals if item[0] in involved)
    message_share_pct = round((message_involved / message_total) * 100.0, 2) if message_total > 0 else 0.0
    overlap_minutes = round(overlap_seconds / 60.0, 2)
    sessions_involved = len(involved)
    overlap_session_share_pct = (
        round((sessions_involved / len(intervals)) * 100.0, 2) if intervals else 0.0
    )

    return {
        "overlap_events": overlap_pairs,
        "sessions_involved": sessions_involved,
        "messages_in_overlapping_sessions": message_involved,
        "message_share_percent": message_share_pct,
        "max_concurrent_sessions": int(max_concurrent),
        "overlap_minutes": overlap_minutes,
        "overlap_session_share_percent": overlap_session_share_pct,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate stats from meta + facets")
    parser.add_argument("--meta-dir", required=True, type=Path, help="Directory containing meta/*.json")
    parser.add_argument("--facets-dir", required=True, type=Path, help="Directory containing facets/*.json")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Optional manifest.jsonl for sqlite-native distributions and lifecycle time series",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output stats.json path")
    args = parser.parse_args()

    meta_dir = args.meta_dir.resolve()
    facets_dir = args.facets_dir.resolve()

    if not meta_dir.exists():
        raise ScriptError(f"meta dir does not exist: {meta_dir}")
    if not facets_dir.exists():
        raise ScriptError(f"facets dir does not exist: {facets_dir}")

    manifest_rows: Dict[str, Dict[str, Any]] = {}
    manifest_supplied = args.manifest is not None
    if args.manifest is not None:
        manifest_path = args.manifest.resolve()
        if not manifest_path.exists():
            raise ScriptError(f"manifest path does not exist: {manifest_path}")
        manifest_rows = load_manifest_records(manifest_path)

    meta_rows = load_records(meta_dir)
    if not meta_rows:
        raise ScriptError("No meta files found.")

    facet_rows = load_records(facets_dir)

    for row in meta_rows.values():
        validate_session_meta_v1(row)
    for row in facet_rows.values():
        validate_facet_v1(row)

    meta_ids = set(meta_rows.keys())
    facet_ids = set(facet_rows.keys())
    aligned_ids = sorted(meta_ids & facet_ids)

    start_times = [parse_iso8601_utc(meta_rows[sid]["start_time"]) for sid in meta_ids]
    end_times = [parse_iso8601_utc(meta_rows[sid]["end_time"]) for sid in meta_ids]

    total_tool_calls = 0
    total_tool_errors = 0
    total_user_messages = 0
    total_assistant_messages = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cached_input_tokens = 0
    total_git_commits = 0
    total_git_pushes = 0
    total_files_modified = 0
    total_lines_added = 0
    total_lines_removed = 0

    uses_mcp_sessions = 0
    uses_task_agent_sessions = 0
    uses_web_search_sessions = 0
    uses_web_fetch_sessions = 0

    duration_values: List[int] = []
    response_times: List[float] = []

    tool_counter: Counter[str] = Counter()
    tool_error_counter: Counter[str] = Counter()
    message_hour_counter: Counter[str] = Counter()
    response_time_counter: Counter[str] = Counter()
    project_counter: Counter[str] = Counter()
    daily_sessions_counter: Counter[str] = Counter()
    daily_messages_counter: Counter[str] = Counter()
    daily_tool_errors_counter: Counter[str] = Counter()
    daily_input_tokens_counter: Counter[str] = Counter()
    daily_output_tokens_counter: Counter[str] = Counter()
    daily_primary_model_counter: Dict[str, Counter[str]] = {}
    day_session_rows: Dict[str, List[tuple[Any, str, str]]] = {}

    source_counter: Counter[str] = Counter()
    approval_mode_counter: Counter[str] = Counter()
    sandbox_type_counter: Counter[str] = Counter()
    sandbox_network_counter: Counter[str] = Counter()
    model_provider_counter: Counter[str] = Counter()
    cli_version_counter: Counter[str] = Counter()
    cli_major_minor_counter: Counter[str] = Counter()
    cli_release_channel_counter: Counter[str] = Counter()
    memory_mode_counter: Counter[str] = Counter()
    archived_status_counter: Counter[str] = Counter()
    has_user_event_counter: Counter[str] = Counter()
    primary_model_counter: Counter[str] = Counter()

    created_day_counter: Counter[str] = Counter()
    archived_day_counter: Counter[str] = Counter()
    archive_latency_hours: List[float] = []
    sessions_with_model_signal = 0
    manifest_missing_sessions = 0
    session_rollups: List[Dict[str, Any]] = []

    for session_id in sorted(meta_ids):
        meta = meta_rows[session_id]
        start_dt = parse_iso8601_utc(meta["start_time"])
        day = utc_day_from_iso8601(meta["start_time"])
        project = project_label(str(meta.get("project_path") or ""))
        day_session_rows.setdefault(day, []).append(
            (start_dt, session_id, project)
        )

        total_user_messages += int(meta["user_message_count"])
        total_assistant_messages += int(meta["assistant_message_count"])
        total_tool_errors += int(meta["tool_errors"])
        total_input_tokens += int(meta["input_tokens"])
        total_output_tokens += int(meta["output_tokens"])
        total_cached_input_tokens += int(meta["cached_input_tokens"])
        total_git_commits += int(meta["git_commits"])
        total_git_pushes += int(meta["git_pushes"])
        total_files_modified += int(meta["files_modified_patch_only"])
        total_lines_added += int(meta["lines_added_patch_only"])
        total_lines_removed += int(meta["lines_removed_patch_only"])

        daily_sessions_counter[day] += 1
        daily_messages_counter[day] += int(meta["user_message_count"]) + int(meta["assistant_message_count"])
        daily_tool_errors_counter[day] += int(meta["tool_errors"])
        daily_input_tokens_counter[day] += int(meta["input_tokens"])
        daily_output_tokens_counter[day] += int(meta["output_tokens"])

        if bool(meta["uses_mcp"]):
            uses_mcp_sessions += 1
        if bool(meta["uses_task_agent"]):
            uses_task_agent_sessions += 1
        if bool(meta["uses_web_search"]):
            uses_web_search_sessions += 1
        if bool(meta["uses_web_fetch"]):
            uses_web_fetch_sessions += 1

        project_counter[project] += 1

        duration = int(meta["duration_minutes"])
        duration_values.append(duration)

        session_message_hour_counter: Counter[str] = Counter()
        for hour in meta["message_hours"]:
            hour_key = f"{int(hour):02d}"
            message_hour_counter[hour_key] += 1
            session_message_hour_counter[hour_key] += 1

        session_response_bucket_counter: Counter[str] = Counter()
        for seconds in meta["user_response_times"]:
            val = float(seconds)
            response_times.append(val)
            bucket = response_time_bucket(val)
            response_time_counter[bucket] += 1
            session_response_bucket_counter[bucket] += 1

        tool_counts = meta["tool_counts"] if isinstance(meta["tool_counts"], dict) else {}
        session_tool_calls = 0
        for name, count in tool_counts.items():
            c = int(count)
            tool_counter[str(name)] += c
            total_tool_calls += c
            session_tool_calls += c

        error_counts = (
            meta["tool_error_categories"] if isinstance(meta["tool_error_categories"], dict) else {}
        )
        for name, count in error_counts.items():
            tool_error_counter[str(name)] += int(count)

        model_turn_counts = meta.get("model_turn_counts")
        primary_model_name = None
        if isinstance(model_turn_counts, dict) and model_turn_counts:
            normalized: List[tuple[str, int]] = []
            for name, count in model_turn_counts.items():
                if isinstance(name, str) and name and isinstance(count, int) and count > 0:
                    normalized.append((name, count))
            if normalized:
                normalized.sort(key=lambda kv: (-kv[1], kv[0]))
                primary_model_name = normalized[0][0]
        if isinstance(meta.get("primary_model"), str) and meta.get("primary_model"):
            primary_model_name = str(meta["primary_model"])

        if primary_model_name:
            sessions_with_model_signal += 1
            primary_model_counter[primary_model_name] += 1
            if day not in daily_primary_model_counter:
                daily_primary_model_counter[day] = Counter()
            daily_primary_model_counter[day][primary_model_name] += 1

        source_value = "<unknown>"
        approval_mode_value = "<unknown>"
        sandbox_type_value = "<unknown>"
        cli_major_minor_value = "<unknown>"
        archived_value: bool | None = None
        archive_latency_hours_value: float | None = None

        if manifest_supplied:
            manifest_row = manifest_rows.get(session_id)
            if not isinstance(manifest_row, dict):
                manifest_missing_sessions += 1
            else:
                thread = manifest_row.get("thread") if isinstance(manifest_row.get("thread"), dict) else {}
                rollout_summary = (
                    manifest_row.get("rollout_summary")
                    if isinstance(manifest_row.get("rollout_summary"), dict)
                    else {}
                )

                source_value = normalize_source(thread.get("source") or rollout_summary.get("session_meta_source"))
                approval_mode_value = str(thread.get("approval_mode") or "<unknown>")
                source_counter[source_value] += 1
                approval_mode_counter[approval_mode_value] += 1

                model_provider_counter[
                    str(thread.get("model_provider") or rollout_summary.get("model_provider") or "<unknown>")
                ] += 1

                cli_version_text = str(thread.get("cli_version") or rollout_summary.get("cli_version") or "<unknown>")
                cli_version_counter[cli_version_text] += 1
                cli_major_minor_value = normalize_cli_major_minor(cli_version_text)
                cli_major_minor_counter[cli_major_minor_value] += 1
                cli_release_channel_counter[classify_cli_release_channel(cli_version_text)] += 1
                memory_mode_counter[str(thread.get("memory_mode") or "<unknown>")] += 1

                sandbox_type_value, sandbox_network = parse_sandbox_policy(thread.get("sandbox_policy"))
                sandbox_type_counter[sandbox_type_value] += 1
                sandbox_network_counter[sandbox_network] += 1

                has_user_event = thread.get("has_user_event")
                has_user_event_counter["has_user_event" if has_user_event else "no_user_event"] += 1

                archived_raw = thread.get("archived")
                archived_value = bool(archived_raw)
                archived_status_counter["archived" if archived_value else "active"] += 1

                created_at = thread.get("created_at")
                archived_at = thread.get("archived_at")
                if isinstance(created_at, int) and created_at > 0:
                    created_day_counter[utc_day_from_unix(created_at)] += 1
                if isinstance(archived_at, int) and archived_at > 0:
                    archived_day_counter[utc_day_from_unix(archived_at)] += 1
                if (
                    isinstance(created_at, int)
                    and isinstance(archived_at, int)
                    and created_at > 0
                    and archived_at >= created_at
                ):
                    latency_hours = (archived_at - created_at) / 3600.0
                    archive_latency_hours.append(latency_hours)
                    archive_latency_hours_value = round(latency_hours, 3)

        facet = facet_rows.get(session_id) if isinstance(facet_rows.get(session_id), dict) else None
        facet_outcome = str(facet["outcome"]) if isinstance(facet, dict) else None

        session_rollups.append(
            {
                "session_id": session_id,
                "start_time": meta["start_time"],
                "end_time": meta["end_time"],
                "day_utc": day,
                "project_label": project,
                "primary_model": primary_model_name or "<unknown>",
                "facet_present": isinstance(facet, dict),
                "outcome": facet_outcome,
                "user_messages": int(meta["user_message_count"]),
                "assistant_messages": int(meta["assistant_message_count"]),
                "messages_total": int(meta["user_message_count"]) + int(meta["assistant_message_count"]),
                "tool_calls": int(session_tool_calls),
                "tool_errors": int(meta["tool_errors"]),
                "tool_error_categories": sorted_counter_dict({str(k): int(v) for k, v in error_counts.items()}),
                "input_tokens": int(meta["input_tokens"]),
                "output_tokens": int(meta["output_tokens"]),
                "cached_input_tokens": int(meta["cached_input_tokens"]),
                "response_time_buckets": sorted_counter_dict(dict(session_response_bucket_counter)),
                "message_hours_utc": sorted_counter_dict(dict(session_message_hour_counter)),
                "source": source_value,
                "approval_mode": approval_mode_value,
                "sandbox_type": sandbox_type_value,
                "cli_version_major_minor": cli_major_minor_value,
                "archived": archived_value,
                "archive_latency_hours": archive_latency_hours_value,
            }
        )

    goal_counter: Counter[str] = Counter()
    outcome_counter: Counter[str] = Counter()
    helpfulness_counter: Counter[str] = Counter()
    session_type_counter: Counter[str] = Counter()
    primary_success_counter: Counter[str] = Counter()
    friction_counter: Counter[str] = Counter()
    satisfaction_counter: Counter[str] = Counter()

    for session_id in aligned_ids:
        facet = facet_rows[session_id]
        outcome_counter[facet["outcome"]] += 1
        helpfulness_counter[facet["assistant_helpfulness"]] += 1
        session_type_counter[facet["session_type"]] += 1
        primary_success_counter[facet["primary_success"]] += 1

        for key, count in facet["goal_categories"].items():
            goal_counter[str(key)] += int(count)
        for key, count in facet["friction_counts"].items():
            friction_counter[str(key)] += int(count)
        for key, count in facet["user_satisfaction_counts"].items():
            satisfaction_counter[str(key)] += int(count)

    model_outcome_counts: Dict[str, Counter[str]] = {}
    model_session_counts: Counter[str] = Counter()
    for session_id in aligned_ids:
        facet = facet_rows[session_id]
        outcome = str(facet["outcome"])
        meta = meta_rows[session_id]
        model_name = meta.get("primary_model")
        if not isinstance(model_name, str) or not model_name.strip():
            model_name = "<unknown>"
        model_name = model_name.strip()
        model_session_counts[model_name] += 1
        if model_name not in model_outcome_counts:
            model_outcome_counts[model_name] = Counter()
        model_outcome_counts[model_name][outcome] += 1

    model_outcome_rows: List[Dict[str, Any]] = []
    for model_name, sessions in sorted(
        model_session_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        counts = model_outcome_counts.get(model_name, Counter())
        focus_ratios = {
            outcome: round((int(counts.get(outcome, 0)) / sessions), 4) if sessions > 0 else 0.0
            for outcome in OUTCOME_FOCUS
        }
        model_outcome_rows.append(
            {
                "model": model_name,
                "sessions": int(sessions),
                "outcome_counts": sorted_counter_dict({k: int(v) for k, v in counts.items()}),
                "outcome_ratios": focus_ratios,
            }
        )

    day_switch_events: Dict[str, int] = {}
    context_switch_daily_rows: List[Dict[str, Any]] = []
    total_switch_events = 0
    days_with_switches = 0
    for day in sorted(day_session_rows.keys()):
        rows = sorted(day_session_rows[day], key=lambda item: (item[0], item[1]))
        switches = 0
        unique_projects: set[str] = set()
        prev_project: str | None = None
        for _, _, project in rows:
            unique_projects.add(project)
            if prev_project is not None and project != prev_project:
                switches += 1
            prev_project = project
        day_switch_events[day] = switches
        total_switch_events += switches
        if switches > 0:
            days_with_switches += 1
        context_switch_daily_rows.append(
            {
                "date": day,
                "sessions": len(rows),
                "switch_events": switches,
                "unique_projects": len(unique_projects),
            }
        )

    switch_bucket_outcome_counts: Dict[str, Counter[str]] = {
        key: Counter() for key in SWITCH_BUCKET_LABELS.keys()
    }
    switch_bucket_session_counts: Counter[str] = Counter()
    for session_id in aligned_ids:
        day = utc_day_from_iso8601(meta_rows[session_id]["start_time"])
        bucket = context_switch_bucket(int(day_switch_events.get(day, 0)))
        switch_bucket_session_counts[bucket] += 1
        switch_bucket_outcome_counts[bucket][facet_rows[session_id]["outcome"]] += 1

    switch_bucket_rows: List[Dict[str, Any]] = []
    for bucket_key in SWITCH_BUCKET_LABELS.keys():
        sessions = int(switch_bucket_session_counts.get(bucket_key, 0))
        counts = switch_bucket_outcome_counts.get(bucket_key, Counter())
        focus_ratios = {
            outcome: round((int(counts.get(outcome, 0)) / sessions), 4) if sessions > 0 else 0.0
            for outcome in OUTCOME_FOCUS
        }
        switch_bucket_rows.append(
            {
                "bucket": bucket_key,
                "label": SWITCH_BUCKET_LABELS[bucket_key],
                "sessions": sessions,
                "outcome_counts": sorted_counter_dict({k: int(v) for k, v in counts.items()}),
                "outcome_ratios": focus_ratios,
            }
        )

    sessions_total = len(meta_ids)
    sessions_faceted = len(aligned_ids)
    facet_coverage_ratio = round(sessions_faceted / sessions_total, 4) if sessions_total else 0.0
    parallel_stats = compute_parallel_session_stats(meta_rows)
    manifest_coverage_ratio = (
        round((sessions_total - manifest_missing_sessions) / sessions_total, 4)
        if sessions_total and manifest_supplied
        else 0.0
    )

    daily_activity_days = sorted(set(daily_sessions_counter.keys()))
    daily_activity_rows = [
        {
            "date": day,
            "sessions": int(daily_sessions_counter.get(day, 0)),
            "messages": int(daily_messages_counter.get(day, 0)),
            "tool_errors": int(daily_tool_errors_counter.get(day, 0)),
            "input_tokens": int(daily_input_tokens_counter.get(day, 0)),
            "output_tokens": int(daily_output_tokens_counter.get(day, 0)),
        }
        for day in daily_activity_days
    ]

    model_timeseries_days = sorted(daily_primary_model_counter.keys())
    model_timeseries_rows: List[Dict[str, Any]] = []
    for day in model_timeseries_days:
        counts = daily_primary_model_counter.get(day, Counter())
        model_timeseries_rows.append(
            {
                "date": day,
                "models": sorted_counter_dict({str(name): int(count) for name, count in counts.items()}),
            }
        )

    lifecycle_days = sorted(set(created_day_counter.keys()) | set(archived_day_counter.keys()))
    session_lifecycle_rows = [
        {
            "date": day,
            "created_sessions": int(created_day_counter.get(day, 0)),
            "archived_sessions": int(archived_day_counter.get(day, 0)),
        }
        for day in lifecycle_days
    ]

    archive_latency_stats = {
        "count": len(archive_latency_hours),
        "p50": round(percentile(archive_latency_hours, 0.5), 3),
        "p90": round(percentile(archive_latency_hours, 0.9), 3),
        "avg": round(average(archive_latency_hours), 3),
        "max": round(max(archive_latency_hours), 3) if archive_latency_hours else 0.0,
        "buckets": archive_latency_buckets(archive_latency_hours),
    }

    stats: Dict[str, Any] = {
        "version": "StatsV1",
        "generated_at": format_iso8601_z(max(end_times)),
        "sessions_total": sessions_total,
        "sessions_faceted": sessions_faceted,
        "facet_coverage_ratio": facet_coverage_ratio,
        "time_range": {
            "start_time": format_iso8601_z(min(start_times)),
            "end_time": format_iso8601_z(max(end_times)),
        },
        "totals": {
            "user_messages": total_user_messages,
            "assistant_messages": total_assistant_messages,
            "messages_total": total_user_messages + total_assistant_messages,
            "tool_calls": total_tool_calls,
            "tool_errors": total_tool_errors,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cached_input_tokens": total_cached_input_tokens,
            "git_commits": total_git_commits,
            "git_pushes": total_git_pushes,
            "files_modified_patch_only": total_files_modified,
            "lines_added_patch_only": total_lines_added,
            "lines_removed_patch_only": total_lines_removed,
            "uses_mcp_sessions": uses_mcp_sessions,
            "uses_task_agent_sessions": uses_task_agent_sessions,
            "uses_web_search_sessions": uses_web_search_sessions,
            "uses_web_fetch_sessions": uses_web_fetch_sessions,
        },
        "distributions": {
            "duration_minutes": {
                "buckets": duration_buckets(duration_values),
                "p50": round(percentile([float(v) for v in duration_values], 0.5), 3),
                "p90": round(percentile([float(v) for v in duration_values], 0.9), 3),
                "max": int(max(duration_values) if duration_values else 0),
            },
            "response_time_seconds": {
                "buckets": sorted_counter_dict(dict(response_time_counter)),
                "p50": round(percentile(response_times, 0.5), 3),
                "p90": round(percentile(response_times, 0.9), 3),
                "avg": round(average(response_times), 3),
                "max": round(max(response_times), 3) if response_times else 0.0,
            },
            "message_hours": sorted_counter_dict(dict(message_hour_counter)),
            "top_tools": sorted_counter_dict(dict(tool_counter), limit=25),
            "tool_error_categories": sorted_counter_dict(dict(tool_error_counter)),
            "top_projects": sorted_counter_dict(dict(project_counter), limit=20),
            "goal_categories": sorted_counter_dict(dict(goal_counter)),
            "outcome": sorted_counter_dict(dict(outcome_counter)),
            "assistant_helpfulness": sorted_counter_dict(dict(helpfulness_counter)),
            "session_type": sorted_counter_dict(dict(session_type_counter)),
            "primary_success": sorted_counter_dict(dict(primary_success_counter)),
            "friction_counts": sorted_counter_dict(dict(friction_counter)),
            "user_satisfaction": sorted_counter_dict(dict(satisfaction_counter)),
        },
        "rankings": {
            "top_tools": [
                {"name": name, "count": count}
                for name, count in sorted(tool_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            ],
            "top_projects": [
                {"project": name, "count": count}
                for name, count in sorted(project_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            ],
            "top_goal_categories": [
                {"name": name, "count": count}
                for name, count in sorted(goal_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            ],
            "top_friction": [
                {"name": name, "count": count}
                for name, count in sorted(friction_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            ],
        },
        "coverage_diagnostics": {
            "sessions_missing_facet": sorted(meta_ids - facet_ids),
            "facet_files_extra": sorted(facet_ids - meta_ids),
        },
        "parallel_sessions": parallel_stats,
        "concurrency_quality": {
            "max_concurrent_sessions": int(parallel_stats.get("max_concurrent_sessions", 0)),
            "overlap_minutes": float(parallel_stats.get("overlap_minutes", 0.0)),
            "sessions_involved": int(parallel_stats.get("sessions_involved", 0)),
            "overlap_session_share_percent": float(
                parallel_stats.get("overlap_session_share_percent", 0.0)
            ),
            "message_share_percent": float(parallel_stats.get("message_share_percent", 0.0)),
        },
        "model_outcome_matrix": {
            "outcomes_focus": OUTCOME_FOCUS,
            "models": model_outcome_rows,
            "coverage_sessions": int(sum(model_session_counts.values())),
        },
        "project_context_switching": {
            "switch_events_total": total_switch_events,
            "days_with_switches": days_with_switches,
            "daily": context_switch_daily_rows,
            "bucket_definitions": SWITCH_BUCKET_LABELS,
            "outcome_by_switch_bucket": switch_bucket_rows,
        },
        "time_series": {
            "daily_activity_utc": daily_activity_rows,
            "session_lifecycle_utc": session_lifecycle_rows,
            "model_sessions_daily_utc": model_timeseries_rows,
        },
        "sqlite_distributions": {
            "source": sorted_counter_dict(dict(source_counter)),
            "approval_mode": sorted_counter_dict(dict(approval_mode_counter)),
            "sandbox_type": sorted_counter_dict(dict(sandbox_type_counter)),
            "archived_status": sorted_counter_dict(dict(archived_status_counter)),
            "primary_model": sorted_counter_dict(dict(primary_model_counter)),
            "cli_version_major_minor": sorted_counter_dict(dict(cli_major_minor_counter)),
            "cli_release_channel": sorted_counter_dict(dict(cli_release_channel_counter)),
        },
        "sqlite_diagnostics": {
            "model_provider": sorted_counter_dict(dict(model_provider_counter)),
            "memory_mode": sorted_counter_dict(dict(memory_mode_counter)),
            "has_user_event": sorted_counter_dict(dict(has_user_event_counter)),
            "sandbox_network_access": sorted_counter_dict(dict(sandbox_network_counter)),
            "cli_version_raw": sorted_counter_dict(dict(cli_version_counter)),
        },
        "sqlite_lifecycle": {
            "manifest_supplied": manifest_supplied,
            "manifest_coverage_ratio": manifest_coverage_ratio,
            "manifest_missing_sessions": manifest_missing_sessions,
            "archived_session_latency_hours": archive_latency_stats,
            "model_signal_sessions": sessions_with_model_signal,
            "model_signal_coverage_ratio": (
                round(sessions_with_model_signal / sessions_total, 4) if sessions_total else 0.0
            ),
        },
        "session_rollups": session_rollups,
    }

    validate_stats_v1(stats)
    dump_json(args.out.resolve(), stats)
    print(f"Wrote stats: {args.out.resolve()} (sessions_total={sessions_total}, sessions_faceted={sessions_faceted})")


if __name__ == "__main__":
    main()
