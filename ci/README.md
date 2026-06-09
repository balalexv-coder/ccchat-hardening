# CI/CD — pull-based auto-deploy

Two environments, one codebase, branch = environment:

| Branch | Env  | Dir                     | Container          | URL                       |
|--------|------|-------------------------|--------------------|---------------------------|
| `dev`  | dev  | `/root/ccchat-dev`      | `ccchat-dev`       | dev.example.com    |
| `main` | prod | `/root/ccchat-hardening`| `ccchat-hardening` | app.example.com        |

## Flow
1. Develop on `dev`. Push → within ~60s the VPS auto-deploys to **dev**.
2. Try it on dev.example.com.
3. Happy? Merge `dev` → `main`. Push → auto-deploys to **prod**.

There is no exposed webhook and no Actions runner. A systemd timer
(`ccchat-autodeploy.timer`) polls GitHub every minute and runs `deploy.sh <env>`
when a branch's tip changes. State (last deployed SHA) lives in
`/root/.ccchat-deployed-{dev,prod}`.

## Manual deploy / rollback
```bash
/root/ccchat-src/deploy.sh dev      # or: prod
# rollback: revert the branch on GitHub (the timer redeploys), or
#   git -C /root/ccchat-src checkout <good-sha> && rsync ... (see deploy.sh)
```

## One-time VPS setup
- `/root/ccchat-src` — clone of this repo; `origin` carries the push token.
- `deploy.sh` mirrors code into the env dir (keeps `state/`, `work/`) and rebuilds.
- Install units: copy `ci/ccchat-autodeploy.{service,timer}` to `/etc/systemd/system/`,
  `systemctl enable --now ccchat-autodeploy.timer`.
- Logs: `journalctl -u ccchat-autodeploy -f`.

## Upgrading the Claude Code CLI (the session image)

Sessions run the CLI from a Docker image, and **dev and prod use different tags** so a CLI upgrade
can be vetted on dev first:

| Env  | `SESSION_IMAGE`     | built from                   |
|------|---------------------|------------------------------|
| dev  | `claude-term:next`  | `/root/claude-term` (pinned `ARG CLAUDE_VERSION`) |
| prod | `claude-term:local` | `/root/claude-term`          |

ccchat is tightly coupled to the CLI (JSONL transcript schema, the `tasks/<uuid>/*.json` snapshot,
the `TaskCreate/TaskUpdate` tool names, exact tmux-TUI strings, and the `--dangerously-skip-permissions`
flag), so a new version can break things — often **silently** (e.g. the task checklist just stops).

To test a new CLI version:
1. `cd /root/claude-term && ./build-next.sh <version>`   (e.g. `2.2.0`, or `latest`) → rebuilds `claude-term:next`.
2. On dev (dev.example.com) create a NEW session (existing ones keep their old image until recreated)
   and check: session boots & auths, the "thinking"/busy indicator, the live task checklist, the
   AskUserQuestion choice buttons, and slash commands.
3. Fix any parsing drift in `backend/session.py`, run the tests, ship to dev.
4. Promote the CLI to prod when happy: `docker tag claude-term:next claude-term:local` (or
   `./build-next.sh <version>` then retag), then recreate prod sessions to pick it up.
