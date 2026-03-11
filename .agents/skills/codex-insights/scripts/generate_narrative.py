#!/usr/bin/env python3
"""Generate narrative.json from aggregated stats + validated facets."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from _shared import ScriptError, dump_json, format_iso8601_z, load_json, run_codex_json, validate_facet_v1

SECTION_KEYS = [
    "at_a_glance",
    "what_you_work_on",
    "how_you_use_codex",
    "wins",
    "friction",
    "feature_suggestions",
    "patterns",
    "horizon",
]


def clip(text: str, max_len: int) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1] + "…"


def load_facets(directory: Path) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        data = load_json(path)
        validate_facet_v1(data)
        rows[data["session_id"]] = data
    if not rows:
        raise ScriptError(f"No facet files found in {directory}")
    return rows


def load_evidence(directory: Path) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        data = load_json(path)
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            rows[session_id] = data
    return rows


def build_examples(
    facets: Dict[str, Dict[str, Any]],
    evidence: Dict[str, Dict[str, Any]],
    *,
    max_items: int,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    ordered = sorted(
        facets.values(),
        key=lambda row: (
            -float(row.get("confidence", 0.0)),
            row.get("session_id", ""),
        ),
    )

    for facet in ordered[:max_items]:
        sid = facet["session_id"]
        ev = evidence.get(sid, {})
        text_signals = ev.get("text_signals") if isinstance(ev.get("text_signals"), dict) else {}
        tool_signals = ev.get("tool_signals") if isinstance(ev.get("tool_signals"), dict) else {}

        top_tools = []
        if isinstance(tool_signals.get("top_tools"), list):
            for tool in tool_signals["top_tools"][:4]:
                if isinstance(tool, dict):
                    name = str(tool.get("name") or "")
                    count = int(tool.get("count") or 0)
                    if name:
                        top_tools.append({"name": name, "count": count})

        item = {
            "session_id": sid,
            "underlying_goal": clip(str(facet.get("underlying_goal") or ""), 180),
            "outcome": facet.get("outcome"),
            "assistant_helpfulness": facet.get("assistant_helpfulness"),
            "session_type": facet.get("session_type"),
            "primary_success": facet.get("primary_success"),
            "friction_counts": facet.get("friction_counts", {}),
            "user_satisfaction_counts": facet.get("user_satisfaction_counts", {}),
            "brief_summary": clip(str(facet.get("brief_summary") or ""), 200),
            "first_prompt": clip(str(text_signals.get("first_prompt") or ""), 200),
            "task_complete_message": clip(str(text_signals.get("task_complete_message") or ""), 180),
            "top_tools": top_tools,
        }
        items.append(item)

    return items


def validate_section(section_key: str, section_obj: Any) -> Dict[str, Any]:
    if not isinstance(section_obj, dict):
        raise ScriptError(f"narrative.sections.{section_key} must be an object")

    out: Dict[str, Any] = {}

    title = section_obj.get("title")
    if title is None:
        title = section_key.replace("_", " ").title()
    if not isinstance(title, str) or not title.strip():
        raise ScriptError(f"narrative.sections.{section_key}.title must be a non-empty string")
    out["title"] = clip(title, 80)

    summary = section_obj.get("summary")
    if summary is None and section_key == "at_a_glance":
        summary = section_obj.get("headline")
    if not isinstance(summary, str) or not summary.strip():
        raise ScriptError(f"narrative.sections.{section_key}.summary must be a non-empty string")
    out["summary"] = clip(summary, 700)

    bullets = section_obj.get("bullets")
    if not isinstance(bullets, list) or len(bullets) < 2:
        raise ScriptError(f"narrative.sections.{section_key}.bullets must have at least 2 items")

    normalized_bullets: List[str] = []
    for bullet in bullets[:8]:
        if not isinstance(bullet, str) or not bullet.strip():
            raise ScriptError(f"narrative.sections.{section_key}.bullets entries must be strings")
        normalized_bullets.append(clip(bullet, 280))
    out["bullets"] = normalized_bullets

    return out


def normalize_narrative(candidate: Dict[str, Any], *, privacy: str) -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ScriptError("Narrative output must be a JSON object")

    sections = candidate.get("sections")
    if not isinstance(sections, dict):
        raise ScriptError("narrative.sections must be an object")

    normalized_sections: Dict[str, Dict[str, Any]] = {}
    for key in SECTION_KEYS:
        if key not in sections:
            raise ScriptError(f"Missing narrative section: {key}")
        normalized_sections[key] = validate_section(key, sections[key])

    language = candidate.get("language", "English")
    if not isinstance(language, str):
        raise ScriptError("narrative.language must be a string")
    language = clip(language, 32)

    generated_at = candidate.get("generated_at")
    if isinstance(generated_at, str) and generated_at.strip():
        generated = clip(generated_at, 64)
    else:
        generated = format_iso8601_z(datetime.now(timezone.utc))

    out = {
        "version": "NarrativeV1",
        "language": language,
        "privacy_mode": privacy,
        "generated_at": generated,
        "sections": normalized_sections,
    }

    if privacy == "redacted":
        path_match = re.search(r"/(Users|home|mnt|var)/[^\s<>{}\"]+", json.dumps(out, ensure_ascii=False))
        if path_match:
            raise ScriptError("Narrative output appears to include an absolute path under redacted mode")

    return out


def build_prompt(
    *,
    stats: Dict[str, Any],
    examples: List[Dict[str, Any]],
    privacy: str,
) -> str:
    schema = {
        "version": "NarrativeV1",
        "language": "English",
        "privacy_mode": privacy,
        "generated_at": "ISO8601 UTC",
        "sections": {
            key: {
                "title": key.replace("_", " ").title(),
                "summary": "1 paragraph grounded in stats and facets",
                "bullets": ["2-6 concise bullet points"],
            }
            for key in SECTION_KEYS
        },
    }

    return (
        "You are producing an English Codex usage insights narrative. "
        "Use only provided stats and examples. Do not invent numbers. "
        "Do not quote raw transcript text. Paraphrase and keep privacy constraints. "
        "Return STRICT JSON only, no markdown.\n\n"
        f"Required JSON schema shape:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"StatsV1:\n{json.dumps(stats, ensure_ascii=False, indent=2)}\n\n"
        f"Facet/Evidence examples (already privacy-filtered):\n{json.dumps(examples, ensure_ascii=False, indent=2)}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate narrative.json from stats + facets + evidence")
    parser.add_argument("--stats", required=True, type=Path, help="Path to stats.json")
    parser.add_argument("--facets-dir", required=True, type=Path, help="Directory containing facets/*.json")
    parser.add_argument("--evidence-dir", required=True, type=Path, help="Directory containing evidence/*.json")
    parser.add_argument("--privacy", default="redacted", choices=["redacted", "raw", "none"], help="Privacy mode")
    parser.add_argument("--model", default="gpt-5.3-codex", help="Model for narrative synthesis")
    parser.add_argument("--timeout-sec", type=int, default=240, help="Timeout per model call")
    parser.add_argument("--max-examples", type=int, default=120, help="Max facet/evidence examples passed to model")
    parser.add_argument("--out", required=True, type=Path, help="Output narrative.json path")
    args = parser.parse_args()

    stats = load_json(args.stats.resolve())
    facets = load_facets(args.facets_dir.resolve())
    evidence = load_evidence(args.evidence_dir.resolve())

    examples = build_examples(facets, evidence, max_items=max(10, int(args.max_examples)))
    prompt = build_prompt(stats=stats, examples=examples, privacy=args.privacy)

    last_error = ""
    narrative: Dict[str, Any] | None = None
    for attempt in range(2):
        if attempt > 0:
            retry_prompt = (
                prompt
                + "\nPrevious output failed validation with error:\n"
                + last_error
                + "\nReturn corrected strict JSON only."
            )
        else:
            retry_prompt = prompt

        candidate = run_codex_json(model=args.model, prompt=retry_prompt, timeout_sec=args.timeout_sec)
        try:
            narrative = normalize_narrative(candidate, privacy=args.privacy)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

    if narrative is None:
        raise ScriptError(f"Narrative generation failed validation after retries: {last_error}")

    dump_json(args.out.resolve(), narrative)
    print(f"Wrote narrative: {args.out.resolve()}")


if __name__ == "__main__":
    main()
