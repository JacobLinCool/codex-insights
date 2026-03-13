#!/usr/bin/env python3
"""Classify FacetV1 records from evidence + meta."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from _shared import (
    FRICTION_KEYS,
    GOAL_CATEGORY_KEYS,
    HELPFULNESS_VALUES,
    OUTCOME_VALUES,
    PRIMARY_SUCCESS_VALUES,
    SATISFACTION_KEYS,
    SESSION_TYPE_VALUES,
    ScriptError,
    dump_json,
    load_json,
    normalize_small_int_dict,
    normalize_text_field,
    parse_confidence,
    run_codex_json,
    validate_facet_v1,
)


def keyword_goal_categories(text: str) -> Dict[str, int]:
    lowered = text.lower()
    rules = {
        "bug_fix": ["bug", "fix", "error", "deadlock", "crash"],
        "feature_implementation": ["implement", "feature", "build", "add", "create"],
        "code_review": ["review", "audit", "feedback", "pr"],
        "code_analysis": ["analyze", "analysis", "understand", "inspect", "investigate"],
        "refactor": ["refactor", "cleanup", "restructure", "reorganize"],
        "documentation": ["document", "readme", "spec", "guide", "agents"],
        "planning": ["plan", "roadmap", "strategy", "design"],
        "testing": ["test", "coverage", "benchmark", "validate"],
        "performance_optimization": ["optimiz", "performance", "latency", "throughput"],
        "tooling": ["script", "cli", "tool", "automation"],
        "ui_ux": ["ui", "ux", "layout", "design", "frontend"],
        "research": ["research", "compare", "insight", "market"],
        "devops": ["ci", "pipeline", "deploy", "release"],
        "workflow_automation": ["workflow", "agent", "orchestr", "taskboard", "loop"],
    }

    out: Dict[str, int] = {}
    for category, tokens in rules.items():
        if any(token in lowered for token in tokens):
            out[category] = 1

    if not out:
        out["code_analysis"] = 1
    return out



def infer_session_type(meta: Dict[str, Any], completion_flags: Dict[str, bool]) -> str:
    duration = int(meta.get("duration_minutes", 0))
    user_messages = int(meta.get("user_message_count", 0))
    files_changed = int(meta.get("files_modified_patch_only", 0))

    if files_changed == 0 and duration >= 15 and not completion_flags.get("has_task_complete", False):
        return "exploration"
    if user_messages <= 2:
        return "single_task"
    if user_messages >= 4 and files_changed > 0:
        return "iterative_refinement"
    if user_messages >= 3:
        return "multi_task"
    return "single_task"



def infer_outcome(meta: Dict[str, Any], completion_flags: Dict[str, bool]) -> str:
    files_changed = int(meta.get("files_modified_patch_only", 0))
    tool_errors = int(meta.get("tool_errors", 0))
    interruptions = int(meta.get("user_interruptions", 0))

    if completion_flags.get("has_task_complete", False):
        if tool_errors == 0 and interruptions == 0:
            return "fully_achieved"
        return "mostly_achieved"

    if interruptions > 0 and files_changed == 0:
        return "not_achieved"
    if files_changed > 0:
        return "partially_achieved"
    return "unclear_from_transcript"



def infer_primary_success(meta: Dict[str, Any], session_type: str, outcome: str) -> str:
    files_changed = int(meta.get("files_modified_patch_only", 0))
    tool_errors = int(meta.get("tool_errors", 0))

    if files_changed >= 3:
        return "multi_file_changes"
    if files_changed > 0 and tool_errors == 0:
        return "correct_code_edits"
    if session_type == "exploration":
        return "good_explanations"
    if outcome in {"mostly_achieved", "fully_achieved"}:
        return "proactive_help"
    return "none"



def infer_helpfulness(outcome: str, tool_errors: int) -> str:
    if outcome == "fully_achieved" and tool_errors == 0:
        return "essential"
    if outcome in {"fully_achieved", "mostly_achieved"}:
        return "very_helpful"
    if outcome == "partially_achieved":
        return "moderately_helpful"
    if outcome == "not_achieved":
        return "unhelpful"
    return "slightly_helpful"



def infer_friction(meta: Dict[str, Any], completion_flags: Dict[str, bool]) -> Tuple[Dict[str, int], str]:
    errors = meta.get("tool_error_categories", {}) if isinstance(meta.get("tool_error_categories"), dict) else {}
    tool_errors = int(meta.get("tool_errors", 0))
    interruptions = int(meta.get("user_interruptions", 0))
    files_changed = int(meta.get("files_modified_patch_only", 0))
    user_messages = int(meta.get("user_message_count", 0))

    friction: Dict[str, int] = {}

    user_rejected = int(errors.get("User Rejected", 0))
    command_failed = int(errors.get("Command Failed", 0))
    other_errors = int(errors.get("Other", 0))

    if command_failed > 0:
        friction["wrong_approach"] = min(3, command_failed)
    if tool_errors >= 3 and files_changed > 0:
        friction["buggy_code"] = min(3, tool_errors // 2)
    if user_rejected > 0:
        friction["user_rejected_action"] = min(3, user_rejected)
    if interruptions > 0 and files_changed == 0:
        friction["excessive_planning"] = 1
    if files_changed >= 10 and interruptions > 0:
        friction["excessive_changes"] = 1
    if interruptions > 0 and user_messages >= 4:
        friction["misunderstood_request"] = 1
    if other_errors > 2 and not completion_flags.get("has_task_complete", False):
        friction["external_limit"] = 1

    if not friction:
        return {}, "No major friction signals detected from deterministic evidence."

    lead = sorted(friction.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    detail = {
        "wrong_approach": "Command failures indicate at least one unproductive initial approach.",
        "buggy_code": "Error and retry signals suggest instability after code edits.",
        "misunderstood_request": "Conversation pattern suggests clarification loops before convergence.",
        "user_rejected_action": "At least one tool action was explicitly rejected by policy/user constraints.",
        "excessive_planning": "Session ended with limited implementation and interruption signals.",
        "excessive_changes": "Large patch-only change volume correlated with interruption/rework signals.",
        "external_limit": "External/system constraints likely limited session completion.",
    }[lead]

    return friction, detail



def infer_satisfaction(outcome: str, friction: Dict[str, int]) -> Dict[str, int]:
    if outcome == "fully_achieved" and not friction:
        return {"satisfied": 1}
    if outcome in {"fully_achieved", "mostly_achieved"}:
        return {"likely_satisfied": 1}
    if outcome == "partially_achieved":
        return {"neutral": 1}
    if outcome == "not_achieved":
        return {"dissatisfied": 1}
    return {"neutral": 1}



def build_rule_seed(meta: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    completion_flags = (
        evidence.get("event_signals", {}).get("completion_flags")
        if isinstance(evidence.get("event_signals"), dict)
        else {}
    )
    if not isinstance(completion_flags, dict):
        completion_flags = {}

    first_prompt = ""
    text_signals = evidence.get("text_signals") if isinstance(evidence.get("text_signals"), dict) else {}
    if isinstance(text_signals, dict):
        first_prompt = str(text_signals.get("first_prompt") or "")

    underlying_goal = first_prompt or "Investigate and complete the requested coding task."
    goal_categories = keyword_goal_categories(underlying_goal)
    session_type = infer_session_type(meta, completion_flags)
    outcome = infer_outcome(meta, completion_flags)
    primary_success = infer_primary_success(meta, session_type, outcome)
    friction_counts, friction_detail = infer_friction(meta, completion_flags)
    user_satisfaction_counts = infer_satisfaction(outcome, friction_counts)
    helpfulness = infer_helpfulness(outcome, int(meta.get("tool_errors", 0)))

    summary = (
        f"Session focused on {underlying_goal.lower()[:120]}"
        f"; outcome inferred as {outcome.replace('_', ' ')}"
        f" with {int(meta.get('files_modified_patch_only', 0))} patch-only files changed."
    )

    confidence = 0.55
    if completion_flags.get("has_task_complete", False):
        confidence += 0.15
    if int(meta.get("tool_errors", 0)) == 0:
        confidence += 0.10
    if int(meta.get("user_message_count", 0)) >= 3:
        confidence += 0.05
    confidence = max(0.05, min(0.95, round(confidence, 3)))

    return {
        "session_id": meta["session_id"],
        "underlying_goal": normalize_text_field(underlying_goal, "underlying_goal", max_len=360),
        "goal_categories": normalize_small_int_dict(
            goal_categories, allowed_keys=GOAL_CATEGORY_KEYS, field_name="goal_categories"
        ),
        "outcome": outcome,
        "assistant_helpfulness": helpfulness,
        "session_type": session_type,
        "primary_success": primary_success,
        "friction_counts": normalize_small_int_dict(
            friction_counts, allowed_keys=FRICTION_KEYS, field_name="friction_counts"
        ),
        "friction_detail": normalize_text_field(friction_detail, "friction_detail", max_len=360),
        "user_satisfaction_counts": normalize_small_int_dict(
            user_satisfaction_counts,
            allowed_keys=SATISFACTION_KEYS,
            field_name="user_satisfaction_counts",
        ),
        "brief_summary": normalize_text_field(summary, "brief_summary", max_len=360),
        "confidence": confidence,
        "evidence_flags": {
            "has_task_complete": bool(completion_flags.get("has_task_complete", False)),
            "has_item_completed": bool(completion_flags.get("has_item_completed", False)),
            "has_turn_aborted": bool(completion_flags.get("has_turn_aborted", False)),
            "tool_errors_present": int(meta.get("tool_errors", 0)) > 0,
            "patch_changes_present": int(meta.get("files_modified_patch_only", 0)) > 0,
            "llm_enriched": False,
            "llm_validation_failed": False,
        },
    }



def normalize_facet(candidate: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    obj = dict(candidate)
    obj["session_id"] = session_id

    obj["underlying_goal"] = normalize_text_field(
        obj.get("underlying_goal", "Investigate and complete the requested coding task."),
        "underlying_goal",
        max_len=360,
    )
    obj["outcome"] = str(obj.get("outcome", "unclear_from_transcript"))
    obj["assistant_helpfulness"] = str(obj.get("assistant_helpfulness", "moderately_helpful"))
    obj["session_type"] = str(obj.get("session_type", "single_task"))
    obj["primary_success"] = str(obj.get("primary_success", "none"))

    if obj["outcome"] not in OUTCOME_VALUES:
        obj["outcome"] = "unclear_from_transcript"
    if obj["assistant_helpfulness"] not in HELPFULNESS_VALUES:
        obj["assistant_helpfulness"] = "moderately_helpful"
    if obj["session_type"] not in SESSION_TYPE_VALUES:
        obj["session_type"] = "single_task"
    if obj["primary_success"] not in PRIMARY_SUCCESS_VALUES:
        obj["primary_success"] = "none"

    obj["goal_categories"] = normalize_small_int_dict(
        obj.get("goal_categories", {}),
        allowed_keys=GOAL_CATEGORY_KEYS,
        field_name="goal_categories",
    )
    obj["friction_counts"] = normalize_small_int_dict(
        obj.get("friction_counts", {}),
        allowed_keys=FRICTION_KEYS,
        field_name="friction_counts",
    )
    obj["user_satisfaction_counts"] = normalize_small_int_dict(
        obj.get("user_satisfaction_counts", {}),
        allowed_keys=SATISFACTION_KEYS,
        field_name="user_satisfaction_counts",
    )

    obj["friction_detail"] = normalize_text_field(
        obj.get("friction_detail", ""), "friction_detail", max_len=360
    )
    obj["brief_summary"] = normalize_text_field(
        obj.get("brief_summary", ""), "brief_summary", max_len=360
    )
    obj["confidence"] = parse_confidence(obj.get("confidence", 0.5))

    evidence_flags = obj.get("evidence_flags", {})
    if not isinstance(evidence_flags, dict):
        evidence_flags = {}
    normalized_flags = {}
    for key, value in evidence_flags.items():
        if isinstance(key, str):
            normalized_flags[key] = bool(value)
    obj["evidence_flags"] = normalized_flags

    return obj


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



def build_hybrid_prompt(
    evidence: Dict[str, Any],
    meta: Dict[str, Any],
    rule_seed: Dict[str, Any],
) -> str:
    schema_hint = {
        "required_fields": [
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
        ],
        "outcome_values": sorted(OUTCOME_VALUES),
        "assistant_helpfulness_values": sorted(HELPFULNESS_VALUES),
        "session_type_values": sorted(SESSION_TYPE_VALUES),
        "primary_success_values": sorted(PRIMARY_SUCCESS_VALUES),
        "goal_category_keys": sorted(GOAL_CATEGORY_KEYS),
        "friction_keys": sorted(FRICTION_KEYS),
        "satisfaction_keys": sorted(SATISFACTION_KEYS),
    }

    return (
        "You are classifying a coding session into FacetV1. "
        "Return STRICT JSON only, with exactly one object. "
        "Use only allowed enum values and keys; keep text concise and evidence-grounded. "
        "Do not add markdown.\n\n"
        f"Schema constraints:\n{json.dumps(schema_hint, ensure_ascii=False, indent=2)}\n\n"
        f"Deterministic rule seed:\n{json.dumps(rule_seed, ensure_ascii=False, indent=2)}\n\n"
        f"Session meta:\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n\n"
        f"Session evidence:\n{json.dumps(evidence, ensure_ascii=False, indent=2)}\n"
    )



def classify_hybrid(
    *,
    model: str,
    evidence: Dict[str, Any],
    meta: Dict[str, Any],
    rule_seed: Dict[str, Any],
    timeout_sec: int,
) -> Dict[str, Any]:
    prompt = build_hybrid_prompt(evidence=evidence, meta=meta, rule_seed=rule_seed)
    last_error = ""

    for attempt in range(2):
        candidate = run_codex_json(model=model, prompt=prompt, timeout_sec=timeout_sec)
        merged = dict(rule_seed)
        merged.update(candidate)
        merged["session_id"] = meta["session_id"]

        try:
            normalized = normalize_facet(merged, meta["session_id"])
            normalized.setdefault("evidence_flags", {})["llm_enriched"] = True
            normalized["evidence_flags"].setdefault("llm_validation_failed", False)
            validate_facet_v1(normalized)
            return normalized
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            prompt = (
                build_hybrid_prompt(evidence=evidence, meta=meta, rule_seed=rule_seed)
                + "\nPrevious output failed validation with this error:\n"
                + last_error
                + "\nReturn corrected JSON only."
            )

    fallback = dict(rule_seed)
    fallback["outcome"] = "unclear_from_transcript"
    fallback["assistant_helpfulness"] = "slightly_helpful"
    fallback["primary_success"] = "none"
    fallback["friction_counts"] = dict(fallback.get("friction_counts", {}))
    fallback["friction_detail"] = normalize_text_field(
        "Facet classification fell back after LLM validation failed: " + last_error,
        "friction_detail",
        max_len=360,
    )
    fallback["user_satisfaction_counts"] = {"neutral": 1}
    fallback["brief_summary"] = normalize_text_field(
        "Facet classification unavailable from model output; marked as unclear.",
        "brief_summary",
        max_len=360,
    )
    fallback["confidence"] = 0.2
    flags = dict(fallback.get("evidence_flags", {}))
    flags["llm_enriched"] = False
    flags["llm_validation_failed"] = True
    fallback["evidence_flags"] = flags

    normalized = normalize_facet(fallback, meta["session_id"])
    validate_facet_v1(normalized)
    return normalized



def main() -> None:
    parser = argparse.ArgumentParser(description="Classify FacetV1 records")
    parser.add_argument("--evidence-dir", required=True, type=Path, help="evidence/*.json directory")
    parser.add_argument("--meta-dir", required=True, type=Path, help="meta/*.json directory")
    parser.add_argument(
        "--engine",
        default="rules_only",
        choices=["hybrid", "rules_only"],
        help="Facet classification engine",
    )
    parser.add_argument("--model", default="gpt-5.3-codex-spark", help="Model for hybrid classification")
    parser.add_argument("--timeout-sec", type=int, default=180, help="Timeout for each model call")
    parser.add_argument("--out-dir", required=True, type=Path, help="Output directory for facets/*.json")
    parser.add_argument("--session-ids", default="", help="Comma-separated session IDs to process")
    parser.add_argument("--session-ids-file", type=Path, help="Optional newline-delimited session ID file")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N sessions")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing facet outputs")
    args = parser.parse_args()

    evidence_files = sorted(args.evidence_dir.resolve().glob("*.json"))
    session_filter = load_session_id_filter(
        args.session_ids,
        args.session_ids_file.resolve() if args.session_ids_file else None,
    )
    if session_filter is not None:
        evidence_files = [path for path in evidence_files if path.stem in session_filter]
    if args.limit > 0:
        evidence_files = evidence_files[: args.limit]

    args.out_dir.resolve().mkdir(parents=True, exist_ok=True)

    processed = 0
    for evidence_path in evidence_files:
        session_id = evidence_path.stem
        out_path = args.out_dir.resolve() / f"{session_id}.json"
        if out_path.exists() and not args.overwrite:
            continue

        meta_path = args.meta_dir.resolve() / f"{session_id}.json"
        if not meta_path.exists():
            raise ScriptError(f"Missing meta file for session {session_id}: {meta_path}")

        evidence = load_json(evidence_path)
        meta = load_json(meta_path)

        rule_seed = build_rule_seed(meta, evidence)
        validate_facet_v1(rule_seed)

        if args.engine == "rules_only":
            facet = rule_seed
        else:
            facet = classify_hybrid(
                model=args.model,
                evidence=evidence,
                meta=meta,
                rule_seed=rule_seed,
                timeout_sec=args.timeout_sec,
            )

        validate_facet_v1(facet)
        dump_json(out_path, facet)
        processed += 1

    print(f"Wrote facets: {processed} -> {args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
