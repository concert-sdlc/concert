#!/bin/sh
# Create the agents' network and start the egress proxy on it. Safe to run again.
#   CONCERT_EGRESS_ALLOW=api.anthropic.com,pypi.org ops/egress/setup.sh
# Then run the runner with CONCERT_DOCKER_NETWORK=concert-egress CONCERT_PROXY_URL=http://concert-proxy:3128
set -eu
cd "$(dirname "$0")"
ALLOW="${CONCERT_EGRESS_ALLOW:-api.anthropic.com}"
NET="${CONCERT_DOCKER_NETWORK:-concert-egress}"

# internal: containers on it have no route out, whatever their own settings say
docker network inspect "$NET" >/dev/null 2>&1 || docker network create --internal "$NET" >/dev/null

CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT
(cd ../.. && uv build --wheel -o "$CTX" >/dev/null)
cp Dockerfile "$CTX/"
docker build -q -t concert-proxy "$CTX" >/dev/null

docker rm -f concert-proxy >/dev/null 2>&1 || true
docker run -d --name concert-proxy --restart unless-stopped \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges --network bridge \
  concert-proxy --allow "$ALLOW" >/dev/null
docker network connect --alias concert-proxy "$NET" concert-proxy
echo "proxy up: http://concert-proxy:3128 on network $NET, allowing: $ALLOW"
