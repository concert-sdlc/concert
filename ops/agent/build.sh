#!/bin/sh
# Build the agent image and check what the runner relies on.
#   ops/agent/build.sh [claude-code-version]      (default: the version this was tested with)
set -eu
cd "$(dirname "$0")"
VERSION="${1:-2.1.310}"
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
# the browser, with the restrictions above: serve a page from inside the container and read it
$RUN sh -c 'mkdir -p "$HOME/site" && cd "$HOME/site" && echo "<title>concert-ok</title>" > index.html \
  && (python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 &) && sleep 1 \
  && node -e "
const { chromium } = require(\"playwright\");
(async () => {
  const b = await chromium.launch({ args: process.env.CHROMIUM_ARGS.split(\" \") });
  const p = await b.newPage();
  await p.goto(\"http://127.0.0.1:8765/\");
  console.log(\"browser title: \" + await p.title());
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"' | grep -q "browser title: concert-ok"
echo "ok: $IMAGE"
