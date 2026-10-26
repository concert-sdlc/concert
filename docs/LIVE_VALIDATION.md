# Live validation

Everything so far is tested against the in-memory fake. This runbook checks the fake against real
GitHub, then runs the runner once end to end. Do it in a **throwaway** repository, never the
project you will adopt. Nothing here touches the Friday-test project.

Three stages, each a gate for the next:

1. **Contract tests** (`tests/live/`): `RestGitHub` against real GitHub, one test per behaviour the
   runner relies on. About 15 minutes, no model, no Docker.
2. **One tick** (`concert run --once`): the PM reads a real issue and board and writes a real event.
3. **One agent run**: the sandbox, the egress proxy and a real model on one tiny issue.

Stop at the first failure. A failure in stage 1 means the fake or `RestGitHub` is wrong; fix both
(the fake first, with a failing test), then re-run the unit suite.

## 0. What you need (you, once)

The earlier probe work already produced most of this (`concert_sdlc/notes/api-probes.md`, org
`concert-throwaway`).

| Item | Notes |
|---|---|
| Throwaway repo in an **organization** | Org-owned, so the PM App can write the board (F1). Make it public, or rulesets are unavailable on a free plan (F4). |
| Branches `main` and `staging` | `staging` is the integration branch (`CONCERT_BASE_BRANCH`). |
| `.github/workflows/concert-ci.yml` on **both** branches | Any workflow that runs and passes; the check-run test needs it. Add it by hand: the Worker App has no Workflows permission. |
| PM App | Repository: Contents, Issues, Pull requests, Checks, Actions (read), Administration. Organization: Projects (write). Installed on the org by an owner, with the repo selected. |
| Worker App | Repository: Contents, Pull requests. No Workflows. Installed on the same repo. |
| Projects v2 board in the org | A single-select field `Status` whose options are the status values (`inbox`, `triage`, `spec`, `spec_approval`, `implementing`, `in_review`, `uat`, `human_uat`, `integrating`, `verifying_staging`, `on_staging`, ...; the exact list is in `process.yaml`). |
| Private keys | `.pem` files outside any repo, mode 600. |
| `process.yaml` on `staging` | Start from `tests/runner/test_service.py`. Needed for stage 2. |

## 1. Contract tests

```bash
cd ~/Workspace/concert
export CONCERT_LIVE=1
export CONCERT_REPO=concert-throwaway/concert-throwaway
export CONCERT_LIVE_CONFIRM=concert-throwaway/concert-throwaway   # must equal CONCERT_REPO
export CONCERT_RUNNER=live-test
export CONCERT_BASE_BRANCH=staging
export CONCERT_PM_APP_ID=... CONCERT_PM_INSTALLATION_ID=... CONCERT_PM_KEY=/path/pm.pem
export CONCERT_WORKER_APP_ID=... CONCERT_WORKER_INSTALLATION_ID=... CONCERT_WORKER_KEY=/path/worker.pem
export CONCERT_BOARD_OWNER=concert-throwaway CONCERT_BOARD_NUMBER=1
uv run pytest tests/live -v
```

Without `CONCERT_LIVE=1` the suite is skipped, and it exits if `CONCERT_LIVE_CONFIRM` does not name
the repo. Each run prefixes everything it creates with `live-<epoch>` and closes its issues and
PRs and deletes its branches at the end. Merged work (one commit and one revert PR on `staging`)
stays.

| Test | Checks | If it fails, suspect |
|---|---|---|
| `test_both_apps_authenticate_and_are_bots` | JWT, installation tokens, bot identity, distinct ids | `app_auth.py`, key or installation id |
| `test_issue_comment_lifecycle` | create, edit (`edited_at`), list after id, delete, close, reopen | edit detection via `updated_at != created_at` |
| `test_open_issues_never_include_pull_requests` | the `/issues` endpoint returns PRs too | the `pull_request` filter |
| `test_a_missing_issue_is_not_found` | 404 maps to `NotFound` | `http.py` |
| `test_status_round_trips_on_the_board` | GraphQL board resolve, add item, update, read back | query shapes in `rest.py` |
| `test_an_unknown_status_option_is_refused` | error before any write | board options |
| `test_branch_commit_read_back` | tree and commit API, delete via `sha: null`, `compare`, authors | `commit`, `list_commits` |
| `test_ref_moves_and_deletes` | non-fast-forward raises `Conflict`, force, delete | 422 mapping |
| `test_orphan_branch_has_no_parents` | the event-mirror and heartbeat branches | `create_orphan_branch` |
| `test_pull_request_flow_and_merge_fencing` | draft, ready, `StaleHead` on a moved head, merge, revert PR | merge 409 text, `revertPullRequest` |
| `test_check_runs_carry_the_workflow_hash` | check run to workflow file to hash | `_workflow_paths`, `check_suite_id` |

### Result of the first live run (2026-10-25, `concert-throwaway/concert-throwaway`)

Stage 1: **11 of 11 pass** after four client fixes. All four were invisible to the fake:

| Found live | Fix |
|---|---|
| The board query had one `}` too many; GitHub's parser rejects it. | Removed the brace. The REST test stub now rejects unbalanced GraphQL. |
| REST `GET /issues` omitted a new issue for ~2 s and still listed a closed one. | `list_open_issues` reads GraphQL `repository.issues(states: OPEN)`, which is read-your-writes. |
| Right after a push the PR shows the old head and `mergeable: null`, and the merge answers 405 even for the right sha. | `merge_pr` waits (1, 2, 4, 8, 15 s) while the PR is unsettled. A 405 with the expected head and a known `mergeable` is a `Conflict` at once; a head that stays elsewhere is `StaleHead`. |
| A second 409 wording for a moved head: "Head branch is out of date". | Treated as `StaleHead` only if the PR head differs from the expected one; otherwise a `Conflict`. |

Still not modelled by the fake: the ~2 s lag of REST listings (the client no longer depends on it),
and the transient PR head after a push (the client now waits it out).

Environment note: a python.org Python on macOS has no CA bundle. Set
`SSL_CERT_FILE=/etc/ssl/cert.pem` (it is in `~/.concert-live/env`) or the runner cannot reach GitHub.
Do the same in the `concert` user's environment on the runner host.

### Result of the cubic whole-repo review (2026-10-25)

21 findings, each checked against the code (and live where it could be): 20 fixed test-first,
1 refuted. Live tests now also cover repeated `mark_ready`/`mark_draft`, `find_revert`, binary and
executable files surviving a commit, and a file over 1 MB; 13 of 13 pass.

Refuted: `mark_ready`/`mark_draft` are idempotent on real GitHub (a repeat returns the PR unchanged),
so no guard was added. Confirmed live: GitHub names a revert branch `revert-<n>-<head branch>`.

Notable fixes: the sandbox refuses FIFOs and devices (a `mkfifo` hung the runner); binary files in a
repository no longer crash polls (`BinaryBlob` reuses the blob by id); Steering fails closed on an
untrusted mirror; writes are not retried after a 5xx; a crashed worker commit is redone; handoffs
fit in a comment and are not re-paid for when GitHub refuses them; the lease is kept alive during a
run; forged handoffs cost no API calls; closed issues leave the queue; `/pm status` text is inert;
`concert init` recovers from a half-finished run; redirects are never followed with the token;
the docker CLI gets PATH/HOME/DOCKER_*; the proxy resolves names off the event loop.

## 2. One tick

```bash
uv run concert run --once
```

Prepare first: open one issue in the repo's `inbox` state, make sure `process.yaml` is on
`staging`, and set `CONCERT_STEERING_ISSUE` to a Steering issue number. Expected: exit 0, no
`concert:` lines on stderr, a `concert-events` branch with a first event, a heartbeat, the issue's
board Status unchanged or advanced as `process.yaml` says. Read the events branch and confirm the
hash chain is intact before moving on.

### Result of stage 2 (2026-10-25)

`concert run --once` against the throwaway repo, with `process.yaml` rendered by `plan_init` and
committed by the admin on `staging` (and the workflows on `main`): exit 0, no stderr. Issue #41
got its `created` event as a `pm-event` comment, mirrored to `concert-events/events/41.jsonl`;
heartbeats were written for `live-test` and `pm`; the board showed `inbox`. A second tick (a new
process, so state re-derived from the comments) added no event, no comment and no board item.

Notes: the PM App cannot push workflow files, so the adoption PRs of `concert init` must be opened
by an admin when workflows change (`run_init` has no CLI command yet and was not run live).
`protection: enforce` was accepted although the repo has no rulesets; no tick check complained.

## 3. One agent run

The sandbox is a container on an internal Docker network whose only neighbour is the egress proxy
(also a container). Set it up once; `check.sh` proves it from inside an agent container:

```bash
ops/agent/build.sh                 # the agent image (pinned Claude Code), with its own checks
ops/egress/setup.sh                # network `concert-egress` and the proxy `concert-proxy`
ops/egress/check.sh                # allowed host reachable; others, IP literals, other ports, a
                                   # direct route and the metadata address all refused
```

Run the runner with the runtime and network for this host (`runc` is acceptable where the host is
already a separate Linux kernel, as Colima's VM is; use `runsc` when gVisor is installed):

```bash
source ~/.concert-live/env
source ~/.concert-live/anthropic.env        # export ANTHROPIC_API_KEY=... (mode 600)
export CONCERT_DOCKER_RUNTIME=runc CONCERT_DOCKER_NETWORK=concert-egress
export CONCERT_PROXY_URL=http://concert-proxy:3128 CONCERT_AGENT_MAX_BUDGET_USD=2
uv run concert run --once        # repeat until the issue moves; or without --once to loop
```

Start with one tiny issue in the throwaway repo and the Steering issue in place. Checks: the agent
container starts and is removed; it reaches only `api.anthropic.com`; a WIP ref and a handoff
appear; no workflow file is ever pushed; the cost reported for the run stays under the cap. Only
then run the Friday test on the real project.

Verified without a model (2026-10-25, OrbStack, `runc`): the image builds and passes its checks,
`ops/egress/check.sh` passes 6 of 6, and `DockerExecutor` runs a command as uid 10001 with only the
run directory mounted and writable. Not yet verified: the same on the Colima VM (file ownership on
its mount), and any model run.

### Result of stage 3 (2026-10-26, `runc`, real model, throwaway issue #41)

One issue went from `inbox` to `on_staging` with every agent run in the sandbox: triage (asked
five questions, answered by a human), spec (approved by hash), implementing, the `code` and
`security` reviews, automated UAT, human UAT (approved by hash), integration (the PM merged the
PR), and staging verification. A change to `process.yaml` on `staging` in the middle made the PM
rebase the branch and send it back through review and UAT, which also worked. Only the spec
documents, `hello.py` and `test_hello.py` reached `staging`; no workflow file was pushed. The
egress proxy and the container lifecycle behaved throughout.

Found live, each invisible to the fake and fixed test-first:

| Found live | Fix |
|---|---|
| The Worker App could not post the handoff (no Issues permission); the failure was swallowed and every `--once` process paid for the same run again. | `issues: write` on the Worker App; failures print as `concert:` lines; refused run ids persist in `CONCERT_STATE_DIR` (default `~/.concert`). |
| A triage run had no head commit and so saw an empty repository. | Head-less runs read the base branch. |
| A reviewer that ran the tests left `__pycache__/*.pyc`; the run was refused as "not text". | `__pycache__` ignored in the workspace, `PYTHONDONTWRITEBYTECODE=1` in the container. |
| CI skipped on a draft PR was recorded as a failure, and GitHub's newest-first order let it replace a later success. | A skipped run is no verdict; the newest run per check wins (`core/verdicts.py`). |
| After the rebase the PM recorded the stale code review of an older run (old head) and the gate waited for ever; no command recovers `human_uat`, only `/pm reset #N in_review`. | `accept_handoff` ignores review and UAT reports from a run that started on an older head. |
| Requests to `127.0.0.1` inside the sandbox went through the egress proxy (405). | `NO_PROXY` is loopback only. |
| UAT and staging verification need the `browser` capability; the image had no browser. | The image carries pinned Playwright and Chromium (`CHROMIUM_ARGS`, build check); the runner declares `browser`. The image is 2 GB. |
| With `max_parallel: 1` the second reviewer was never assigned and the first looped on lease expiry. | A run with a valid report posted keeps its lease and frees its slot. |

Cost, from the Console, up to the first `uat`: **USD 1.24** (about ten agent runs, 1.14 M input and
22 k output tokens, model `claude-opus-5-5`), including the runs the permission bug wasted. About
USD 0.12 a run, roughly USD 1 for a tiny issue through review. The whole lifecycle to `on_staging`,
with the rebase and the re-review, came to **USD 2.60** in total. The runner does not record cost;
read it from the Console Cost page.

## Record the result

Add the date, the pass or fail of each stage and every mismatch found to `docs/STATUS.md`, and
move each fixed mismatch into a regression test against the fake.
