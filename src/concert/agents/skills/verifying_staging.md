# Verify staging

The change has been merged to the staging branch. On a fresh, disposable environment built from
committed fixtures and migrations, run the integration, end-to-end, migration and smoke checks.
Do not change any files and do not touch any persistent deployment.

Exits: `passed` if every check passes, `failed` otherwise (put each failing check and its output
in `doubts`).
