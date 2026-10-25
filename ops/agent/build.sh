#!/bin/sh
# Build the agent image and check what the runner relies on.
#   ops/agent/build.sh [claude-code-version]      (default: latest; pin it for a real host)
set -eu
cd "$(dirname "$0")"
VERSION="${1:-latest}"
IMAGE="${CONCERT_DOCKER_IMAGE:-concert-agent:latest}"

docker build --build-arg "CLAUDE_CODE_VERSION=$VERSION" -t "$IMAGE" .

echo "== checks"
# the same restrictions docker_argv applies, so a missing tool or a write to the root fails here
RUN="docker run --rm --user 10001:10001 --read-only --tmpfs /tmp:rw,size=64m --cap-drop=ALL \
  --security-opt=no-new-privileges --network=none $IMAGE"
$RUN claude --version
$RUN sh -c 'git --version && rg --version >/dev/null && python3 --version && make --version >/dev/null'
$RUN sh -c 'mkdir -p "$HOME" && touch "$HOME/ok" && echo "HOME is writable: $HOME"'
if $RUN sh -c 'touch /usr/denied' 2>/dev/null; then echo "FAIL: the root filesystem is writable" >&2; exit 1; fi
echo "ok: $IMAGE"
