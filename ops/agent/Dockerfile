# The image an agent run executes in (docker_argv in concert/sandbox/executor.py).
#
# The container is the permission boundary: it runs as an unprivileged user with a read-only root,
# no capabilities, no network except the egress proxy, and only the run's directory mounted at
# /work. So this image holds the agent CLI and the usual tools, never credentials: the model
# credential reaches it as an environment variable at run time.
#
# Projects whose checks need other runtimes extend it:   FROM concert-agent:latest
#                                                         USER root / RUN apt-get ... / USER 10001
FROM node:22-slim

# no default: the CLI that handles the model credential must be a version you chose
ARG CLAUDE_CODE_VERSION
# the browser UAT and staging verification drive (runner capability `browser`), pinned too
ARG PLAYWRIGHT_VERSION=1.49.1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN test -n "$CLAUDE_CODE_VERSION" \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates ripgrep python3 make curl \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "playwright@${PLAYWRIGHT_VERSION}" \
 && claude --version \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /opt/playwright \
 && rm -rf /var/lib/apt/lists/*

# The root filesystem is read-only at run time; /tmp is a tmpfs, so the CLI keeps its state there.
# Chromium cannot create its own sandbox with every capability dropped and no user namespaces, and
# /dev/shm is small; the container is the boundary, so `chromium.launch(args=CHROMIUM_ARGS)` says so.
ENV NODE_PATH=/usr/local/lib/node_modules \
    CHROMIUM_ARGS="--no-sandbox --disable-dev-shm-usage --disable-gpu"
ENV HOME=/tmp/home \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin agent
USER 10001:10001
WORKDIR /work
