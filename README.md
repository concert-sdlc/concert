# Concert

A project-management-driven AI SDLC runner: the tracker's states are the process, a deterministic
PM drives issues through `inbox → triage → spec → spec_approval → implementing → in_review → uat →
human_uat → integrating → verifying_staging → on_staging`, and sandboxed agents do the work between
human gates. Design: `~/.gstack/projects/concert_sdlc/` (the reviewed design doc).

```
uv sync
uv run pytest            # ~900 tests, no network
uv run concert run --once
```

Status, what is built, what is deliberately missing and what needs a person: [docs/STATUS.md](docs/STATUS.md).
