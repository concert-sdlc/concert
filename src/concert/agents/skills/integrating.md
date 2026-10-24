# Integrate

Bring the branch up to date with the target branch and prepare it for merge. Rebase onto the
target branch, resolve conflicts carefully, and re-run the project's checks. Never rewrite history
other than this rebase, and do not add unrelated changes.

Exits:
- `ready_to_merge`: the branch is already up to date and every check passes.
- `rebased`: you rebased or resolved conflicts; the change must be reviewed again. List the old and
  new commits in `rebases`.

You cannot merge: a separate service does that after verifying every requirement.
