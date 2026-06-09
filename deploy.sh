#!/usr/bin/env bash
# Deploy an environment from its branch.
#
#   ./deploy.sh dev    # branch dev  -> the dev environment  (compose: docker-compose.dev.yml)
#   ./deploy.sh prod   # branch main -> production           (compose: docker-compose.hardening.yml)
#
# Pulls the target branch into the shared source checkout, mirrors code into the env dir (never
# touching its state/, work/ or .env), builds the image, RUNS THE TEST SUITE in that image, and
# only then swaps the container in. On a failed test or healthcheck it rolls back to the previous
# image. Per-host config (paths, optional integrations) lives in a gitignored .env in the env dir.
set -euo pipefail

ENVN="${1:-}"
SRC_DIR=/root/ccchat-src

case "$ENVN" in
  dev)
    BRANCH=dev  ; DIR=/root/ccchat-dev       ; COMPOSE=docker-compose.dev.yml       ; PROJ=ccchat-dev ; IMG=ccchat-dev:local ;;
  prod)
    BRANCH=main ; DIR=/root/ccchat-hardening ; COMPOSE=docker-compose.hardening.yml ; PROJ=ccchat-hardening ; IMG=ccchat-hardening:local ;;
  *)
    echo "usage: $0 dev|prod" >&2 ; exit 2 ;;
esac

# Reverse-proxy reload after a redeploy: the recreated app container gets a new IP, and some proxies
# cache the upstream and need a nudge to re-resolve (else they 502). Defaults target a Caddy
# container named "caddy"; override PROXY_CONTAINER / PROXY_RELOAD_CMD for your proxy, or blank
# PROXY_CONTAINER to skip entirely (e.g. published ports / host networking / DNS-based upstreams).
PROXY_CONTAINER="${PROXY_CONTAINER:-caddy}"
PROXY_RELOAD_CMD="${PROXY_RELOAD_CMD:-caddy reload --force --config /etc/caddy/Caddyfile --adapter caddyfile}"
reload_proxy() {
  [ -n "$PROXY_CONTAINER" ] || { echo "==> proxy reload disabled"; return 0; }
  if docker exec "$PROXY_CONTAINER" $PROXY_RELOAD_CMD >/dev/null 2>&1; then
    echo "==> proxy ($PROXY_CONTAINER) re-resolved upstreams"
  else
    echo "==> warn: proxy reload skipped/failed ($PROXY_CONTAINER)"
  fi
}

# Serialise deploys: the shared source checkout is mutated (git reset) so two concurrent runs (e.g. a
# manual deploy racing the timer) would mirror a mixed tree. flock makes them queue.
exec 9>/run/ccchat-deploy.lock || true
flock 9 2>/dev/null || true

if [ ! -d "$SRC_DIR/.git" ]; then
  echo "!! $SRC_DIR is not a clone — set it up first (see ci/README.md)" >&2 ; exit 1
fi
git -C "$SRC_DIR" fetch -q origin "$BRANCH"
git -C "$SRC_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
git -C "$SRC_DIR" reset -q --hard "origin/$BRANCH"
echo "==> deploying '$ENVN' @ $(git -C "$SRC_DIR" rev-parse --short HEAD) → $DIR (project $PROJ)"

mkdir -p "$DIR"
# Mirror code into the deploy dir; runtime data (state/, work/) and .git are preserved.
rsync -a --delete \
  --exclude '.git' --exclude 'state' --exclude 'work' --exclude '.env' \
  --exclude '*.bak' --exclude '*.bak-*' --exclude '__pycache__' \
  "$SRC_DIR"/ "$DIR"/
cd "$DIR"

# Remember the currently-running image so we can roll back if the new one is bad.
docker image inspect "$IMG" >/dev/null 2>&1 && docker tag "$IMG" "${IMG%:*}:prev" || true

echo "==> building image"
docker compose -p "$PROJ" -f "$COMPOSE" build "$PROJ"

# --- test gate: run the suite INSIDE the freshly-built image before swapping it in ---
echo "==> running tests in the new image"
if ! docker run --rm -e STATIC_DIR=/app/static \
      -v "$DIR/tests:/app/tests:ro" -v "$DIR/conftest.py:/app/conftest.py:ro" \
      -v "$DIR/pyproject.toml:/app/pyproject.toml:ro" \
      "$IMG" sh -c "pip install -q pytest >/dev/null 2>&1 && cd /app && python -m pytest -q"; then
  echo "==> TESTS FAILED — not deploying '$ENVN' (running container untouched)" >&2
  exit 1
fi

echo "==> starting container"
docker compose -p "$PROJ" -f "$COMPOSE" up -d "$PROJ"

# `up` recreates the container with a NEW IP; nudge the reverse proxy to re-resolve (else it 502s).
reload_proxy

# Health-check the container directly (the public URL is behind CF Access on dev → SSO redirect).
echo "==> waiting for $PROJ to answer /healthz"
for i in $(seq 1 30); do
  if docker exec "$PROJ" python3 -c 'import sys,urllib.request
try:
    sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:3000/healthz",timeout=4).status==200 else 1)
except Exception:
    sys.exit(1)' 2>/dev/null; then
    echo "==> $ENVN healthy ✅"; exit 0
  fi
  sleep 2
done

echo "==> healthcheck FAILED ❌ — rolling back to the previous image" >&2
docker compose -p "$PROJ" -f "$COMPOSE" logs --tail 40 || true
if docker image inspect "${IMG%:*}:prev" >/dev/null 2>&1; then
  docker tag "${IMG%:*}:prev" "$IMG"
  docker compose -p "$PROJ" -f "$COMPOSE" up -d "$PROJ" || true
  reload_proxy || true
  echo "==> rolled back '$ENVN' to the previous image" >&2
fi
exit 1
