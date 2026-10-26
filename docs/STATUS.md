# Status

Milestone 1 (the "Friday test"): one runner, one adopted project. Everything below is covered by
tests that run against an in-memory fake of GitHub. **Live validation, 2026-10-25/26** (see
[LIVE_VALIDATION.md](LIVE_VALIDATION.md)): 14 live contract tests against real GitHub, and one
throwaway issue taken from `inbox` to `on_staging` with real agents (Claude Code in a `runc`
container behind the egress proxy), about USD 1.24 up to UAT. Not yet exercised live: quota
pauses, workflow-file pushes, the daily digest, `concert adopt`, revert PRs, conflicts on rebase,
gVisor, and the same flow on the Colima host.

## Built

| Area | Where | Notes |
|---|---|---|
| Event log, derived state, transitions | `core/events`, `derive`, `transitions` | hash-chained pm-events; the log is the source of truth |
| Decisions | `core/decide`, `effects`, `ci`, `evidence`, `queue`, `dependencies`, `kits`, `classify`, `workflow` | pure; no I/O |
| Poll loop (one issue) | `runner/poll.py` | adoption, branch checks, commit binding, intake, effects, evidence, merge gate, rebases |
| Cycle (all issues) | `runner/cycle.py` | queue head, freeze, dependencies, fails closed on a failed poll |
| Service tick + CLI | `runner/service.py`, `cli/` | reloads `process.yaml`, heartbeats, per-issue error isolation, daily digest |
| Worker + agent | `runner/worker.py`, `agents/claude_code.py` | WIP refs, handoffs, quota pauses, lessons, workflow edits never pushed |
| Sandbox | `sandbox/`, `ops/agent/` | workspace safety, Docker argv, egress policy and CONNECT proxy, the agent image |
| GitHub | `github/` | fake (tests), REST+GraphQL client, App auth, urllib transport |
| Adoption | `adopt/`, `cli/adopting.py` | `concert adopt` (report, admin approval, PRs); the generated `concert-ci.yml` and `concert-watchdog.yml` |
| Metrics | `core/metrics.py`, `runner/digest.py` | lead time, first pass, re-entries, reverts by merge week, human-gate waits |

Lifecycle features: gates and kits, CI evidence, security/protected-path requirements, merge at an
expected head, pre-gate refresh and queue-head rebase (file-level three-way, conflicts go to
implementing then needs_input), staging verification with one re-run, auto-revert (exact-inverse
check, CI re-run once) and re-landing, freeze, hotfixes, `needs_input`, dependencies, close/reopen,
duplicates, blocker comments, agent usage-limit pauses, `docs` and `workflow` issue types, lesson
capture and run-start selection, admin `/pm pause|resume|status|reset`, the watchdog.

## Not built yet

Left out of Milestone 1 on purpose or not reached; none needs a person except where noted.

- **Branch protection:** `adopt` applies no rulesets and writes `protection: detect`; the
  detect-only incident handling (holds, `/pm accept-push`, `/pm revert-push`) does not exist, and
  nothing checks at startup that the rules a project relies on are in place.
- **More admin commands:** `/pm revert #N`, `/pm unfreeze`, `/pm reassign`, `/pm adopt-commits`,
  `/pm runner disable`, `/pm not-security-sensitive`, `/pm undo`. A frozen queue is cleared with
  `/unblock` on the blocked issue.
- **Credential gateway:** the agent gets the model credential as an environment variable
  (`CONCERT_AGENT_AUTH`) instead of through a proxy that injects it, so a prompt-injected agent can
  read it inside its own sandbox (the egress allowlist still limits where it can send it).
- **Local-first verification:** `ci.local` is rendered into the CI workflow, but workers do not run
  the checks before pushing, so CI minutes are spent on every ready PR.
- **Digest gaps:** GitHub health, Actions minutes this month, and manual-revert events.
- **Adopt gaps:** no Projects v2 board, labels or App creation (use `ops/create-github-app.py` and
  `gh project`), no gap issues, no workflow-trigger inventory.
- **Lessons:** curation PRs, packs and export are Milestone 2 by design.
- **Milestone 2 by design:** fleet and coordinator failover, reprioritization, `concert init`, the
  plugin, a second agent adapter.

## Known deviations from the design

- `needs_prerequisite` / `blocked_by` defer the run behind the declared dependency and never
  charge; a bound of 3 deferrals per state ends in a block (design: charge unless declared before).
- A `start` dependency never holds back the spec itself (design: declared by the spec run takes
  effect on approval). Agent-declared dependencies are applied at once.
- The worker reads whole trees to materialise a workspace; for big repos it should clone.
- `edited` comments are detected by `updated_at != created_at` over REST.
- A staging rebase is a file-level merge into one commit: two edits to the same file conflict even
  when git could have merged them, which sends the issue back to implementing.
- The digest covers the issues the runner has seen since it started (their events are kept in
  memory), not the whole history.
- The quota pause keeps a run's lease by listing it in the heartbeat; after 6 hours or 24 pauses it
  is left to lapse and is charged as an infra retry.

## Needs a person

1. GitHub Apps (PM and Worker), installation ids, bot user ids, private keys (paths in env).
2. A Projects v2 board with a single-select `Status` whose options are the status values.
3. Host setup scripts with `sudo` (`ops/` in the design repo), Docker (`runc` for now; gVisor
   later); the agent image builds with `ops/agent/build.sh` (Claude Code, Playwright, Chromium).
4. `process.yaml` on the staging branch (see `tests/runner/test_service.py` for a minimal one).
5. The Friday test on the real project (`concert adopt` first).

## Environment

`CONCERT_REPO`, `CONCERT_RUNNER`, `CONCERT_{PM,WORKER}_{APP_ID,INSTALLATION_ID,KEY}`,
optional `CONCERT_BASE_BRANCH`, `CONCERT_INTERVAL`, `CONCERT_BOARD_{OWNER,NUMBER}`, `CONCERT_MODEL`,
`CONCERT_DOCKER_{IMAGE,NETWORK,RUNTIME}`, `CONCERT_PROXY_URL`, `CONCERT_STEERING_ISSUE`.

### How the agent signs in

| `CONCERT_AGENT_AUTH` | Credential passed into the sandbox (by name only) | Spend control |
|---|---|---|
| `api_key` (default) | `ANTHROPIC_API_KEY` | `CONCERT_AGENT_MAX_BUDGET_USD` per run (default 5); set a monthly limit on the key's Console workspace too |
| `oauth_token` | `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` | none: the plan's own quota applies and is shared with your interactive use |

Exactly one credential reaches the sandbox: Claude Code prefers an API key over the token, so
passing both would bill the key without being asked. `concert run` refuses to start when the chosen
variable is unset. Never run with `--bare`: it ignores the OAuth token.

Anthropic's terms (checked 2026-10-25, code.claude.com/docs/en/legal-and-compliance) say Pro and Max
limits "assume ordinary, individual usage", that products and services should use API keys, and that
plan credentials must not be routed or intermediated on behalf of others. A subscription token for
your own scripts is documented, but whether an always-on autonomous runner counts as ordinary use is
not settled; `oauth_token` is therefore off by default and your own risk. Ask Anthropic if unsure.
