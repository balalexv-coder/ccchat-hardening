"""Admin-configurable optional mounts (was a hardcoded dict in manager.py).

Global config (not per-user), stored in /state/mounts.json as a list of entries:
    {name, path, description, admin_only, read_only, dest}
- name        : short slug, also the default container destination (/<name>)
- path        : HOST path (the docker daemon resolves bind mounts against the host fs).
                Supports aliases, expanded per session at mount time:
                  @workspace        -> the root holding ALL users' chat workspaces
                  @workspace/@user  -> this user's folder (all their chats)
                  @user             -> the user's resolved folder name (slug)
- description : text injected into the session system prompt when this mount is attached
- admin_only  : if true, only admins may select it at session creation (review #3/#4)
- read_only   : mount read-only (default true — safe for credential-bearing paths)
- dest        : optional explicit container path (defaults to "/<name>")

Empty by default (no entries on a fresh install). Admins edit the table via /api/admin/mounts;
everyone selects from it (filtered) at session creation.
"""
import os
import re
from pathlib import Path

from . import jsonstore

MOUNTS_FILE = Path(os.environ.get("CCCHAT_MOUNTS", "/state/mounts.json"))
_LOCK = jsonstore.lock_for(MOUNTS_FILE)

_NAME_RE = re.compile(r"[^a-z0-9_-]+")

# Host paths that must NEVER be a bind source — read access alone = host/root compromise. Rejected.
_DENY = ("/", "/var/run/docker.sock", "/run/docker.sock", "/var/run", "/run",
         "/proc", "/sys", "/dev", "/boot")
# Sensitive host trees (secrets / host config): permitted ONLY as admin-only, read-only mounts.
_SENSITIVE = ("/root/machines", "/root/.ssh", "/root/claude-term", "/root/.claude",
              "/etc", "/var/lib/docker", "/home", "/state")


def _under(norm: str, prefixes) -> bool:
    for p in prefixes:
        pp = p.rstrip("/")
        if pp == "":                       # root "/": exact match only (don't match every abs path)
            if norm == "/":
                return True
        elif norm == pp or norm.startswith(pp + "/"):
            return True
    return False


def _load() -> list:
    """The configured mounts. Empty list on a fresh install (no hardcoded defaults)."""
    d = jsonstore.load(MOUNTS_FILE, [])
    return d if isinstance(d, list) else []


def _save(items: list) -> None:
    jsonstore.save(MOUNTS_FILE, items)


def all_mounts() -> list:
    return _load()


def get(name: str):
    for m in _load():
        if m.get("name") == name:
            return m
    return None


def dest_of(m: dict) -> str:
    return m.get("dest") or ("/" + m.get("name", "").strip("/"))


def expand_path(path: str, user_slug: str, work_root: str) -> str:
    """Expand the @workspace / @user aliases into a concrete HOST path for one session.
        @workspace -> work_root (all users' workspaces live under here)
        @user      -> user_slug (the user's resolved folder name)
    Plain absolute paths pass through unchanged."""
    out = (path or "").replace("@workspace", work_root.rstrip("/")).replace("@user", user_slug or "")
    return re.sub(r"/+", "/", out)  # collapse any doubled slashes from substitution


def visible_for(is_admin: bool) -> list:
    """Mounts a user may see/select at session creation (name + description only)."""
    return [{"name": m["name"], "description": m.get("description", "")}
            for m in _load() if is_admin or not m.get("admin_only")]


def validate(items) -> list:
    """Sanitise an admin-submitted table into clean entries; skips invalid rows.
    A path is valid if it is absolute ("/...") or uses an alias ("@workspace...")."""
    out, seen = [], set()
    for raw in (items or []):
        if not isinstance(raw, dict):
            continue
        name = _NAME_RE.sub("", str(raw.get("name", "")).strip().lower())[:40]
        path = str(raw.get("path", "")).strip()
        dest = (str(raw.get("dest")).strip() if raw.get("dest") else "/" + name)
        if not name or name in seen or not (path.startswith("/") or path.startswith("@")):
            continue
        # block mount-option injection / traversal in either the source or container path
        if ":" in path or ":" in dest or ".." in path or ".." in dest:
            continue
        admin_only = bool(raw.get("admin_only", False))
        read_only = bool(raw.get("read_only", True))
        # Apply the host-path policy to literal absolute paths (aliases expand to user workspaces).
        if path.startswith("/"):
            norm = os.path.normpath(path)
            norm = re.sub(r"/{2,}", "/", norm)   # collapse a leading "//" (POSIX keeps it) so the denylist can't be bypassed
            if _under(norm, _DENY):
                continue                      # never allowed as a bind source
            if _under(norm, _SENSITIVE):
                admin_only = True             # secrets/host config: admins only, read-only
                read_only = True
        seen.add(name)
        out.append({
            "name": name,
            "path": path,
            "description": str(raw.get("description", ""))[:2000],
            "admin_only": admin_only,
            "read_only": read_only,
            "dest": dest,
        })
    return out


def replace(items) -> list:
    clean = validate(items)
    _save(clean)
    return clean
