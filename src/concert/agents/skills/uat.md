# Automated acceptance test

Run the application and follow the steps in `docs/specs/<issue>/uat.md` exactly as a user would.
Do not change any source files. Capture what you observe in `decisions`.

Exits:
- `passed`: every step behaved as described.
- `failed`: a step did not; describe it precisely in `doubts`.
- `needs_prerequisite`: the environment cannot run the app; say what is missing.
