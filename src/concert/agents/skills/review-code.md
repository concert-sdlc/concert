# Code review

Review the change against the approved spec and plan. Do not change any files.

Check, in order: it does what the spec says and nothing else; tests prove each behaviour and would
fail without the change; errors and edge cases are handled; the code is simple and follows project
conventions. Binding requirements in the spec that the change misses are `changes_needed`.
Deviations the agent reported in `plan_deviations` are not failures by themselves.

Exits: `clean` or `changes_needed` (put each required change in `doubts`, specific and actionable).
