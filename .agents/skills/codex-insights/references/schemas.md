# Codex Insights Schemas

This skill uses three strict contracts: `SessionMetaV1`, `FacetV1`, `StatsV1`.
Any missing required key is a validation failure.

## SessionMetaV1

Required fields:

- `session_id` (string)
- `project_path` (string)
- `start_time` (ISO8601 UTC string)
- `end_time` (ISO8601 UTC string)
- `duration_minutes` (non-negative integer)
- `user_message_count` (non-negative integer)
- `assistant_message_count` (non-negative integer)
- `tool_counts` (object string -> non-negative integer)
- `tool_errors` (non-negative integer)
- `tool_error_categories` (object string -> non-negative integer)
- `uses_mcp` (boolean)
- `uses_task_agent` (boolean)
- `uses_web_search` (boolean)
- `uses_web_fetch` (boolean)
- `input_tokens` (non-negative integer)
- `output_tokens` (non-negative integer)
- `cached_input_tokens` (non-negative integer)
- `first_prompt_redacted` (string)
- `user_interruptions` (non-negative integer)
- `user_response_times` (array of non-negative numbers, seconds)
- `message_hours` (array of integers in `[0,23]`)
- `git_commits` (non-negative integer)
- `git_pushes` (non-negative integer)
- `git_branch` (string or null)
- `git_sha` (string or null)
- `files_modified_patch_only` (non-negative integer)
- `lines_added_patch_only` (non-negative integer)
- `lines_removed_patch_only` (non-negative integer)
- `derivation_notes` (array of strings)

Notes:

- Token counts must use max observed `total_token_usage` values per session.
- File/line deltas are patch-only (from `apply_patch` payloads), not git diffs.
- Unknown metrics must remain explicit (zero or empty), not guessed.

## FacetV1

Required fields:

- `session_id` (string)
- `underlying_goal` (string, max 360 chars)
- `goal_categories` (object; keys from allowed taxonomy; counts are non-negative integers)
- `outcome` (enum)
- `assistant_helpfulness` (enum)
- `session_type` (enum)
- `primary_success` (enum)
- `friction_counts` (object; keys from allowed taxonomy)
- `friction_detail` (string, max 360 chars)
- `user_satisfaction_counts` (object; keys from allowed taxonomy)
- `brief_summary` (string, max 360 chars)
- `confidence` (number in `[0,1]`)
- `evidence_flags` (object string -> boolean)

## StatsV1

Required top-level fields:

- `generated_at` (ISO8601 UTC string)
- `sessions_total` (non-negative integer)
- `sessions_faceted` (non-negative integer)
- `facet_coverage_ratio` (number in `[0,1]`)
- `time_range` (object)
- `totals` (object)
- `distributions` (object)

Expected content (enforced by pipeline semantics):

- Global totals: messages, tool calls/errors, tokens, git actions, patch-only deltas, usage flags.
- Distributions/histograms: duration, response-time buckets, message hours, tool errors, outcome/helpfulness/session type/primary success, friction and satisfaction.
- Top-N rankings: tools/projects/goal categories/friction.
- Coverage diagnostics: `sessions_missing_facet`, `facet_files_extra`.
- `time_series.daily_activity_utc`: per-day sessions/messages/tool-errors/tokens derived from session start timestamps.
- `time_series.session_lifecycle_utc`: per-day created/archived session counts from sqlite thread metadata.
- `time_series.model_sessions_daily_utc`: per-day model usage counts (primary model per session from `turn_context.model`).
- `sqlite_distributions`: high-signal mixes promoted to report visuals (source/approval/sandbox/primary-model/archive/cli-major-minor/release-channel).
- `sqlite_diagnostics`: low-signal or noisy sqlite fields kept for diagnostics only (raw cli versions, model_provider, memory_mode, has_user_event, sandbox_network_access).
- `sqlite_lifecycle`: manifest coverage diagnostics and archived-session latency stats (`count`, `p50`, `p90`, `avg`, `max` hours).
- `concurrency_quality`: max concurrent sessions and overlap duration (`overlap_minutes`) with session/message shares.
- `model_outcome_matrix`: outcome ratios per model (focus outcomes: fully/mostly/not achieved).
- `project_context_switching`: daily switch counts and outcome distributions by switch-intensity bucket.
- `session_rollups`: per-session compact aggregates (no raw transcript) for report-side dynamic date filtering and chart recomputation.

## NarrativeV1 (generated, not part of strict shared validator)

- `version`, `language`, `privacy_mode`, `generated_at`, `sections`
- Required sections:
  - `at_a_glance`
  - `what_you_work_on`
  - `how_you_use_codex`
  - `wins`
  - `friction`
  - `feature_suggestions`
  - `patterns`
  - `horizon`

Each section must include:

- `title` (string)
- `summary` (string)
- `bullets` (array, at least 2 strings)
