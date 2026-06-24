# Security

## Trust model — read this before deploying

Vivarium orchestrates per-session Docker containers via the **host Docker
socket**. Mounting `docker.sock` is **host-root-equivalent**: the orchestrator
process can create privileged containers and therefore control the host. Treat
the orchestrator as a host-root-equivalent service.

Consequences:

- **The orchestrator process is the trust boundary**, not the containers it
  launches. Anyone who can reach an admin account, or exploit the orchestrator,
  effectively has host root.
- **Run it on a host you control**, ideally dedicated, and put it behind your own
  authentication/TLS (a reverse proxy and/or an SSO gate). The app has its own
  username/password layer with an admin approval gate, but defense-in-depth is
  expected.
- **Session containers are sandboxes for the agents, not for untrusted users.**
  Workspace mounts are admin-defined, opt-in, and read-only by default, with a
  denylist for sensitive host paths — but an admin can still expose anything.
- Secrets (`.env`, `state/`, per-user credentials, signing secrets) are
  gitignored and must never be committed. There is no default `admin/admin`.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Instead, open a
private [GitHub Security Advisory](https://github.com/balalexv-coder/ccchat-hardening/security/advisories/new)
on this repository. We'll acknowledge and work on a fix; coordinated disclosure
is appreciated.
