#!/usr/bin/env python3
"""Shared helpers for codex-insights scripts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

SESSION_META_REQUIRED_KEYS = {
    "session_id",
    "project_path",
    "start_time",
    "end_time",
    "duration_minutes",
    "user_message_count",
    "assistant_message_count",
    "tool_counts",
    "tool_errors",
    "tool_error_categories",
    "uses_mcp",
    "uses_task_agent",
    "uses_web_search",
    "uses_web_fetch",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "first_prompt_redacted",
    "user_interruptions",
    "user_response_times",
    "message_hours",
    "git_commits",
    "git_pushes",
    "git_branch",
    "git_sha",
    "files_modified_patch_only",
    "lines_added_patch_only",
    "lines_removed_patch_only",
    "derivation_notes",
}

FACET_REQUIRED_KEYS = {
    "session_id",
    "underlying_goal",
    "goal_categories",
    "outcome",
    "assistant_helpfulness",
    "session_type",
    "primary_success",
    "friction_counts",
    "friction_detail",
    "user_satisfaction_counts",
    "brief_summary",
    "confidence",
    "evidence_flags",
}

OUTCOME_VALUES = {
    "fully_achieved",
    "mostly_achieved",
    "partially_achieved",
    "not_achieved",
    "unclear_from_transcript",
}

HELPFULNESS_VALUES = {
    "essential",
    "very_helpful",
    "moderately_helpful",
    "slightly_helpful",
    "unhelpful",
}

SESSION_TYPE_VALUES = {
    "single_task",
    "multi_task",
    "iterative_refinement",
    "exploration",
}

PRIMARY_SUCCESS_VALUES = {
    "multi_file_changes",
    "good_explanations",
    "correct_code_edits",
    "proactive_help",
    "good_debugging",
    "none",
}

FRICTION_KEYS = {
    "wrong_approach",
    "buggy_code",
    "misunderstood_request",
    "user_rejected_action",
    "excessive_planning",
    "excessive_changes",
    "external_limit",
}

SATISFACTION_KEYS = {
    "likely_satisfied",
    "satisfied",
    "neutral",
    "dissatisfied",
    "frustrated",
}

GOAL_CATEGORY_KEYS = {
    "bug_fix",
    "feature_implementation",
    "code_review",
    "code_analysis",
    "refactor",
    "documentation",
    "planning",
    "testing",
    "performance_optimization",
    "tooling",
    "ui_ux",
    "research",
    "devops",
    "workflow_automation",
}

_REDACT_PATTERNS = [
    (re.compile(r"https?://\S+"), "<URL>"),
    (re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+"), "<PATH>"),
    (re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
]


class ScriptError(RuntimeError):
    """Raised for expected script failures with user-facing messages."""


def parse_iso8601_utc(ts: str) -> datetime:
    """Parse ISO8601 timestamps with trailing Z into timezone-aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def format_iso8601_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            yield json.loads(raw)


def find_rollout_files(sessions_root: Path, archived_root: Path) -> List[Path]:
    files = list(sessions_root.rglob("rollout-*.jsonl"))
    files.extend(archived_root.glob("rollout-*.jsonl"))
    return sorted({p.resolve() for p in files})


def extract_session_meta_from_rollout(rollout_path: Path) -> Optional[Dict[str, Any]]:
    for idx, row in enumerate(iter_jsonl(rollout_path)):
        if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict):
            return row["payload"]
        if idx > 64:
            break
    return None


def redact_text(text: str, max_len: int = 220) -> str:
    text = text or ""
    compact = re.sub(r"\s+", " ", text).strip()
    for pattern, replacement in _REDACT_PATTERNS:
        compact = pattern.sub(replacement, compact)
    if len(compact) > max_len:
        compact = compact[: max_len - 1] + "…"
    return compact


def parse_function_arguments(arguments_text: str) -> Dict[str, Any]:
    if not arguments_text:
        return {}
    try:
        data = json.loads(arguments_text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        return {}
    return {}


def classify_tool_error(text: str, exit_code: Optional[int]) -> str:
    lower = (text or "").lower()
    if "rejected" in lower and ("blocked by policy" in lower or "user rejected" in lower):
        return "User Rejected"
    if "file too large" in lower:
        return "File Too Large"
    if "file not found" in lower or "no such file" in lower:
        return "File Not Found"
    if "apply_patch" in lower and "failed" in lower:
        return "Edit Failed"
    if exit_code is not None and exit_code != 0:
        return "Command Failed"
    if "failed" in lower or "error" in lower:
        return "Other"
    return "Command Failed"


def parse_exit_code_from_output(output: str) -> Optional[int]:
    if not output:
        return None
    match = re.search(r"Process exited with code (-?\d+)", output)
    if match:
        return int(match.group(1))
    match = re.search(r'"exit_code"\s*:\s*(-?\d+)', output)
    if match:
        return int(match.group(1))
    return None


def parse_apply_patch_stats(patch_text: str) -> Tuple[set[str], int, int]:
    files: set[str] = set()
    lines_added = 0
    lines_removed = 0
    for raw in (patch_text or "").splitlines():
        if raw.startswith("*** Update File: "):
            files.add(raw.split(": ", 1)[1].strip())
            continue
        if raw.startswith("*** Add File: "):
            files.add(raw.split(": ", 1)[1].strip())
            continue
        if raw.startswith("*** Delete File: "):
            files.add(raw.split(": ", 1)[1].strip())
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lines_added += 1
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            lines_removed += 1
            continue
    return files, lines_added, lines_removed


def extract_json_object_from_text(text: str) -> Dict[str, Any]:
    """Extract first JSON object from model output text."""
    start = text.find("{")
    if start < 0:
        raise ScriptError("No JSON object found in model output.")

    depth = 0
    in_string = False
    escaped = False
    end = -1
    for i, ch in enumerate(text[start:], start=start):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        raise ScriptError("Unterminated JSON object in model output.")

    try:
        obj = json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        raise ScriptError(f"Failed to parse model JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ScriptError("Model JSON output is not an object.")
    return obj


def run_codex_json(model: str, prompt: str, timeout_sec: int = 180) -> Dict[str, Any]:
    cmd = ["codex", "exec", "--skip-git-repo-check", "-m", model, prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise ScriptError(f"codex exec failed (exit {proc.returncode}): {output[:800]}")
    return extract_json_object_from_text(output)


def sorted_counter_dict(counter: Dict[str, int], limit: Optional[int] = None) -> Dict[str, int]:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit is not None:
        items = items[:limit]
    return {k: v for k, v in items}


def response_time_bucket(seconds: float) -> str:
    if seconds < 2:
        return "<2s"
    if seconds < 10:
        return "2-10s"
    if seconds < 30:
        return "10-30s"
    if seconds < 60:
        return "30s-1m"
    if seconds < 120:
        return "1-2m"
    if seconds < 300:
        return "2-5m"
    if seconds < 900:
        return "5-15m"
    return ">15m"


def parse_non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ScriptError(f"{field_name} must be a non-negative integer")
    return value


def parse_confidence(value: Any, field_name: str = "confidence") -> float:
    if not isinstance(value, (int, float)):
        raise ScriptError(f"{field_name} must be numeric")
    f = float(value)
    if f < 0.0 or f > 1.0:
        raise ScriptError(f"{field_name} must be in [0, 1]")
    return round(f, 4)


def normalize_small_int_dict(
    data: Any,
    *,
    allowed_keys: Optional[set[str]] = None,
    field_name: str,
) -> Dict[str, int]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ScriptError(f"{field_name} must be an object")
    out: Dict[str, int] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise ScriptError(f"{field_name} contains invalid key")
        if allowed_keys is not None and key not in allowed_keys:
            raise ScriptError(f"{field_name} key '{key}' is not allowed")
        if not isinstance(value, int) or value < 0:
            raise ScriptError(f"{field_name}.{key} must be a non-negative integer")
        if value > 0:
            out[key] = value
    return sorted_counter_dict(out)


def normalize_text_field(value: Any, field_name: str, max_len: int = 400) -> str:
    if not isinstance(value, str):
        raise ScriptError(f"{field_name} must be a string")
    text = value.strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def validate_session_meta_v1(obj: Dict[str, Any]) -> None:
    missing = SESSION_META_REQUIRED_KEYS - set(obj.keys())
    if missing:
        raise ScriptError(f"SessionMetaV1 missing keys: {sorted(missing)}")

    parse_non_negative_int(obj["duration_minutes"], "duration_minutes")
    parse_non_negative_int(obj["user_message_count"], "user_message_count")
    parse_non_negative_int(obj["assistant_message_count"], "assistant_message_count")
    parse_non_negative_int(obj["tool_errors"], "tool_errors")
    parse_non_negative_int(obj["input_tokens"], "input_tokens")
    parse_non_negative_int(obj["output_tokens"], "output_tokens")
    parse_non_negative_int(obj["cached_input_tokens"], "cached_input_tokens")
    parse_non_negative_int(obj["user_interruptions"], "user_interruptions")
    parse_non_negative_int(obj["git_commits"], "git_commits")
    parse_non_negative_int(obj["git_pushes"], "git_pushes")
    parse_non_negative_int(obj["files_modified_patch_only"], "files_modified_patch_only")
    parse_non_negative_int(obj["lines_added_patch_only"], "lines_added_patch_only")
    parse_non_negative_int(obj["lines_removed_patch_only"], "lines_removed_patch_only")

    if not isinstance(obj["tool_counts"], dict):
        raise ScriptError("tool_counts must be an object")
    for key, value in obj["tool_counts"].items():
        if not isinstance(key, str) or not key or not isinstance(value, int) or value < 0:
            raise ScriptError("tool_counts must map string keys to non-negative integers")

    normalize_small_int_dict(obj["tool_error_categories"], field_name="tool_error_categories")

    for flag in ["uses_mcp", "uses_task_agent", "uses_web_search", "uses_web_fetch"]:
        if not isinstance(obj[flag], bool):
            raise ScriptError(f"{flag} must be boolean")

    if not isinstance(obj["first_prompt_redacted"], str):
        raise ScriptError("first_prompt_redacted must be a string")
    if not isinstance(obj["message_hours"], list):
        raise ScriptError("message_hours must be a list")
    for hour in obj["message_hours"]:
        if not isinstance(hour, int) or hour < 0 or hour > 23:
            raise ScriptError("message_hours must contain integers in [0, 23]")

    if not isinstance(obj["user_response_times"], list):
        raise ScriptError("user_response_times must be a list")
    for seconds in obj["user_response_times"]:
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise ScriptError("user_response_times entries must be non-negative numbers")

    if not isinstance(obj["derivation_notes"], list) or not all(
        isinstance(x, str) for x in obj["derivation_notes"]
    ):
        raise ScriptError("derivation_notes must be a list of strings")

    # Optional model signal fields
    if "model_turn_counts" in obj:
        if not isinstance(obj["model_turn_counts"], dict):
            raise ScriptError("model_turn_counts must be an object when present")
        for key, value in obj["model_turn_counts"].items():
            if not isinstance(key, str) or not key or not isinstance(value, int) or value < 0:
                raise ScriptError("model_turn_counts must map string keys to non-negative integers")

    if "models_used" in obj:
        if not isinstance(obj["models_used"], list) or not all(
            isinstance(x, str) and x for x in obj["models_used"]
        ):
            raise ScriptError("models_used must be a list of non-empty strings when present")

    if "primary_model" in obj and obj["primary_model"] is not None and not isinstance(
        obj["primary_model"], str
    ):
        raise ScriptError("primary_model must be string or null when present")



def validate_facet_v1(obj: Dict[str, Any]) -> None:
    missing = FACET_REQUIRED_KEYS - set(obj.keys())
    if missing:
        raise ScriptError(f"FacetV1 missing keys: {sorted(missing)}")

    if obj["outcome"] not in OUTCOME_VALUES:
        raise ScriptError(f"Invalid outcome: {obj['outcome']}")
    if obj["assistant_helpfulness"] not in HELPFULNESS_VALUES:
        raise ScriptError(f"Invalid assistant_helpfulness: {obj['assistant_helpfulness']}")
    if obj["session_type"] not in SESSION_TYPE_VALUES:
        raise ScriptError(f"Invalid session_type: {obj['session_type']}")
    if obj["primary_success"] not in PRIMARY_SUCCESS_VALUES:
        raise ScriptError(f"Invalid primary_success: {obj['primary_success']}")

    normalize_text_field(obj["underlying_goal"], "underlying_goal", max_len=360)
    normalize_text_field(obj["friction_detail"], "friction_detail", max_len=360)
    normalize_text_field(obj["brief_summary"], "brief_summary", max_len=360)

    normalize_small_int_dict(
        obj["goal_categories"], allowed_keys=GOAL_CATEGORY_KEYS, field_name="goal_categories"
    )
    normalize_small_int_dict(
        obj["friction_counts"], allowed_keys=FRICTION_KEYS, field_name="friction_counts"
    )
    normalize_small_int_dict(
        obj["user_satisfaction_counts"],
        allowed_keys=SATISFACTION_KEYS,
        field_name="user_satisfaction_counts",
    )

    parse_confidence(obj["confidence"])  # validates bounds

    if not isinstance(obj["evidence_flags"], dict):
        raise ScriptError("evidence_flags must be an object")
    for key, value in obj["evidence_flags"].items():
        if not isinstance(key, str):
            raise ScriptError("evidence_flags keys must be strings")
        if not isinstance(value, bool):
            raise ScriptError("evidence_flags values must be booleans")



def validate_stats_v1(obj: Dict[str, Any]) -> None:
    for key in [
        "generated_at",
        "sessions_total",
        "sessions_faceted",
        "facet_coverage_ratio",
        "time_range",
        "totals",
        "distributions",
    ]:
        if key not in obj:
            raise ScriptError(f"StatsV1 missing key: {key}")
    parse_non_negative_int(obj["sessions_total"], "sessions_total")
    parse_non_negative_int(obj["sessions_faceted"], "sessions_faceted")
    parse_confidence(obj["facet_coverage_ratio"], "facet_coverage_ratio")
    if not isinstance(obj["time_range"], dict):
        raise ScriptError("time_range must be an object")
    if not isinstance(obj["totals"], dict):
        raise ScriptError("totals must be an object")
    if not isinstance(obj["distributions"], dict):
        raise ScriptError("distributions must be an object")



def make_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--verbose", action="store_true", help="Print additional progress logs")
    return parser
