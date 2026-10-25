# Status

Milestone 1 (the "Friday test"): one runner, one adopted project. Everything below is covered by
tests that run against an in-memory fake of GitHub; **real GitHub was exercised on 2026-10-25** (11 live contract tests,
see [LIVE_VALIDATION.md](LIVE_VALIDATION.md)); nothing has run against Docker or a model yet.

## Built

| Area | Where | Notes |
|---|---|---|
| Event log, derived state, transitions | `core/events`, `derive`, `transitions` | hash-chained pm-events; the log is the source of truth |
| Decisions | `core/decide`, `effects`, `ci`, `evidence`, `queue`, `dependencies`, `kits` | pure; no I/O |
| Poll loop (one issue) | `runner/poll.py` | adoption, branch checks, commit binding, intake, effects, evidence, merge gate |
| Cycle (all issues) | `runner/cycle.py` | queue head, freeze, dependencies, fails closed on a failed poll |
| Service tick + CLI | `runner/service.py`, `cli/` | reloads `process.yaml`, heartbeats, per-issue error isolation |
| Worker + agent | `runner/worker.py`, `agents/claude_code.py` | WIP refs, handoffs, workflow edits never pushed |
| Sandbox | `sandbox/` | workspace safety, Docker argv, egress policy and CONNECT proxy |
| GitHub | `github/` | fake (tests), REST+GraphQL client, App auth, urllib transport |

Lifecycle features: gates and kits, CI evidence, security/protected-path requirements, merge at an
expected head, staging verification with one re-run, auto-revert (exact-inverse check) and
re-landing, freeze, hotfixes, `needs_input`, dependencies, close/reopen, duplicates, blocker comments.

## Not built yet (no person needed)

- Admin `/pm` commands (`pause`, `resume`, `reset`, `unfreeze`, `revert`, `status`, ...).
- `concert adopt` and generated workflows (`concert-ci.yml`, `concert-watchdog.yml`) with hash pinning.
- Daily digest and delivery metrics; lesson capture; docs/workflow issue types.
- Pre-gate refresh (rebase before the UAT kit) and the `integrating` re-prove path.
- Revert-failure re-run of CI on the revert PR (a failure freezes at once).
- Quota/pause handling for the agent; `needs_prerequisite` only defers (see below).

## Known deviations from the design

- `needs_prerequisite` / `blocked_by` defer the run behind the declared dependency and never
  charge; a bound of 3 deferrals per state ends in a block (design: charge unless declared before).
- A `start` dependency never holds back the spec itself (design: declared by the spec run takes
  effect on approval). Agent-declared dependencies are applied at once.
- The worker reads whole trees to materialise a workspace; for big repos it should clone.
- `edited` comments are detected by `updated_at != created_at` over REST.
- Issue closed after merge is noted, but no Steering note is posted (there is no Steering issue yet).

## Needs a person

1. GitHub Apps (PM and Worker), installation ids, bot user ids, private keys (paths in env).
2. A Projects v2 board with a single-select `Status` whose options are the status values.
3. Host setup scripts with `sudo` (`ops/` in the design repo), Docker + gVisor, the agent image.
4. `process.yaml` on the staging branch (see `tests/runner/test_service.py` for a minimal one).
5. Live validation of `RestGitHub` (GraphQL shapes, check-run/workflow mapping) and the Friday test.

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
