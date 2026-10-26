# Verify staging

The change has been merged to the staging branch. On a fresh, disposable environment built from
committed fixtures and migrations, run the integration, end-to-end, migration and smoke checks.
Do not change any files and do not touch any persistent deployment.

Nothing outside the sandbox is reachable, so "disposable environment" means starting the
application inside it, on 127.0.0.1. Playwright and Chromium are installed for browser checks:
launch with `chromium.launch({ args: process.env.CHROMIUM_ARGS.split(" ") })`.

Exits: `passed` if every check passes, `failed` otherwise (put each failing check and its output
in `doubts`).
