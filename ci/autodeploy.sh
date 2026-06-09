#!/usr/bin/env bash
# Pull-based CI: poll GitHub and deploy when a tracked branch advances.
#   origin/dev  advanced → deploy.sh dev   (dev.example.com)
#   origin/main advanced → deploy.sh prod  (app.example.com)
# Driven by the ccchat-autodeploy.timer (~every 60s). Idempotent: records the last
# deployed SHA per env in /root/.ccchat-deployed-<env> and only acts on changes.
set -uo pipefail

SRC=/root/ccchat-src
DEPLOY="$SRC/deploy.sh"

git -C "$SRC" fetch -q origin dev main 2>/dev/null || { echo "$(date -Is) fetch failed"; exit 0; }

for pair in "dev:dev" "main:prod"; do
  branch="${pair%%:*}"; envn="${pair##*:}"
  remote_sha="$(git -C "$SRC" rev-parse "origin/$branch" 2>/dev/null)" || continue
  state="/root/.ccchat-deployed-$envn"
  failed="/root/.ccchat-failed-$envn"      # a sha that exhausted its retries
  count="/root/.ccchat-failcount-$envn"
  last="$(cat "$state" 2>/dev/null || true)"
  [ "$remote_sha" = "$last" ] && continue
  # stop hammering a known-bad sha — wait for a new commit (a fix) before trying again
  if [ "$(cat "$failed" 2>/dev/null)" = "$remote_sha" ]; then continue; fi
  echo "$(date -Is) $envn: ${last:-<none>} -> $remote_sha — deploying"
  if bash "$DEPLOY" "$envn"; then
    echo "$remote_sha" > "$state"; rm -f "$count" "$failed"
    echo "$(date -Is) $envn deployed ✅"
  else
    n=$(( $(cat "$count" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$count"
    if [ "$n" -ge 3 ]; then
      echo "$remote_sha" > "$failed"; rm -f "$count"
      echo "$(date -Is) $envn GIVING UP on $remote_sha after $n failures ❌ — push a fix to retry"
    else
      echo "$(date -Is) $envn deploy FAILED ❌ (attempt $n/3, will retry next tick)"
    fi
  fi
done
