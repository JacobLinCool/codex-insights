#!/usr/bin/env python3
"""Run full codex-insights pipeline end-to-end."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from _shared import ScriptError, ensure_dir, load_jsonl

STATE_VERSION = "StateIndexV1"


def run_step(name: str, cmd: List[str]) -> None:
    print(f"[codex-insights] {name}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        raise ScriptError(f"Step '{name}' failed (exit={proc.returncode}):\n{output.strip()}")
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip())


def fingerprint_manifest_row(row: Dict[str, Any]) -> str:
    payload = row.get("fingerprint")
    if not isinstance(payload, dict):
        thread = row.get("thread") if isinstance(row.get("thread"), dict) else {}
        payload = {
            "thread_updated_at": int(thread.get("updated_at", 0) or 0),
            "rollout_path": str(row.get("rollout_path") or ""),
        }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_state_index(state_file: Path) -> Tuple[Dict[str, Any] | None, str | None]:
    if not state_file.exists():
        return None, "state index missing"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"state index unreadable: {exc}"
    if not isinstance(state, dict):
        return None, "state index invalid structure"
    if state.get("version") != STATE_VERSION:
        return None, "state index version mismatch"
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        return None, "state index sessions missing"
    for sid, payload in sessions.items():
        if not isinstance(sid, str) or not sid:
            return None, "state index has invalid session id"
        if not isinstance(payload, dict) or not isinstance(payload.get("fingerprint"), str):
            return None, f"state index payload invalid for session {sid}"
    return state, None


def write_state_index(state_file: Path, fingerprints: Dict[str, str], manifest_path: Path) -> None:
    state = {
        "version": STATE_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifest_path": str(manifest_path),
        "sessions": {
            sid: {"fingerprint": fp}
            for sid, fp in sorted(fingerprints.items(), key=lambda kv: kv[0])
        },
    }
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def outputs_are_healthy(meta_dir: Path, facets_dir: Path, session_ids: Set[str]) -> Tuple[bool, str | None]:
    if not meta_dir.exists() or not facets_dir.exists():
        return False, "meta/facets output directory missing"
    for sid in sorted(session_ids):
        meta_path = meta_dir / f"{sid}.json"
        facet_path = facets_dir / f"{sid}.json"
        if not meta_path.exists() or not facet_path.exists():
            return False, f"missing required output for session {sid}"
        try:
            json.loads(meta_path.read_text(encoding="utf-8"))
            json.loads(facet_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return False, f"corrupt output json for session {sid}: {exc}"
    return True, None


def write_session_id_file(path: Path, session_ids: Set[str]) -> None:
    ordered = sorted(session_ids)
    path.write_text("\n".join(ordered) + ("\n" if ordered else ""), encoding="utf-8")


def remove_session_outputs(session_ids: Set[str], meta_dir: Path, evidence_dir: Path, facets_dir: Path) -> int:
    removed = 0
    for sid in session_ids:
        for directory in [meta_dir, evidence_dir, facets_dir]:
            path = directory / f"{sid}.json"
            if path.exists():
                path.unlink()
                removed += 1
    return removed


def prune_unknown_outputs(valid_session_ids: Set[str], meta_dir: Path, evidence_dir: Path, facets_dir: Path) -> int:
    removed = 0
    for directory in [meta_dir, evidence_dir, facets_dir]:
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            if path.stem not in valid_session_ids:
                path.unlink()
                removed += 1
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run codex-insights extraction + analysis pipeline")
    parser.add_argument("--db", required=True, type=Path, help="Path to state_5.sqlite")
    parser.add_argument("--sessions-root", required=True, type=Path, help="Path to sessions root")
    parser.add_argument("--archived-root", required=True, type=Path, help="Path to archived_sessions root")
    parser.add_argument("--scope", default="sqlite_threads", choices=["sqlite_threads"], help="Inclusion scope")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path.home() / ".codex" / "insights" / "latest",
        help="Output workspace directory",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="Incremental state index path (default: <work-dir>/state_index.json)",
    )
    parser.add_argument("--privacy", default="redacted", choices=["redacted", "raw", "none"], help="Privacy mode")
    parser.add_argument("--engine", default="rules_only", choices=["hybrid", "rules_only"], help="Facet engine")
    parser.add_argument(
        "--classifier-model",
        default="gpt-5.3-codex-spark",
        help="Model for classify_facets hybrid mode",
    )
    parser.add_argument(
        "--narrative-model",
        default="gpt-5.3-codex",
        help="Model for generate_narrative",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit sessions for development runs")
    parser.add_argument("--template", type=Path, help="Optional custom report template HTML")
    parser.add_argument("--skip-validate", action="store_true", help="Skip final validate_outputs step")
    parser.add_argument("--skip-narrative", action="store_true", help="Skip generate_narrative step")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    skill_dir = script_dir.parent

    work_dir = args.work_dir.resolve()
    ensure_dir(work_dir)
    state_file = args.state_file.resolve() if args.state_file else (work_dir / "state_index.json")

    manifest = work_dir / "manifest.jsonl"
    manifest_meta = work_dir / "manifest.meta.json"
    meta_dir = work_dir / "meta"
    evidence_dir = work_dir / "evidence"
    facets_dir = work_dir / "facets"
    stats_path = work_dir / "stats.json"
    narrative_path = work_dir / "narrative.json"
    report_path = work_dir / "report.html"

    template_path = args.template.resolve() if args.template else (skill_dir / "assets" / "report_template.html")
    if not template_path.exists():
        raise ScriptError(f"Report template not found: {template_path}")

    py = sys.executable

    run_step(
        "build_manifest",
        [
            py,
            str(script_dir / "build_manifest.py"),
            "--db",
            str(args.db.resolve()),
            "--sessions-root",
            str(args.sessions_root.resolve()),
            "--archived-root",
            str(args.archived_root.resolve()),
            "--scope",
            args.scope,
            "--out",
            str(manifest),
            "--meta-out",
            str(manifest_meta),
        ],
    )

    manifest_rows = load_jsonl(manifest)
    limited_mode = args.limit > 0
    if args.limit > 0:
        manifest_rows = manifest_rows[: args.limit]

    manifest_ids: Set[str] = set()
    manifest_fingerprints: Dict[str, str] = {}
    for row in manifest_rows:
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid:
            raise ScriptError(f"manifest row has invalid session_id: {row}")
        if sid in manifest_ids:
            raise ScriptError(f"manifest has duplicate session_id: {sid}")
        manifest_ids.add(sid)
        manifest_fingerprints[sid] = fingerprint_manifest_row(row)

    state, state_error = load_state_index(state_file)
    full_rebuild_reason = state_error
    if full_rebuild_reason is None and state is not None:
        healthy, unhealthy_reason = outputs_are_healthy(meta_dir, facets_dir, set(state.get("sessions", {}).keys()))
        if not healthy:
            full_rebuild_reason = unhealthy_reason

    old_fingerprints: Dict[str, str] = {}
    if state and isinstance(state.get("sessions"), dict):
        for sid, payload in state["sessions"].items():
            if isinstance(payload, dict) and isinstance(payload.get("fingerprint"), str):
                old_fingerprints[sid] = payload["fingerprint"]

    added_ids: Set[str] = set()
    changed_ids: Set[str] = set()
    removed_ids: Set[str] = set()
    unchanged_ids: Set[str] = set()

    if full_rebuild_reason:
        added_ids = set(manifest_ids)
        changed_ids = set()
        removed_ids = set() if limited_mode else (set(old_fingerprints.keys()) - manifest_ids)
        print(f"[codex-insights] full rebuild triggered: {full_rebuild_reason}")
    else:
        old_ids = set(old_fingerprints.keys())
        added_ids = manifest_ids - old_ids
        removed_ids = set() if limited_mode else (old_ids - manifest_ids)
        for sid in sorted(manifest_ids & old_ids):
            if old_fingerprints.get(sid) != manifest_fingerprints.get(sid):
                changed_ids.add(sid)
            else:
                unchanged_ids.add(sid)

    dirty_ids = set(added_ids | changed_ids)
    session_ids_file = work_dir / "dirty_session_ids.txt"
    write_session_id_file(session_ids_file, dirty_ids)

    print(
        "[codex-insights] incremental summary:",
        f"total={len(manifest_ids)}",
        f"dirty={len(dirty_ids)}",
        f"added={len(added_ids)}",
        f"changed={len(changed_ids)}",
        f"removed={len(removed_ids)}",
        f"unchanged={len(unchanged_ids)}",
    )

    if not limited_mode:
        removed_output_count = remove_session_outputs(removed_ids, meta_dir, evidence_dir, facets_dir)
        pruned_output_count = prune_unknown_outputs(manifest_ids, meta_dir, evidence_dir, facets_dir)
        if removed_output_count or pruned_output_count:
            print(
                "[codex-insights] cleaned outputs:",
                f"removed={removed_output_count}",
                f"pruned={pruned_output_count}",
            )
    else:
        print("[codex-insights] limit mode: skipped output pruning and state mutation for safety")

    if dirty_ids:
        extract_cmd = [
            py,
            str(script_dir / "extract_meta.py"),
            "--manifest",
            str(manifest),
            "--out-dir",
            str(meta_dir),
            "--session-ids-file",
            str(session_ids_file),
        ]
        if args.limit > 0:
            extract_cmd.extend(["--limit", str(args.limit)])
        run_step("extract_meta", extract_cmd)
    else:
        print("[codex-insights] extract_meta: skipped (no dirty sessions)")

    if dirty_ids:
        evidence_cmd = [
            py,
            str(script_dir / "prepare_evidence.py"),
            "--manifest",
            str(manifest),
            "--meta-dir",
            str(meta_dir),
            "--privacy",
            args.privacy,
            "--out-dir",
            str(evidence_dir),
            "--session-ids-file",
            str(session_ids_file),
        ]
        if args.limit > 0:
            evidence_cmd.extend(["--limit", str(args.limit)])
        run_step("prepare_evidence", evidence_cmd)
    else:
        print("[codex-insights] prepare_evidence: skipped (no dirty sessions)")

    if dirty_ids:
        facets_cmd = [
            py,
            str(script_dir / "classify_facets.py"),
            "--evidence-dir",
            str(evidence_dir),
            "--meta-dir",
            str(meta_dir),
            "--engine",
            args.engine,
            "--model",
            args.classifier_model,
            "--out-dir",
            str(facets_dir),
            "--session-ids-file",
            str(session_ids_file),
            "--overwrite",
        ]
        if args.limit > 0:
            facets_cmd.extend(["--limit", str(args.limit)])
        run_step("classify_facets", facets_cmd)
    else:
        print("[codex-insights] classify_facets: skipped (no dirty sessions)")

    run_step(
        "aggregate_stats",
        [
            py,
            str(script_dir / "aggregate_stats.py"),
            "--meta-dir",
            str(meta_dir),
            "--facets-dir",
            str(facets_dir),
            "--manifest",
            str(manifest),
            "--out",
            str(stats_path),
        ],
    )

    if not args.skip_narrative:
        run_step(
            "generate_narrative",
            [
                py,
                str(script_dir / "generate_narrative.py"),
                "--stats",
                str(stats_path),
                "--facets-dir",
                str(facets_dir),
                "--evidence-dir",
                str(evidence_dir),
                "--privacy",
                args.privacy,
                "--model",
                args.narrative_model,
                "--out",
                str(narrative_path),
            ],
        )
    elif not narrative_path.exists():
        raise ScriptError("--skip-narrative was set but narrative.json does not exist")

    run_step(
        "render_report",
        [
            py,
            str(script_dir / "render_report.py"),
            "--stats",
            str(stats_path),
            "--narrative",
            str(narrative_path),
            "--template",
            str(template_path),
            "--out",
            str(report_path),
        ],
    )

    if not args.skip_validate:
        run_step(
            "validate_outputs",
            [
                py,
                str(script_dir / "validate_outputs.py"),
                "--meta-dir",
                str(meta_dir),
                "--facets-dir",
                str(facets_dir),
                "--stats",
                str(stats_path),
                "--report",
                str(report_path),
                "--manifest",
                str(manifest),
                "--manifest-meta",
                str(manifest_meta),
                "--evidence-dir",
                str(evidence_dir),
                "--privacy",
                args.privacy,
            ],
        )

    if not limited_mode:
        write_state_index(state_file=state_file, fingerprints=manifest_fingerprints, manifest_path=manifest)
    else:
        print("[codex-insights] limit mode: skipped state index write")

    print("[codex-insights] pipeline complete")
    print(f"  manifest:  {manifest}")
    print(f"  manifest_meta: {manifest_meta}")
    print(f"  meta:      {meta_dir}")
    print(f"  evidence:  {evidence_dir}")
    print(f"  facets:    {facets_dir}")
    print(f"  stats:     {stats_path}")
    print(f"  narrative: {narrative_path}")
    print(f"  report:    {report_path}")
    print(f"  state:     {state_file}")


if __name__ == "__main__":
    main()
