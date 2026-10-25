#!/bin/sh
# Check the sandbox network from inside an agent container (needs ops/agent/build.sh and setup.sh).
set -u
IMAGE="${CONCERT_DOCKER_IMAGE:-concert-agent:latest}"
NET="${CONCERT_DOCKER_NETWORK:-concert-egress}"
PROXY="${CONCERT_PROXY_URL:-http://concert-proxy:3128}"
FAILS=0

agent() {
  docker run --rm --user 10001:10001 --read-only --tmpfs /tmp:rw,size=16m --cap-drop=ALL \
    --security-opt=no-new-privileges --network "$NET" "$IMAGE" "$@"
}
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILS=$((FAILS + 1)); }

echo "From inside an agent container on $NET"
code=$(agent curl -sS -o /dev/null -w '%{http_code}' --max-time 20 --proxy "$PROXY" https://api.anthropic.com/ 2>/dev/null)
[ -n "$code" ] && [ "$code" != 000 ] && pass "an allowed host is reachable through the proxy (HTTP $code)" || fail "allowed host unreachable"

agent curl -sS -o /dev/null --max-time 20 --proxy "$PROXY" https://example.com/ >/dev/null 2>&1 \
  && fail "a host that is not allowed was reachable" || pass "a host that is not allowed is refused"

agent curl -sS -o /dev/null --max-time 20 --proxy "$PROXY" https://1.1.1.1/ >/dev/null 2>&1 \
  && fail "an IP literal was reachable" || pass "an IP literal is refused"

agent curl -sS -o /dev/null --max-time 20 --proxy "$PROXY" https://api.anthropic.com:8443/ >/dev/null 2>&1 \
  && fail "a non-443 port was reachable" || pass "a port other than 443 is refused"

agent curl -sS -o /dev/null --max-time 8 --noproxy '*' https://api.anthropic.com/ >/dev/null 2>&1 \
  && fail "the agent reached the internet without the proxy" || pass "there is no route out except the proxy"

agent curl -sS -o /dev/null --max-time 8 --noproxy '*' http://169.254.169.254/ >/dev/null 2>&1 \
  && fail "the metadata address was reachable" || pass "the cloud metadata address is unreachable"

[ "$FAILS" = 0 ] && echo "All checks passed." || { echo "$FAILS check(s) failed."; exit 1; }
