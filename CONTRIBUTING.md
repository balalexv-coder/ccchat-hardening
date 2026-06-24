# Contributing

Thanks for your interest in Vivarium! Contributions — bug reports, fixes,
features, docs — are welcome.

## Development setup

```bash
pip install -r requirements.txt
pytest
```

The pure logic (transcript/pane parsers, auth, mounts policy, the JSON stores,
the task-snapshot reader) is unit-tested under [`tests/`](tests/) and runs
without Docker. **Runtime/container behaviour** (launching sessions, the tmux
pane scrape, the live chat stream) must be validated against a real Docker host —
there is no mock for it.

## Module map

| File | Responsibility |
|------|----------------|
| `backend/app.py` | FastAPI app: HTTP + WebSocket endpoints, the security gate |
| `backend/manager.py` | Docker container lifecycle (create/stop/reap), per-user credential seeds |
| `backend/session.py` | One live session: tail the JSONL transcript, scrape the tmux pane, deliver input |
| `backend/userauth.py` | App-native username/password accounts, signed cookie tokens, email recovery |
| `backend/*_store.py`, `jsonstore.py` | Small JSON-backed stores (settings, mounts, ideas, push, config) |
| `static/index.html` | The entire single-file web UI (vanilla JS) |

## Conventions

- Match the surrounding code's style; keep changes scoped to the task.
- Add/adjust unit tests for any pure-logic change; run `pytest` before opening a PR.
- Never commit secrets or host-specific values — those live in `.env` (gitignored).
- Note: environment variables use the legacy `CCCHAT_` prefix (the project was
  formerly "ccchat"); kept for compatibility with existing deployments.

## Branch model

`dev` is the integration branch; `main` is production. The example CI/CD
(`deploy.sh` + `ci/`) auto-deploys each on push. Open PRs against `dev`.
