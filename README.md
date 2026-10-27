# Concert

A project-management-driven AI SDLC runner. The tracker's states are the process: a deterministic
PM moves each GitHub issue through

```
inbox → triage → spec → spec_approval → implementing → in_review → uat → human_uat
      → integrating → verifying_staging → on_staging
```

and sandboxed Claude Code agents do the work between the human gates (spec approval, UAT approval).
Approvals are given by commenting on the issue with the content hash they approve
(`/approve spec`, `/approve uat <head>`), so a change after review voids the approval.

## How it works

- **PM** (one GitHub App): reads issue and PR facts, decides the next transition, merges, rebases,
  moves the Projects v2 board. It holds the only token that can merge or push workflow files.
- **Worker** (a second GitHub App, no `workflows` permission): the agent's identity. Each run
  happens in a Docker container behind an egress proxy, with a budget cap per run, and hands off
  its result in a comment the PM trusts by author id.
- **Runner**: polls for work, leases runs, starts the agent containers, and reports back.
- **Process**: `process.yaml` in the project repo declares stages, runner capabilities and parallelism.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```
uv sync
uv run pytest                 # no network; live contract tests are skipped
uv run ruff check . && uv run pyright
uv run concert --help
```

Commands: `run` (PM plus this runner's worker; `--once` for a single pass), `watchdog`, `adopt`
(put an existing organization repository under Concert), `proxy` (the egress proxy).

Agent authentication is `CONCERT_AGENT_AUTH=api_key` (default, reads `ANTHROPIC_API_KEY`) or
`oauth_token`. `CONCERT_AGENT_MAX_BUDGET_USD` caps each run and `CONCERT_MODEL` selects the model.
Never commit keys or App private keys.

## Layout

- `src/concert/`: the code (`core/` decisions, `runner/`, `cli/`, GitHub clients).
- `ops/agent/`: the agent container image (Claude Code, Playwright and Chromium pinned).
- `ops/egress/`: the egress proxy that sandboxed agents go through.
- `tests/`: unit and integration tests against fakes, plus live tests against a throwaway repo.

## Status

What is built, what is deliberately missing and what needs a person: [docs/STATUS.md](docs/STATUS.md).
How it was validated against real GitHub and a real model, including cost (USD 2.60 for one issue
from `inbox` to `on_staging`): [docs/LIVE_VALIDATION.md](docs/LIVE_VALIDATION.md).
