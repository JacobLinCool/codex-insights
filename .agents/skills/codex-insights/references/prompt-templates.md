# Prompt Templates

These templates document intended prompting behavior for the two LLM stages.
The executable scripts build equivalent prompts programmatically.

## Facet Classification Template (Hybrid)

System intent:

- Classify one session into `FacetV1`.
- Use deterministic rule seed as prior, then refine with evidence.
- Output strict JSON only.

Template skeleton:

```text
You are classifying a coding session into FacetV1.
Return STRICT JSON only with exactly one object.
Use only allowed enum values and keys.
Do not add markdown.

Schema constraints:
{schema_json}

Deterministic rule seed:
{rule_seed_json}

Session meta:
{meta_json}

Session evidence:
{evidence_json}
```

Repair retry template:

```text
Previous output failed validation with this error:
{validation_error}
Return corrected JSON only.
```

## Narrative Template

System intent:

- Produce English report narrative from `StatsV1` and validated facets.
- Do not invent metrics.
- Respect privacy mode.
- Do not quote raw transcript text.

Template skeleton:

```text
You are producing an English Codex usage insights narrative.
Use only provided stats and examples.
Do not invent numbers.
Do not quote raw transcript text.
Paraphrase and keep privacy constraints.
Return STRICT JSON only.

Required JSON schema shape:
{narrative_schema_json}

StatsV1:
{stats_json}

Facet/Evidence examples (already privacy-filtered):
{examples_json}
```

Repair retry template:

```text
Previous output failed validation with error:
{validation_error}
Return corrected strict JSON only.
```

## Prompt Rules

- Never include full raw transcript in prompts by default.
- Keep evidence compact and redacted.
- Enforce fixed taxonomy by embedding allowed enums/keys.
- If model output is invalid after one retry, fail validation (or fallback only where explicitly designed, e.g., facets).
