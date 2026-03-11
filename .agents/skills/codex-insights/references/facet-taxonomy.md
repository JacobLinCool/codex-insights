# Facet Taxonomy

## outcome

Allowed enum values:

- `fully_achieved`
- `mostly_achieved`
- `partially_achieved`
- `not_achieved`
- `unclear_from_transcript`

## assistant_helpfulness

Allowed enum values:

- `essential`
- `very_helpful`
- `moderately_helpful`
- `slightly_helpful`
- `unhelpful`

## session_type

Allowed enum values:

- `single_task`
- `multi_task`
- `iterative_refinement`
- `exploration`

## primary_success

Allowed enum values:

- `multi_file_changes`
- `good_explanations`
- `correct_code_edits`
- `proactive_help`
- `good_debugging`
- `none`

## goal_categories keys

Allowed keys for `goal_categories` map:

- `bug_fix`
- `feature_implementation`
- `code_review`
- `code_analysis`
- `refactor`
- `documentation`
- `planning`
- `testing`
- `performance_optimization`
- `tooling`
- `ui_ux`
- `research`
- `devops`
- `workflow_automation`

## friction_counts keys

Allowed keys for `friction_counts` map:

- `wrong_approach`
- `buggy_code`
- `misunderstood_request`
- `user_rejected_action`
- `excessive_planning`
- `excessive_changes`
- `external_limit`

## user_satisfaction_counts keys

Allowed keys for `user_satisfaction_counts` map:

- `likely_satisfied`
- `satisfied`
- `neutral`
- `dissatisfied`
- `frustrated`

## Friction and Satisfaction Semantics

Use `friction_counts` and `user_satisfaction_counts` as signal maps, not probability distributions:

- Omit zero-valued keys.
- Keep values small and evidence-backed.
- Never infer sentiment from style alone; require explicit evidence signals (interruptions, tool errors, repeated retries, completion markers).
- If evidence is weak, prefer `outcome=unclear_from_transcript` and low confidence.
