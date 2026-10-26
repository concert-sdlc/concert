# Automated acceptance test

Run the application and follow the steps in `docs/specs/<issue>/uat.md` exactly as a user would.
Do not change any source files. Capture what you observe in `decisions`.

The sandbox has no network except the model API, so start the application yourself inside it
(on 127.0.0.1) and drive it from there. Playwright and Chromium are installed:

    const { chromium } = require("playwright");
    const browser = await chromium.launch({ args: process.env.CHROMIUM_ARGS.split(" ") });

`CHROMIUM_ARGS` is required (the container is the sandbox, so Chromium's own is off). Plain HTTP
checks with `curl` are fine where the step has no page to look at.

Exits:
- `passed`: every step behaved as described.
- `failed`: a step did not; describe it precisely in `doubts`.
- `needs_prerequisite`: the environment cannot run the app; say what is missing.
