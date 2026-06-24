# Vivarium

**Self-hosted web orchestrator for [Claude Code](https://www.anthropic.com/claude-code).**
Run many sandboxed coding-agent sessions — each in its own isolated Docker
container — and drive them from your browser: a chat UI, a live task checklist,
and an embedded terminal, all in one place.

Think of it as a *vivarium*: a set of controlled, isolated habitats where each
agent lives and works behind glass, while you watch and steer through the web UI.

> Vivarium is an independent project. "Claude Code" and "Claude" are products of
> Anthropic; this software orchestrates the Claude Code CLI but is not affiliated
> with or endorsed by Anthropic.

---

## What it does

- **Per-session containers.** Each chat session runs the Claude Code CLI in `tmux`
  inside its own Docker container, with an isolated, persistent workspace.
- **Rich chat UI.** Streams the transcript live — assistant messages (Markdown +
  Mermaid), thinking, tool calls, and a **live task checklist** driven by the
  agent's `TaskCreate`/`TaskUpdate` tools.
- **Interactive prompts as buttons.** The agent's `AskUserQuestion` widget renders
  as clickable choices instead of raw terminal text.
- **Embedded terminal.** A resizable `ttyd` pane (per-session credentials) sits
  beside the chat — toggle it on/off, or swap it for a full-screen terminal.
- **Multi-user.** App-native username/password accounts (scrypt-hashed, HMAC
  cookie tokens) with an **admin approval gate** for new registrations.
- **Workspace mounts.** Admin-defined, opt-in, read-only-by-default host mounts,
  with a denylist that blocks sensitive paths.
- **Optional integrations.** One-click VS Code Remote-SSH deep links, voice input
  via an external whisper-stt service (browser Web Speech API fallback), an egress
  proxy for network-restricted sessions.

## Architecture

```
browser ──ws/https──▶ orchestrator (FastAPI)
                          │  docker.sock
                          ▼
                    session container  ── claude (Claude Code CLI) in tmux
                          │                 └─ tails the JSONL transcript +
                          ▼                    reads the tasks/ snapshot
                    isolated workspace (host bind-mount)
```

The orchestrator launches and stops session containers via the host Docker
socket, tails each session's JSONL transcript for the chat stream, and scrapes
the tmux pane for live state (busy indicator, the `AskUserQuestion` widget). See
[`backend/`](backend/) for the moving parts (`app.py`, `manager.py`, `session.py`,
`userauth.py`, `mounts_store.py`).

## Quick start

Requirements: Docker (with access to the daemon socket) and a session image
containing the Claude Code CLI (referenced by `SESSION_IMAGE`).

```bash
cp .env.example .env        # then edit for your host
docker compose up -d --build
```

The UI is then served as **plain HTTP on `http://localhost:3000`**. Vivarium does
**not** require any particular reverse proxy — put nginx / Traefik / Caddy / etc.
in front for TLS and remote access, or just use it locally. (The `caddy reload`
step in `deploy.sh` is only the example deployment's proxy; it's configurable.)

On first run an `admin` account is seeded (set `CCCHAT_ADMIN_PASSWORD`, or read the
generated one from the logs); there is no default `admin/admin`.

## Configuration

All host-specific values live in `.env` (gitignored). See
[`.env.example`](.env.example) for the full list — work-root paths, the session
image, and optional integrations (VS Code SSH host, STT upstream, extra admins).

## Deployment

[`deploy.sh`](deploy.sh) + [`ci/`](ci/) implement a simple **pull-based CI/CD**:
a systemd timer polls the Git remote and redeploys when a branch advances
(`dev` → a dev environment, `main` → production), building the image, running the
tests inside it, swapping the container, and health-checking it directly with
rollback on failure. These files are an example layout — adapt the paths and hosts
to your own infrastructure.

## Development

```bash
pip install -r requirements.txt
pytest
```

Pure logic (parsers, auth, mounts, the task-snapshot reader) is unit-tested under
[`tests/`](tests/). Runtime/container behaviour must be validated against a real
Docker host.

## License

[MIT](LICENSE) © balalexv-coder
