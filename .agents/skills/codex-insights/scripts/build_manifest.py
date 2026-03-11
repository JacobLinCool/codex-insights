#!/usr/bin/env python3
"""Build a rollout manifest for codex-insights."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from _shared import (
    ScriptError,
    extract_session_meta_from_rollout,
    find_rollout_files,
    format_iso8601_z,
    parse_iso8601_utc,
    write_jsonl,
)

VALID_SCOPES = {"sqlite_threads"}


def load_thread_rows(db_path: Path) -> Dict[str, Dict[str, Any]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT
              id,
              created_at,
              updated_at,
              archived_at,
              cwd,
              source,
              model_provider,
              title,
              archived,
              tokens_used,
              git_sha,
              git_branch,
              git_origin_url,
              rollout_path,
              cli_version,
              has_user_event,
              memory_mode,
              approval_mode,
              sandbox_policy
            FROM threads
            """
        ).fetchall()
    finally:
        con.close()

    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        out[str(row["id"])] = dict(row)
    return out


def select_rollout_for_session(existing: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not existing:
        return candidate
    # Prefer the latest session_meta timestamp when duplicates exist.
    old_ts = parse_iso8601_utc(existing["session_meta_timestamp"]) if existing.get("session_meta_timestamp") else None
    new_ts = parse_iso8601_utc(candidate["session_meta_timestamp"]) if candidate.get("session_meta_timestamp") else None
    if old_ts is None:
        return candidate
    if new_ts is None:
        return existing
    if new_ts >= old_ts:
        return candidate
    return existing


def build_manifest_rows(
    db_path: Path,
    sessions_root: Path,
    archived_root: Path,
    scope: str,
) -> List[Dict[str, Any]]:
    if scope not in VALID_SCOPES:
        raise ScriptError(f"Unsupported --scope '{scope}'. Allowed: {sorted(VALID_SCOPES)}")

    thread_rows = load_thread_rows(db_path)
    rollout_files = find_rollout_files(sessions_root, archived_root)

    selected: Dict[str, Dict[str, Any]] = {}
    missing_meta = 0
    for rollout_path in rollout_files:
        session_meta = extract_session_meta_from_rollout(rollout_path)
        if not session_meta:
            missing_meta += 1
            continue

        session_id = session_meta.get("id")
        if not isinstance(session_id, str) or not session_id:
            missing_meta += 1
            continue

        if session_id not in thread_rows:
            continue

        candidate = {
            "session_id": session_id,
            "rollout_path": str(rollout_path),
            "session_meta_timestamp": session_meta.get("timestamp"),
            "session_meta_source": session_meta.get("source"),
            "session_meta_cwd": session_meta.get("cwd"),
            "cli_version": session_meta.get("cli_version"),
            "model_provider": session_meta.get("model_provider"),
            "rollout_mtime_ns": rollout_path.stat().st_mtime_ns,
            "rollout_size": rollout_path.stat().st_size,
        }
        selected[session_id] = select_rollout_for_session(selected.get(session_id, {}), candidate)

    rows: List[Dict[str, Any]] = []
    for session_id, thread in sorted(thread_rows.items(), key=lambda kv: kv[0]):
        rollout = selected.get(session_id)
        if not rollout:
            raise ScriptError(
                "Manifest build failed: missing rollout file for sqlite thread "
                f"{session_id}. Scope sqlite_threads requires 1:1 mapping."
            )

        rows.append(
            {
                "session_id": session_id,
                "rollout_path": rollout["rollout_path"],
                "scope": scope,
                "thread": {
                    "created_at": int(thread["created_at"]),
                    "updated_at": int(thread["updated_at"]),
                    "archived_at": int(thread["archived_at"]) if thread["archived_at"] is not None else None,
                    "cwd": thread["cwd"],
                    "source": thread["source"],
                    "model_provider": thread["model_provider"],
                    "title": thread["title"],
                    "archived": int(thread["archived"]),
                    "tokens_used": int(thread["tokens_used"]),
                    "git_sha": thread["git_sha"],
                    "git_branch": thread["git_branch"],
                    "git_origin_url": thread["git_origin_url"],
                    "rollout_path_from_thread": thread["rollout_path"],
                    "cli_version": thread["cli_version"],
                    "has_user_event": int(thread["has_user_event"]),
                    "memory_mode": thread["memory_mode"],
                    "approval_mode": thread["approval_mode"],
                    "sandbox_policy": thread["sandbox_policy"],
                },
                "rollout_summary": {
                    "session_meta_timestamp": rollout.get("session_meta_timestamp"),
                    "session_meta_source": rollout.get("session_meta_source"),
                    "session_meta_cwd": rollout.get("session_meta_cwd"),
                    "cli_version": rollout.get("cli_version"),
                    "model_provider": rollout.get("model_provider"),
                    "is_archived_rollout": "/archived_sessions/" in rollout["rollout_path"],
                },
                "fingerprint": {
                    "thread_updated_at": int(thread["updated_at"]),
                    "rollout_path": rollout["rollout_path"],
                    "rollout_mtime_ns": int(rollout["rollout_mtime_ns"]),
                    "rollout_size": int(rollout["rollout_size"]),
                },
                "build_notes": {
                    "db_path": str(db_path),
                    "sessions_root": str(sessions_root),
                    "archived_root": str(archived_root),
                    "ignored_rollouts_without_session_meta": missing_meta,
                },
            }
        )

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Build codex-insights manifest.jsonl")
    parser.add_argument("--db", required=True, type=Path, help="Path to state_5.sqlite")
    parser.add_argument("--sessions-root", required=True, type=Path, help="Path to ~/.codex/sessions")
    parser.add_argument("--archived-root", required=True, type=Path, help="Path to ~/.codex/archived_sessions")
    parser.add_argument(
        "--scope",
        default="sqlite_threads",
        choices=sorted(VALID_SCOPES),
        help="Session inclusion scope",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output manifest JSONL path")
    parser.add_argument(
        "--meta-out",
        type=Path,
        help="Optional manifest metadata JSON output path (default: <out-dir>/manifest.meta.json)",
    )
    args = parser.parse_args()

    rows = build_manifest_rows(
        db_path=args.db.resolve(),
        sessions_root=args.sessions_root.resolve(),
        archived_root=args.archived_root.resolve(),
        scope=args.scope,
    )
    out_path = args.out.resolve()
    write_jsonl(out_path, rows)

    min_ts = min(r["thread"]["created_at"] for r in rows)
    max_ts = max(r["thread"]["updated_at"] for r in rows)
    meta_out = args.meta_out.resolve() if args.meta_out else (out_path.parent / "manifest.meta.json")
    manifest_meta = {
        "version": "ManifestMetaV1",
        "built_at": format_iso8601_z(datetime.now(timezone.utc)),
        "scope": args.scope,
        "manifest_path": str(out_path),
        "threads_count_at_build": len(rows),
        "thread_window_unix": {"min": int(min_ts), "max": int(max_ts)},
    }
    meta_out.write_text(json.dumps(manifest_meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("Manifest written:", out_path, f"rows={len(rows)}")
    print("Manifest meta written:", meta_out)
    print(f"thread_window_unix=[{min_ts}, {max_ts}]")


if __name__ == "__main__":
    main()
