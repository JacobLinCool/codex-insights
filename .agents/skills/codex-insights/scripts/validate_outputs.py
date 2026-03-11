#!/usr/bin/env python3
"""Validate codex-insights outputs and cross-artifact consistency."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict

from _shared import (
    ScriptError,
    iter_jsonl,
    load_json,
    parse_confidence,
    validate_facet_v1,
    validate_session_meta_v1,
    validate_stats_v1,
)

REQUIRED_REPORT_HEADINGS = [
    "At a Glance",
    "What You Work On",
    "How You Use Codex",
    "Wins",
    "Friction",
    "Feature Suggestions",
    "Patterns",
    "Horizon",
]


_PATH_PATTERN = re.compile(r"/(Users|home|mnt|var)/[^\s<>{}\"]+")


def load_records(directory: Path) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        data = load_json(path)
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ScriptError(f"Missing session_id in {path}")
        rows[session_id] = data
    return rows


def validate_scope_consistency(manifest_path: Path, manifest_meta_path: Path, scope: str) -> None:
    manifest_rows = list(iter_jsonl(manifest_path))
    manifest_count = len(manifest_rows)

    ids = [row.get("session_id") for row in manifest_rows]
    if any(not isinstance(sid, str) or not sid for sid in ids):
        raise ScriptError("manifest.jsonl contains invalid session_id values")
    if len(set(ids)) != len(ids):
        raise ScriptError("manifest.jsonl has duplicate session_id entries")

    if not manifest_meta_path.exists():
        raise ScriptError(f"Scope consistency failed: manifest meta file not found: {manifest_meta_path}")
    try:
        manifest_meta = json.loads(manifest_meta_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ScriptError(f"Scope consistency failed: manifest meta file unreadable: {exc}") from exc

    if not isinstance(manifest_meta, dict):
        raise ScriptError("Scope consistency failed: manifest meta must be a JSON object")
    if manifest_meta.get("scope") != scope:
        raise ScriptError(
            "Scope consistency failed: manifest meta scope mismatch "
            f"({manifest_meta.get('scope')} != {scope})"
        )
    thread_count = manifest_meta.get("threads_count_at_build")
    if not isinstance(thread_count, int) or thread_count < 0:
        raise ScriptError("Scope consistency failed: invalid threads_count_at_build in manifest meta")
    if manifest_count != thread_count:
        raise ScriptError(
            "Scope consistency failed: manifest row count does not match manifest meta snapshot count "
            f"({manifest_count} != {thread_count})"
        )


def validate_report(report_path: Path) -> str:
    if not report_path.exists():
        raise ScriptError(f"Report file not found: {report_path}")
    text = report_path.read_text(encoding="utf-8")
    for heading in REQUIRED_REPORT_HEADINGS:
        if heading not in text:
            raise ScriptError(f"Report missing required section heading: {heading}")
    return text


def validate_privacy_redacted(report_text: str, evidence_dir: Path | None) -> None:
    if _PATH_PATTERN.search(report_text):
        raise ScriptError("Redacted privacy check failed: absolute path found in report")

    if evidence_dir is None:
        return

    for path in sorted(evidence_dir.glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        if _PATH_PATTERN.search(raw):
            raise ScriptError(f"Redacted privacy check failed: absolute path found in evidence file {path}")
        if '"user_message.message"' in raw:
            raise ScriptError(f"Redacted privacy check failed: raw message field marker found in {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate codex-insights output artifacts")
    parser.add_argument("--meta-dir", required=True, type=Path, help="Directory containing meta/*.json")
    parser.add_argument("--facets-dir", required=True, type=Path, help="Directory containing facets/*.json")
    parser.add_argument("--stats", required=True, type=Path, help="Path to stats.json")
    parser.add_argument("--report", required=True, type=Path, help="Path to report.html")
    parser.add_argument("--manifest", type=Path, help="Optional manifest.jsonl for scope checks")
    parser.add_argument("--manifest-meta", type=Path, help="Optional manifest.meta.json for scope checks")
    parser.add_argument("--scope", default="sqlite_threads", choices=["sqlite_threads"], help="Manifest scope")
    parser.add_argument("--evidence-dir", type=Path, help="Optional evidence dir for privacy checks")
    parser.add_argument("--privacy", default="redacted", choices=["redacted", "raw", "none"], help="Privacy mode")
    args = parser.parse_args()

    meta_rows = load_records(args.meta_dir.resolve())
    if not meta_rows:
        raise ScriptError("No meta files found")

    facet_rows = load_records(args.facets_dir.resolve())
    if not facet_rows:
        raise ScriptError("No facet files found")

    for row in meta_rows.values():
        validate_session_meta_v1(row)
    for row in facet_rows.values():
        validate_facet_v1(row)

    meta_ids = set(meta_rows.keys())
    facet_ids = set(facet_rows.keys())

    unknown_facet_ids = sorted(facet_ids - meta_ids)
    if unknown_facet_ids:
        raise ScriptError(f"Facets contain unknown session IDs not present in meta: {unknown_facet_ids[:8]}")

    stats = load_json(args.stats.resolve())
    validate_stats_v1(stats)

    if args.privacy == "redacted":
        stats_text = json.dumps(stats, ensure_ascii=False)
        if _PATH_PATTERN.search(stats_text):
            raise ScriptError("Redacted privacy check failed: absolute path found in stats.json")

    sessions_total = int(stats["sessions_total"])
    sessions_faceted = int(stats["sessions_faceted"])

    expected_total = len(meta_ids)
    expected_faceted = len(meta_ids & facet_ids)
    expected_ratio = round(expected_faceted / expected_total, 4) if expected_total else 0.0

    if sessions_total != expected_total:
        raise ScriptError(f"stats.sessions_total mismatch ({sessions_total} != {expected_total})")
    if sessions_faceted != expected_faceted:
        raise ScriptError(f"stats.sessions_faceted mismatch ({sessions_faceted} != {expected_faceted})")

    actual_ratio = parse_confidence(stats["facet_coverage_ratio"], "facet_coverage_ratio")
    if round(actual_ratio, 4) != expected_ratio:
        raise ScriptError(
            "stats.facet_coverage_ratio mismatch "
            f"({actual_ratio:.4f} != {expected_ratio:.4f})"
        )

    diagnostics = stats.get("coverage_diagnostics") if isinstance(stats.get("coverage_diagnostics"), dict) else {}
    if diagnostics:
        missing = diagnostics.get("sessions_missing_facet")
        extra = diagnostics.get("facet_files_extra")
        if isinstance(missing, list) and sorted(missing) != sorted(meta_ids - facet_ids):
            raise ScriptError("coverage_diagnostics.sessions_missing_facet does not match derived values")
        if isinstance(extra, list) and sorted(extra) != sorted(facet_ids - meta_ids):
            raise ScriptError("coverage_diagnostics.facet_files_extra does not match derived values")

    time_series = stats.get("time_series") if isinstance(stats.get("time_series"), dict) else {}
    daily_activity = (
        time_series.get("daily_activity_utc")
        if isinstance(time_series.get("daily_activity_utc"), list)
        else []
    )
    if daily_activity:
        sessions_sum = 0
        tool_error_sum = 0
        for row in daily_activity:
            if not isinstance(row, dict):
                raise ScriptError("time_series.daily_activity_utc contains non-object row")
            sessions_val = row.get("sessions")
            tool_error_val = row.get("tool_errors")
            if not isinstance(sessions_val, int) or sessions_val < 0:
                raise ScriptError("time_series.daily_activity_utc.sessions must be non-negative integer")
            if not isinstance(tool_error_val, int) or tool_error_val < 0:
                raise ScriptError("time_series.daily_activity_utc.tool_errors must be non-negative integer")
            sessions_sum += sessions_val
            tool_error_sum += tool_error_val

        if sessions_sum != sessions_total:
            raise ScriptError(
                "time_series.daily_activity_utc session sum mismatch "
                f"({sessions_sum} != {sessions_total})"
            )

        totals = stats.get("totals") if isinstance(stats.get("totals"), dict) else {}
        total_tool_errors = totals.get("tool_errors")
        if isinstance(total_tool_errors, int) and total_tool_errors != tool_error_sum:
            raise ScriptError(
                "time_series.daily_activity_utc tool error sum mismatch "
                f"({tool_error_sum} != {total_tool_errors})"
            )

    sqlite_lifecycle = stats.get("sqlite_lifecycle") if isinstance(stats.get("sqlite_lifecycle"), dict) else {}
    sqlite_distributions = (
        stats.get("sqlite_distributions") if isinstance(stats.get("sqlite_distributions"), dict) else {}
    )
    manifest_supplied = bool(sqlite_lifecycle.get("manifest_supplied"))
    if manifest_supplied:
        manifest_missing = sqlite_lifecycle.get("manifest_missing_sessions", 0)
        if not isinstance(manifest_missing, int) or manifest_missing < 0:
            raise ScriptError("sqlite_lifecycle.manifest_missing_sessions must be non-negative integer")

        source_counter = (
            sqlite_distributions.get("source") if isinstance(sqlite_distributions.get("source"), dict) else {}
        )
        source_total = 0
        for value in source_counter.values():
            if not isinstance(value, int) or value < 0:
                raise ScriptError("sqlite_distributions.source values must be non-negative integers")
            source_total += value

        if source_total + manifest_missing != sessions_total:
            raise ScriptError(
                "sqlite_distributions.source does not align with manifest coverage "
                f"({source_total} + {manifest_missing} != {sessions_total})"
            )

    model_outcome = stats.get("model_outcome_matrix") if isinstance(stats.get("model_outcome_matrix"), dict) else {}
    if model_outcome:
        rows = model_outcome.get("models")
        coverage = model_outcome.get("coverage_sessions")
        if isinstance(rows, list):
            session_sum = 0
            for row in rows:
                if not isinstance(row, dict):
                    raise ScriptError("model_outcome_matrix.models contains non-object row")
                sessions = row.get("sessions")
                if not isinstance(sessions, int) or sessions < 0:
                    raise ScriptError("model_outcome_matrix.models.sessions must be non-negative integer")
                session_sum += sessions
            if isinstance(coverage, int) and session_sum != coverage:
                raise ScriptError(
                    "model_outcome_matrix coverage mismatch "
                    f"({session_sum} != {coverage})"
                )
            if session_sum != sessions_faceted:
                raise ScriptError(
                    "model_outcome_matrix session sum must equal sessions_faceted "
                    f"({session_sum} != {sessions_faceted})"
                )

    project_switching = (
        stats.get("project_context_switching")
        if isinstance(stats.get("project_context_switching"), dict)
        else {}
    )
    if project_switching:
        daily = project_switching.get("daily")
        if isinstance(daily, list):
            total_daily_sessions = 0
            for row in daily:
                if not isinstance(row, dict):
                    raise ScriptError("project_context_switching.daily contains non-object row")
                sessions_val = row.get("sessions")
                switch_val = row.get("switch_events")
                if not isinstance(sessions_val, int) or sessions_val < 0:
                    raise ScriptError("project_context_switching.daily.sessions must be non-negative integer")
                if not isinstance(switch_val, int) or switch_val < 0:
                    raise ScriptError("project_context_switching.daily.switch_events must be non-negative integer")
                total_daily_sessions += sessions_val
            if total_daily_sessions != sessions_total:
                raise ScriptError(
                    "project_context_switching daily session sum mismatch "
                    f"({total_daily_sessions} != {sessions_total})"
                )

    concurrency_quality = (
        stats.get("concurrency_quality") if isinstance(stats.get("concurrency_quality"), dict) else {}
    )
    if concurrency_quality:
        max_concurrent = concurrency_quality.get("max_concurrent_sessions")
        overlap_minutes = concurrency_quality.get("overlap_minutes")
        if not isinstance(max_concurrent, int) or max_concurrent < 0:
            raise ScriptError("concurrency_quality.max_concurrent_sessions must be non-negative integer")
        if not isinstance(overlap_minutes, (int, float)) or overlap_minutes < 0:
            raise ScriptError("concurrency_quality.overlap_minutes must be non-negative number")

    session_rollups = stats.get("session_rollups")
    if session_rollups is not None:
        if not isinstance(session_rollups, list):
            raise ScriptError("session_rollups must be an array when present")
        if len(session_rollups) != sessions_total:
            raise ScriptError(
                "session_rollups length mismatch "
                f"({len(session_rollups)} != {sessions_total})"
            )
        rollup_ids: set[str] = set()
        rollup_tool_errors = 0
        rollup_input_tokens = 0
        rollup_output_tokens = 0
        for row in session_rollups:
            if not isinstance(row, dict):
                raise ScriptError("session_rollups contains non-object row")
            sid = row.get("session_id")
            if not isinstance(sid, str) or not sid:
                raise ScriptError("session_rollups row missing valid session_id")
            rollup_ids.add(sid)
            tool_errors = row.get("tool_errors")
            input_tokens = row.get("input_tokens")
            output_tokens = row.get("output_tokens")
            if not isinstance(tool_errors, int) or tool_errors < 0:
                raise ScriptError("session_rollups.tool_errors must be non-negative integer")
            if not isinstance(input_tokens, int) or input_tokens < 0:
                raise ScriptError("session_rollups.input_tokens must be non-negative integer")
            if not isinstance(output_tokens, int) or output_tokens < 0:
                raise ScriptError("session_rollups.output_tokens must be non-negative integer")
            rollup_tool_errors += tool_errors
            rollup_input_tokens += input_tokens
            rollup_output_tokens += output_tokens
        if rollup_ids != meta_ids:
            raise ScriptError("session_rollups session IDs must exactly match meta session IDs")
        totals = stats.get("totals") if isinstance(stats.get("totals"), dict) else {}
        if isinstance(totals.get("tool_errors"), int) and totals.get("tool_errors") != rollup_tool_errors:
            raise ScriptError("session_rollups tool_errors sum mismatch totals.tool_errors")
        if isinstance(totals.get("input_tokens"), int) and totals.get("input_tokens") != rollup_input_tokens:
            raise ScriptError("session_rollups input_tokens sum mismatch totals.input_tokens")
        if isinstance(totals.get("output_tokens"), int) and totals.get("output_tokens") != rollup_output_tokens:
            raise ScriptError("session_rollups output_tokens sum mismatch totals.output_tokens")

    report_text = validate_report(args.report.resolve())

    if args.privacy == "redacted":
        evidence_dir = args.evidence_dir.resolve() if args.evidence_dir else None
        validate_privacy_redacted(report_text, evidence_dir)

    if args.manifest and args.manifest_meta:
        validate_scope_consistency(args.manifest.resolve(), args.manifest_meta.resolve(), args.scope)

    print("Validation passed")
    print(f"  meta sessions:   {len(meta_rows)}")
    print(f"  facet sessions:  {len(facet_rows)}")
    print(f"  coverage ratio:  {expected_ratio:.4f}")


if __name__ == "__main__":
    main()
