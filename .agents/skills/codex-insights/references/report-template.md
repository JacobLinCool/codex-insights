# Report Template Requirements

`assets/report_template.html` must render a deterministic Codex-branded report with the following sections:

1. `At a Glance`
2. `What You Work On`
3. `How You Use Codex`
4. `Wins`
5. `Friction`
6. `Feature Suggestions`
7. `Patterns`
8. `Horizon`

## Structural Requirements

- Include generated timestamp and analyzed time range.
- Include UTC date-range controls (`7D/30D/90D/All + custom start/end`) that recompute KPI cards and charts client-side.
- Include high-level metric cards:
  - sessions
  - facet coverage
  - tool calls/errors
  - token totals
  - archived session ratio
- Include advanced chart blocks:
  - user response time distribution
  - daily activity trend (time series)
  - model usage trend (time series; one line per model)
  - model outcome composition (stacked bars with ratio/count toggle)
  - project context switching and outcome-by-switch-intensity charts
  - concurrency quality KPIs (max concurrent sessions, overlap minutes)
  - time-of-day and tool-error distributions
  - sqlite-native distributions (source, approval mode, sandbox type, CLI versions)
- Narrative sections stay full-window even when date filter is active (must be clearly labeled in UI).
- Include at least these distribution tables:
  - top tools
  - outcome histogram
  - friction histogram
  - satisfaction histogram
  - tool error categories

## Privacy Requirements (Default Redacted)

- Do not render raw transcript lines.
- Do not render absolute local file paths.
- Narrative examples must come from redacted evidence snippets.

## Rendering Contract

`render_report.py` must consume:

- `stats.json`
- `narrative.json`
- `assets/report_template.html`

and output:

- `report.html` with all section placeholders replaced.

## Failure Conditions

Validation should fail if:

- Required section headings are missing.
- Coverage metrics are inconsistent with session counts.
- Redacted mode report appears to leak absolute paths.
