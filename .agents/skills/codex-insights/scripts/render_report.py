#!/usr/bin/env python3
"""Render Codex Insights HTML report from stats + narrative."""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from _shared import ScriptError, load_json, parse_iso8601_utc

SECTION_TITLE_MAP = {
    "at_a_glance": "At a Glance",
    "what_you_work_on": "What You Work On",
    "how_you_use_codex": "How You Use Codex",
    "wins": "Wins",
    "friction": "Friction",
    "feature_suggestions": "Feature Suggestions",
    "patterns": "Patterns",
    "horizon": "Horizon",
}

NARRATIVE_BANNED_TERMS = re.compile(
    r"\b(p(?:50|90|95|99)|median|average|avg|percentile|percentiles)\b",
    flags=re.IGNORECASE,
)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def format_compact_count(value: int) -> str:
    n = int(value)
    abs_n = abs(n)
    if abs_n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:.1f}T"
    if abs_n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if abs_n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs_n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def format_utc_human(ts: Any) -> str:
    if not isinstance(ts, str) or not ts:
        return ""
    try:
        dt = parse_iso8601_utc(ts)
    except Exception:  # noqa: BLE001
        return str(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_window_human(start_ts: Any, end_ts: Any) -> str:
    if not isinstance(start_ts, str) or not isinstance(end_ts, str):
        return f"{start_ts} to {end_ts}"
    try:
        start = parse_iso8601_utc(start_ts)
        end = parse_iso8601_utc(end_ts)
    except Exception:  # noqa: BLE001
        return f"{start_ts} to {end_ts}"
    if end < start:
        end = start
    duration = end - start
    return f"{format_utc_human(start_ts)} to {format_utc_human(end_ts)} ({format_duration(duration)})"


def format_duration(duration: timedelta) -> str:
    total_seconds = int(max(duration.total_seconds(), 0))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def sorted_rows(counter: Dict[str, Any], *, limit: int = 12) -> List[Tuple[str, int]]:
    rows: List[Tuple[str, int]] = []
    if not isinstance(counter, dict):
        return rows
    for key, value in counter.items():
        if isinstance(key, str) and isinstance(value, int):
            rows.append((key, value))
    rows.sort(key=lambda kv: (-kv[1], kv[0]))
    return rows[:limit]


def render_counter_table(title: str, counter: Dict[str, Any], *, limit: int = 12) -> str:
    rows = sorted_rows(counter, limit=limit)
    if not rows:
        return (
            f"<section class=\"card\"><h3>{esc(title)}</h3>"
            "<p class=\"muted\">No data.</p></section>"
        )

    body = "".join(
        f"<tr><td>{esc(name)}</td><td class=\"num\">{esc(format_compact_count(count))}</td></tr>"
        for name, count in rows
    )
    return (
        f"<section class=\"card\"><h3>{esc(title)}</h3>"
        "<table><thead><tr><th>Label</th><th class=\"num\">Count</th></tr></thead>"
        f"<tbody>{body}</tbody></table></section>"
    )


def render_sqlite_diagnostics_tables(stats: Dict[str, Any]) -> str:
    sqlite_diagnostics = (
        stats.get("sqlite_diagnostics") if isinstance(stats.get("sqlite_diagnostics"), dict) else {}
    )
    if not sqlite_diagnostics:
        return ""

    table_specs = [
        ("Diagnostics: Model Provider", "model_provider", 8),
        ("Diagnostics: Memory Mode", "memory_mode", 8),
        ("Diagnostics: User Event Flag", "has_user_event", 8),
        ("Diagnostics: Sandbox Network Access", "sandbox_network_access", 10),
        ("Diagnostics: Raw CLI Version", "cli_version_raw", 15),
    ]

    cards: List[str] = []
    for title, key, limit in table_specs:
        counter = sqlite_diagnostics.get(key)
        if isinstance(counter, dict):
            cards.append(render_counter_table(title, counter, limit=limit))

    if not cards:
        return ""

    header = (
        "<section class=\"card span-full\">"
        "<h3>SQLite Diagnostics (Low-Signal Fields)</h3>"
        "<p class=\"muted\">Moved out of primary visuals to reduce scan cost.</p>"
        "</section>"
    )
    return header + "".join(cards)


def render_narrative_section(key: str, sections: Dict[str, Any]) -> str:
    data = sections.get(key)
    canonical_title = SECTION_TITLE_MAP[key]
    if not isinstance(data, dict):
        return (
            f"<section class=\"section\"><h2>{esc(canonical_title)}</h2>"
            "<p class=\"muted\">No narrative content.</p></section>"
        )

    title = str(data.get("title") or canonical_title)
    summary = sanitize_narrative_text(str(data.get("summary") or ""))
    subtitle = ""
    if title.strip().lower() != canonical_title.lower():
        subtitle = f"<p class=\"muted\">{esc(title)}</p>"

    bullets_html = ""
    bullets = data.get("bullets")
    if isinstance(bullets, list):
        items = [
            f"<li>{esc(clean)}</li>"
            for item in bullets
            if isinstance(item, str)
            for clean in [sanitize_narrative_text(item)]
            if clean.strip()
        ]
        if items:
            bullets_html = f"<ul>{''.join(items)}</ul>"

    return (
        f"<section class=\"section\"><h2>{esc(canonical_title)}</h2>"
        f"{subtitle}<p>{esc(summary)}</p>{bullets_html}</section>"
    )


def sanitize_narrative_text(text: str) -> str:
    compact = " ".join(str(text or "").split())
    if not compact:
        return ""
    chunks = re.split(r"(?<=[.!?])\s+|;\s+", compact)
    kept = [chunk.strip() for chunk in chunks if chunk.strip() and not NARRATIVE_BANNED_TERMS.search(chunk)]
    if kept:
        return " ".join(kept)
    return "Distribution details are shown in the charts."


def render_metric_cards(stats: Dict[str, Any]) -> str:
    sessions_total = int(stats.get("sessions_total", 0))
    sessions_faceted = int(stats.get("sessions_faceted", 0))
    coverage = float(stats.get("facet_coverage_ratio", 0.0))

    totals = stats.get("totals") if isinstance(stats.get("totals"), dict) else {}
    sqlite_distributions = (
        stats.get("sqlite_distributions") if isinstance(stats.get("sqlite_distributions"), dict) else {}
    )
    archived_status = (
        sqlite_distributions.get("archived_status")
        if isinstance(sqlite_distributions.get("archived_status"), dict)
        else {}
    )
    tool_calls = int(totals.get("tool_calls", 0))
    tool_errors = int(totals.get("tool_errors", 0))
    input_tokens = int(totals.get("input_tokens", 0))
    output_tokens = int(totals.get("output_tokens", 0))
    archived_sessions = int(archived_status.get("archived", 0))
    archived_ratio = (archived_sessions / sessions_total * 100.0) if sessions_total else 0.0

    cards = [
        ("sessions_total", "Sessions", sessions_total, "int"),
        ("sessions_faceted", "Faceted Sessions", sessions_faceted, "int"),
        ("facet_coverage", "Facet Coverage", coverage * 100.0, "pct2"),
        ("tool_calls", "Tool Calls", tool_calls, "int"),
        ("tool_errors", "Tool Errors", tool_errors, "int"),
        ("input_tokens", "Input Tokens", input_tokens, "int"),
        ("output_tokens", "Output Tokens", output_tokens, "int"),
        ("archived_sessions", "Archived Sessions", archived_sessions, "int"),
        ("archived_ratio", "Archived Ratio", archived_ratio, "pct1"),
    ]

    html_rows: List[str] = []
    for key, label, value, kind in cards:
        if kind == "int":
            display = format_compact_count(int(value))
            raw = str(int(value))
        elif kind == "pct2":
            display = f"{float(value):.2f}%"
            raw = f"{float(value):.6f}"
        elif kind == "pct1":
            display = f"{float(value):.1f}%"
            raw = f"{float(value):.6f}"
        else:
            display = str(value)
            raw = str(value)
        html_rows.append(
            "<div class=\"metric\" "
            f"data-metric=\"{esc(key)}\" data-kind=\"{esc(kind)}\" data-raw=\"{esc(raw)}\">"
            f"<div class=\"metric-label\">{esc(label)}</div>"
            f"<div class=\"metric-value\">{esc(display)}</div>"
            "</div>"
        )
    return "".join(html_rows)


def build_chart_payload(stats: Dict[str, Any]) -> Dict[str, Any]:
    distributions = stats.get("distributions") if isinstance(stats.get("distributions"), dict) else {}
    sqlite_distributions = (
        stats.get("sqlite_distributions") if isinstance(stats.get("sqlite_distributions"), dict) else {}
    )
    sqlite_lifecycle = stats.get("sqlite_lifecycle") if isinstance(stats.get("sqlite_lifecycle"), dict) else {}
    time_series = stats.get("time_series") if isinstance(stats.get("time_series"), dict) else {}

    response = (
        distributions.get("response_time_seconds")
        if isinstance(distributions.get("response_time_seconds"), dict)
        else {}
    )
    response_buckets = response.get("buckets") if isinstance(response.get("buckets"), dict) else {}
    tool_errors = (
        distributions.get("tool_error_categories")
        if isinstance(distributions.get("tool_error_categories"), dict)
        else {}
    )
    message_hours = distributions.get("message_hours") if isinstance(distributions.get("message_hours"), dict) else {}
    parallel = stats.get("parallel_sessions") if isinstance(stats.get("parallel_sessions"), dict) else {}
    source_distribution = (
        sqlite_distributions.get("source") if isinstance(sqlite_distributions.get("source"), dict) else {}
    )
    approval_distribution = (
        sqlite_distributions.get("approval_mode")
        if isinstance(sqlite_distributions.get("approval_mode"), dict)
        else {}
    )
    sandbox_distribution = (
        sqlite_distributions.get("sandbox_type")
        if isinstance(sqlite_distributions.get("sandbox_type"), dict)
        else {}
    )
    cli_distribution = (
        sqlite_distributions.get("cli_version_major_minor")
        if isinstance(sqlite_distributions.get("cli_version_major_minor"), dict)
        else {}
    )
    archived_status = (
        sqlite_distributions.get("archived_status")
        if isinstance(sqlite_distributions.get("archived_status"), dict)
        else {}
    )
    archive_latency = (
        sqlite_lifecycle.get("archived_session_latency_hours")
        if isinstance(sqlite_lifecycle.get("archived_session_latency_hours"), dict)
        else {}
    )
    archive_latency_buckets = (
        archive_latency.get("buckets") if isinstance(archive_latency.get("buckets"), dict) else {}
    )
    daily_activity = (
        time_series.get("daily_activity_utc")
        if isinstance(time_series.get("daily_activity_utc"), list)
        else []
    )
    model_sessions_daily = (
        time_series.get("model_sessions_daily_utc")
        if isinstance(time_series.get("model_sessions_daily_utc"), list)
        else []
    )
    concurrency_quality = (
        stats.get("concurrency_quality") if isinstance(stats.get("concurrency_quality"), dict) else {}
    )
    model_outcome_matrix = (
        stats.get("model_outcome_matrix") if isinstance(stats.get("model_outcome_matrix"), dict) else {}
    )
    project_context_switching = (
        stats.get("project_context_switching")
        if isinstance(stats.get("project_context_switching"), dict)
        else {}
    )
    time_range = stats.get("time_range") if isinstance(stats.get("time_range"), dict) else {}
    session_rollups = (
        stats.get("session_rollups") if isinstance(stats.get("session_rollups"), list) else []
    )

    ordered_response_labels = ["2-10s", "10-30s", "30s-1m", "1-2m", "2-5m", "5-15m", ">15m", "<2s"]
    response_items: List[Dict[str, Any]] = []
    for label in ordered_response_labels:
        count = int(response_buckets.get(label, 0))
        if count > 0:
            response_items.append({"label": label, "count": count})
    if not response_items:
        response_items = [{"label": label, "count": int(response_buckets.get(label, 0))} for label in ordered_response_labels]

    ordered_latency_labels = ["<10m", "10-30m", "30-60m", "1-6h", "6-24h", "1-3d", ">3d"]
    archive_latency_items: List[Dict[str, Any]] = []
    for label in ordered_latency_labels:
        count = int(archive_latency_buckets.get(label, 0))
        if count > 0:
            archive_latency_items.append({"label": label, "count": count})
    if not archive_latency_items:
        archive_latency_items = [
            {"label": label, "count": int(archive_latency_buckets.get(label, 0))}
            for label in ordered_latency_labels
        ]

    tool_error_items = [
        {"label": key, "count": int(value)}
        for key, value in sorted_rows(tool_errors, limit=12)
    ]
    source_items = [{"label": key, "count": int(value)} for key, value in sorted_rows(source_distribution, limit=10)]
    approval_items = [
        {"label": key, "count": int(value)}
        for key, value in sorted_rows(approval_distribution, limit=10)
    ]
    sandbox_items = [
        {"label": key, "count": int(value)}
        for key, value in sorted_rows(sandbox_distribution, limit=10)
    ]
    cli_items = [{"label": key, "count": int(value)} for key, value in sorted_rows(cli_distribution, limit=10)]
    daily_items: List[Dict[str, Any]] = []
    for row in daily_activity:
        if not isinstance(row, dict):
            continue
        date = row.get("date")
        if not isinstance(date, str):
            continue
        daily_items.append(
            {
                "date": date,
                "sessions": int(row.get("sessions", 0)),
                "messages": int(row.get("messages", 0)),
                "tool_errors": int(row.get("tool_errors", 0)),
            }
        )

    model_daily_rows: List[Dict[str, Any]] = []
    all_models: set[str] = set()
    for row in model_sessions_daily:
        if not isinstance(row, dict):
            continue
        date = row.get("date")
        models = row.get("models")
        if not isinstance(date, str) or not isinstance(models, dict):
            continue
        clean_models: Dict[str, int] = {}
        for name, count in models.items():
            if isinstance(name, str) and name and isinstance(count, int) and count >= 0:
                clean_models[name] = count
                all_models.add(name)
        model_daily_rows.append({"date": date, "models": clean_models})

    model_names = sorted(all_models)
    model_series: List[Dict[str, Any]] = []
    for model in model_names:
        values: List[int] = []
        for row in model_daily_rows:
            models = row.get("models", {})
            values.append(int(models.get(model, 0)))
        model_series.append({"model": model, "values": values})

    focus_outcomes_raw = model_outcome_matrix.get("outcomes_focus")
    focus_outcomes = (
        [x for x in focus_outcomes_raw if isinstance(x, str) and x]
        if isinstance(focus_outcomes_raw, list)
        else ["fully_achieved", "mostly_achieved", "not_achieved"]
    )
    matrix_rows_raw = model_outcome_matrix.get("models")
    matrix_rows = matrix_rows_raw if isinstance(matrix_rows_raw, list) else []
    matrix_models: List[str] = []
    matrix_rows_payload: List[Dict[str, Any]] = []
    matrix_cells: List[List[Any]] = []
    for y_idx, row in enumerate(matrix_rows):
        if not isinstance(row, dict):
            continue
        model_name = row.get("model")
        sessions = row.get("sessions")
        ratios = row.get("outcome_ratios")
        counts = row.get("outcome_counts")
        if (
            not isinstance(model_name, str)
            or not model_name
            or not isinstance(sessions, int)
            or sessions < 0
            or not isinstance(ratios, dict)
            or not isinstance(counts, dict)
        ):
            continue
        matrix_models.append(model_name)
        clean_ratio: Dict[str, float] = {}
        clean_counts: Dict[str, int] = {}
        for x_idx, outcome in enumerate(focus_outcomes):
            ratio_val = float(ratios.get(outcome, 0.0))
            count_val = int(counts.get(outcome, 0))
            ratio_pct = round(max(0.0, min(1.0, ratio_val)) * 100.0, 2)
            clean_ratio[outcome] = ratio_pct
            clean_counts[outcome] = count_val
            matrix_cells.append([x_idx, y_idx, ratio_pct, count_val])
        matrix_rows_payload.append(
            {
                "model": model_name,
                "sessions": sessions,
                "ratios_percent": clean_ratio,
                "counts": clean_counts,
            }
        )

    project_daily_raw = project_context_switching.get("daily")
    project_daily_rows: List[Dict[str, Any]] = []
    if isinstance(project_daily_raw, list):
        for row in project_daily_raw:
            if not isinstance(row, dict):
                continue
            date = row.get("date")
            if not isinstance(date, str):
                continue
            project_daily_rows.append(
                {
                    "date": date,
                    "sessions": int(row.get("sessions", 0)),
                    "switch_events": int(row.get("switch_events", 0)),
                    "unique_projects": int(row.get("unique_projects", 0)),
                }
            )

    switch_bucket_raw = project_context_switching.get("outcome_by_switch_bucket")
    switch_bucket_rows: List[Dict[str, Any]] = []
    if isinstance(switch_bucket_raw, list):
        for row in switch_bucket_raw:
            if not isinstance(row, dict):
                continue
            label = row.get("label")
            sessions = row.get("sessions")
            ratios = row.get("outcome_ratios")
            if not isinstance(label, str) or not isinstance(sessions, int) or not isinstance(ratios, dict):
                continue
            switch_bucket_rows.append(
                {
                    "label": label,
                    "sessions": sessions,
                    "fully_achieved": round(float(ratios.get("fully_achieved", 0.0)) * 100.0, 2),
                    "mostly_achieved": round(float(ratios.get("mostly_achieved", 0.0)) * 100.0, 2),
                    "not_achieved": round(float(ratios.get("not_achieved", 0.0)) * 100.0, 2),
                }
            )

    return {
        "time_range": {
            "start_time": str(time_range.get("start_time") or ""),
            "end_time": str(time_range.get("end_time") or ""),
        },
        "session_rollups": session_rollups,
        "response_time_distribution": {
            "items": response_items,
            "count": sum(item["count"] for item in response_items),
        },
        "daily_activity_utc": daily_items,
        "model_sessions_time_series": {
            "dates": [row["date"] for row in model_daily_rows],
            "series": model_series,
        },
        "model_outcome_matrix": {
            "outcomes": focus_outcomes,
            "models": matrix_models,
            "cells": matrix_cells,
            "rows": matrix_rows_payload,
        },
        "parallel_sessions": {
            "overlap_events": int(parallel.get("overlap_events", 0)),
            "sessions_involved": int(parallel.get("sessions_involved", 0)),
            "message_share_percent": float(parallel.get("message_share_percent", 0.0)),
        },
        "concurrency_quality": {
            "max_concurrent_sessions": int(concurrency_quality.get("max_concurrent_sessions", 0)),
            "overlap_minutes": float(concurrency_quality.get("overlap_minutes", 0.0)),
            "sessions_involved": int(concurrency_quality.get("sessions_involved", 0)),
            "overlap_session_share_percent": float(
                concurrency_quality.get("overlap_session_share_percent", 0.0)
            ),
            "message_share_percent": float(concurrency_quality.get("message_share_percent", 0.0)),
        },
        "project_context_switching": {
            "daily": project_daily_rows,
            "switch_events_total": int(project_context_switching.get("switch_events_total", 0)),
            "days_with_switches": int(project_context_switching.get("days_with_switches", 0)),
            "outcome_by_bucket": switch_bucket_rows,
        },
        "message_hours_utc": {str(k): int(v) for k, v in message_hours.items()},
        "tool_error_categories": tool_error_items,
        "source_distribution": source_items,
        "approval_mode_distribution": approval_items,
        "sandbox_type_distribution": sandbox_items,
        "cli_version_distribution": cli_items,
        "archived_status": {
            "active": int(archived_status.get("active", 0)),
            "archived": int(archived_status.get("archived", 0)),
        },
        "archive_latency_distribution": {
            "count": int(archive_latency.get("count", 0)),
            "items": archive_latency_items,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Render report.html from stats.json + narrative.json")
    parser.add_argument("--stats", required=True, type=Path, help="Path to stats.json")
    parser.add_argument("--narrative", required=True, type=Path, help="Path to narrative.json")
    parser.add_argument("--template", required=True, type=Path, help="Path to HTML template")
    parser.add_argument("--out", required=True, type=Path, help="Output report.html path")
    args = parser.parse_args()

    stats = load_json(args.stats.resolve())
    narrative = load_json(args.narrative.resolve())

    if not isinstance(narrative, dict) or not isinstance(narrative.get("sections"), dict):
        raise ScriptError("Narrative JSON missing sections object")

    sections = narrative["sections"]

    distributions = stats.get("distributions") if isinstance(stats.get("distributions"), dict) else {}

    html_template = args.template.resolve().read_text(encoding="utf-8")

    time_range = stats.get("time_range") if isinstance(stats.get("time_range"), dict) else {}
    start_time_raw = time_range.get("start_time", "")
    end_time_raw = time_range.get("end_time", "")

    replacements = {
        "{{GENERATED_AT}}": esc(format_utc_human(stats.get("generated_at", ""))),
        "{{TIME_RANGE_START}}": esc(format_window_human(start_time_raw, end_time_raw)),
        "{{TIME_RANGE_END}}": "",
        "{{METRIC_CARDS}}": render_metric_cards(stats),
        "{{CHART_DATA_JSON}}": json.dumps(build_chart_payload(stats), ensure_ascii=False),
        "{{AT_A_GLANCE}}": render_narrative_section("at_a_glance", sections),
        "{{WHAT_YOU_WORK_ON}}": render_narrative_section("what_you_work_on", sections),
        "{{HOW_YOU_USE_CODEX}}": render_narrative_section("how_you_use_codex", sections),
        "{{WINS}}": render_narrative_section("wins", sections),
        "{{FRICTION}}": render_narrative_section("friction", sections),
        "{{FEATURE_SUGGESTIONS}}": render_narrative_section("feature_suggestions", sections),
        "{{PATTERNS}}": render_narrative_section("patterns", sections),
        "{{HORIZON}}": render_narrative_section("horizon", sections),
        "{{TOP_TOOLS_TABLE}}": render_counter_table(
            "Top Tools",
            distributions.get("top_tools", {}),
            limit=15,
        ),
        "{{OUTCOME_TABLE}}": render_counter_table(
            "Outcomes",
            distributions.get("outcome", {}),
            limit=8,
        ),
        "{{FRICTION_TABLE}}": render_counter_table(
            "Friction Breakdown",
            distributions.get("friction_counts", {}),
            limit=10,
        ),
        "{{SATISFACTION_TABLE}}": render_counter_table(
            "User Satisfaction Signals",
            distributions.get("user_satisfaction", {}),
            limit=8,
        ),
        "{{TOOL_ERROR_TABLE}}": render_counter_table(
            "Tool Error Categories",
            distributions.get("tool_error_categories", {}),
            limit=10,
        ),
        "{{SQLITE_DIAGNOSTICS_TABLES}}": render_sqlite_diagnostics_tables(stats),
    }

    rendered = html_template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)

    args.out.resolve().write_text(rendered, encoding="utf-8")
    print(f"Wrote report: {args.out.resolve()}")


if __name__ == "__main__":
    main()
