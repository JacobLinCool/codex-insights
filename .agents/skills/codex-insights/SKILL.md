---
name: codex-insights
description: Generate Codex usage insights from state_5.sqlite and rollout files. Use when you need meta/facets/report outputs with deterministic extraction, hybrid facet classification, redacted defaults, and HTML reporting.
---

# Codex Insights

## Overview

This skill builds Codex usage insights artifacts from local data:

- Deterministic extraction: `manifest`, `meta`, `stats`
- Hybrid semantic analysis: `facets`, `narrative`
- Deterministic rendering: `report.html`

Default operating mode:

- Scope: `sqlite_threads`
- Facets engine: `hybrid` (rules + LLM)
- Report language: English
- Privacy: `redacted`

## Inputs

Required input paths:

- SQLite DB: `~/.codex/state_5.sqlite`
- Sessions root: `~/.codex/sessions`
- Archived sessions root: `~/.codex/archived_sessions`

Output workspace (default):

- `~/.codex/insights/latest`

## Quick Start

Run full pipeline:

```bash
python3 scripts/run_pipeline.py \
  --db ~/.codex/state_5.sqlite \
  --sessions-root ~/.codex/sessions \
  --archived-root ~/.codex/archived_sessions \
  --scope sqlite_threads \
  --privacy redacted \
  --engine hybrid \
  --classifier-model gpt-5.3-codex-spark \
  --narrative-model gpt-5.3-codex
```

Notes:
- Pipeline always runs incremental updates using `state_index.json` in work dir.
- To force full rebuild: remove `~/.codex/insights/latest/state_index.json` (or clear the directory) and rerun.

Partial run (deterministic only, no LLM):

```bash
python3 scripts/build_manifest.py \
  --db ~/.codex/state_5.sqlite \
  --sessions-root ~/.codex/sessions \
  --archived-root ~/.codex/archived_sessions \
  --scope sqlite_threads \
  --out ~/.codex/insights/latest/manifest.jsonl

python3 scripts/extract_meta.py \
  --manifest ~/.codex/insights/latest/manifest.jsonl \
  --out-dir ~/.codex/insights/latest/meta

python3 scripts/prepare_evidence.py \
  --manifest ~/.codex/insights/latest/manifest.jsonl \
  --meta-dir ~/.codex/insights/latest/meta \
  --privacy redacted \
  --out-dir ~/.codex/insights/latest/evidence

python3 scripts/classify_facets.py \
  --evidence-dir ~/.codex/insights/latest/evidence \
  --meta-dir ~/.codex/insights/latest/meta \
  --engine rules_only \
  --out-dir ~/.codex/insights/latest/facets

python3 scripts/aggregate_stats.py \
  --meta-dir ~/.codex/insights/latest/meta \
  --facets-dir ~/.codex/insights/latest/facets \
  --manifest ~/.codex/insights/latest/manifest.jsonl \
  --out ~/.codex/insights/latest/stats.json
```

Finish report-only stage:

```bash
python3 scripts/generate_narrative.py \
  --stats ~/.codex/insights/latest/stats.json \
  --facets-dir ~/.codex/insights/latest/facets \
  --evidence-dir ~/.codex/insights/latest/evidence \
  --privacy redacted \
  --model gpt-5.3-codex \
  --out ~/.codex/insights/latest/narrative.json

python3 scripts/render_report.py \
  --stats ~/.codex/insights/latest/stats.json \
  --narrative ~/.codex/insights/latest/narrative.json \
  --template assets/report_template.html \
  --out ~/.codex/insights/latest/report.html
```

## Pipeline Steps

1. `build_manifest.py`
- Scans rollout files from sessions + archived_sessions.
- Keeps only session IDs present in `threads` table (`sqlite_threads` scope).
- Enforces 1:1 coverage between SQLite threads and selected rollouts.
- Emits `manifest.meta.json` snapshot metadata for scope-consistent validation.

2. `extract_meta.py`
- Produces `meta/<session_id>.json` (`SessionMetaV1`).
- Uses deterministic signals only (messages, tools, errors, tokens, patch deltas).

3. `prepare_evidence.py`
- Produces `evidence/<session_id>.json`.
- Uses redacted compact evidence by default.

4. `classify_facets.py`
- Produces `facets/<session_id>.json` (`FacetV1`).
- `hybrid`: rules first, LLM refine, strict schema-locked validation.
- On repeated invalid LLM output: marks `outcome=unclear_from_transcript` with diagnostics.
- Unless user especially wants to run on all sessions, run up to 50 sessions with LLM to keep costs manageable. Use `rules_only` for full deterministic classification.

5. `aggregate_stats.py`
- Produces `stats.json` (`StatsV1`) with totals, distributions, rankings, coverage diagnostics.
- Also computes advanced sqlite-native insights (`source`, `approval_mode`, `sandbox_type`, archive lifecycle) and daily time series.

6. `generate_narrative.py`
- Produces `narrative.json` (`NarrativeV1`) from stats + validated facets + redacted evidence.

7. `render_report.py`
- Produces `report.html` from `assets/report_template.html`.

8. `validate_outputs.py`
- Enforces schema validity and cross-artifact consistency.
- Checks required report sections and redacted privacy leakage guards.
- Uses snapshot-anchored scope validation (`manifest.jsonl` + `manifest.meta.json`), not live DB row counts.

## Quality Gates

Always run before finalizing:

```bash
python3 scripts/validate_outputs.py \
  --meta-dir ~/.codex/insights/latest/meta \
  --facets-dir ~/.codex/insights/latest/facets \
  --stats ~/.codex/insights/latest/stats.json \
  --report ~/.codex/insights/latest/report.html \
  --manifest ~/.codex/insights/latest/manifest.jsonl \
  --manifest-meta ~/.codex/insights/latest/manifest.meta.json \
  --evidence-dir ~/.codex/insights/latest/evidence \
  --privacy redacted
```

Contract references:

- `references/schemas.md`
- `references/facet-taxonomy.md`
- `references/prompt-templates.md`
- `references/report-template.md`

## Troubleshooting

- `Manifest build failed: missing rollout file for sqlite thread ...`
  - `threads` contains sessions not present in scanned rollout roots.
  - Check sessions/archive directories and rollout retention.

- `FacetV1 missing keys` or enum validation failures
  - Re-run `classify_facets.py` with `--engine rules_only` to isolate LLM issues.
  - Keep strict schema; do not add ad-hoc keys.

- `Narrative generation failed validation`
  - Re-run `generate_narrative.py` with a stable model and smaller `--max-examples`.

- Redacted privacy failure in validation
  - Ensure report/evidence rendering is not printing absolute paths.
  - Keep default `--privacy redacted` for production reports.
