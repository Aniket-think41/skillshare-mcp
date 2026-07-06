"""
SkillShare MCP server (stdio).

Exposes the registry to MCP clients — Claude Code, Claude Desktop, or any
agent runtime. Wraps the REST API in BACKEND_SPEC.md.

Auth: call the `login` tool to sign in via the browser (device flow) — it stores
rotating credentials that auto-refresh. Alternatively set env:

    SKILLSHARE_API_URL   e.g. https://skillshare-backend-1081098542602.us-central1.run.app
    SKILLSHARE_TOKEN     skst_… PAT or access token (overrides stored creds; for CI)

Public marketplace tools work without signing in; org/pod tools require it.
"""

import json as _json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# Shared local-scan core + auth flow live in the CLI package so CLI and MCP stay
# in lockstep (declared as a dependency in pyproject).
from skillshare_cli.authflow import CONFIG_DIR, api_url, save_creds, valid_access_token
from skillshare_cli.local.detect import DETECTORS
from skillshare_cli.local.scan import reconcile, redacted_payload, scan

# Where `login` parks the pending device session for `complete_login` to finish.
PENDING_LOGIN_FILE = CONFIG_DIR / "device-login.json"


def _base_url() -> str:
    """Backend base URL: env override → stored creds → default (via authflow)."""
    return api_url()


def _token() -> str | None:
    """Bearer token: SKILLSHARE_TOKEN env → stored creds (auto-refreshed)."""
    return valid_access_token()

mcp = FastMCP(
    "skillshare",
    instructions=(
        "SkillShare is a registry of AI skills, MCP servers, and team notes, "
        "organized as org → projects → pods. Use search/list tools to find "
        "resources, get_resource for full content (SKILL.md, MCP config JSON, "
        "note markdown), and create_note/star/import to write. Resources can "
        "carry typed attachments (link/image/video/file): upload_file stores a "
        "local file and returns a URL you can pass to create_note's attachments.\n\n"
        "PROACTIVE INBOX — do this WITHOUT being asked: at the start of each "
        "conversation (and again after you publish or create resources), call "
        "check_updates(unread_only=True). If it returns any items, tell the user "
        "up front — before other work — e.g. \"📬 2 new in your team: 'Release "
        "Train Conductor' (skill) by @dev, 'Brand Voice' published by @priya\". "
        "For each, offer to install it (get_resource / import_resource, or share "
        "its install_command) and, once the user has seen them, call "
        "mark_notifications_read to clear the inbox. Keep it to one short summary; "
        "don't nag if the inbox is empty.\n\n"
        "CONTRIBUTE BACK — when the user mentions a local skill/MCP/note, has just "
        "built one, or asks what they could share, call scan_local to find local "
        "artifacts not yet on the platform and offer to push the NEW ones. Always "
        "show the redacted preview (secrets are stripped automatically) and get "
        "explicit approval before push_artifact; if they decline, call "
        "dismiss_artifact so it isn't suggested again.\n\n"
        "SIGN IN — public marketplace/search tools work anonymously, but org/pod "
        "tools and writes need auth. If a tool fails with a 'not signed in' error, "
        "call the `login` tool, give the user the verification URL + code to open in "
        "their browser, then call `complete_login` to finish. Right after a "
        "successful login, OFFER (don't force) to enable the status bar counts via "
        "`setup_statusline` — it edits the user's Claude Code settings.json, so only "
        "do it once they say yes.\n\n"
        "FROM GITHUB — when the user shares a GitHub repo URL (or asks to add a "
        "skill/MCP that lives on GitHub), call import_from_github(url, "
        "dry_run=True) to detect what's inside (SKILL.md skills, server.json / "
        ".mcp.json MCP servers, or a README note), show them the detected items, "
        "then call it again with a scope (org_slug or pod_id) to import."
    ),
)


class SkillShareError(Exception):
    pass


def _request(method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> Any:
    t = _token()
    headers = {"Authorization": f"Bearer {t}"} if t else {}
    with httpx.Client(base_url=_base_url(), timeout=30) as client:
        res = client.request(method, path, json=json, params=params, headers=headers)
    if res.status_code == 401:
        raise SkillShareError("Not signed in (or the session expired). Run the `login` tool to authenticate.")
    if res.status_code >= 400:
        try:
            err = res.json()["error"]
            raise SkillShareError(f"{err['code']}: {err['message']}")
        except (KeyError, ValueError):
            raise SkillShareError(f"HTTP {res.status_code}: {res.text[:200]}")
    return res.json()


def _upload(path: str, kind: str) -> dict:
    """Upload a local file to POST /api/uploads (multipart) → {file_url, ...}."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise SkillShareError(f"file not found: {path}")
    t = _token()
    headers = {"Authorization": f"Bearer {t}"} if t else {}
    with p.open("rb") as fh, httpx.Client(base_url=_base_url(), timeout=120) as client:
        res = client.post("/api/uploads", data={"kind": kind}, files={"file": (p.name, fh)}, headers=headers)
    if res.status_code >= 400:
        try:
            err = res.json()["error"]
            raise SkillShareError(f"{err['code']}: {err['message']}")
        except (KeyError, ValueError):
            raise SkillShareError(f"HTTP {res.status_code}: {res.text[:200]}")
    return res.json()


def _summary(r: dict) -> dict:
    """Trimmed resource for list results — keeps agent context small."""
    return {
        "id": r["id"],
        "type": r["type"],
        "title": r["title"],
        "description": r["description"],
        "version": r["version"],
        "tags": r["tags"],
        "scope": r.get("scope_label") or r["scope_type"],
        "visibility": r.get("visibility"),
        "author": (r.get("author") or {}).get("username"),
        "stars": r["stars_count"],
        "is_public": r["is_public"],
        "updated_at": str(r["updated_at"]),
    }


def _resource_scope(org_slug: str, project_id: str, pod_id: str) -> dict:
    """Resolve a resource scope from exactly one of org_slug / project_id /
    pod_id. Most specific wins if several are (wrongly) passed. A resource is
    inherited by everything beneath its scope and notifies that scope's
    members."""
    if pod_id:
        return {"scope_type": "POD", "scope_id": pod_id}
    if project_id:
        return {"scope_type": "PROJECT", "scope_id": project_id}
    if org_slug:
        org = _request("GET", f"/api/orgs/{org_slug}")
        return {"scope_type": "ORG", "scope_id": org["id"]}
    raise SkillShareError("Provide a target scope: org_slug, project_id, or pod_id")


# ---------- auth: browser device login ----------

@mcp.tool()
def login(client_name: str = "SkillShare MCP") -> dict:
    """Start a browser sign-in for SkillShare (so org/pod tools work). Returns a
    verification URL + short code the user opens in their browser to log in or
    sign up and approve this client. After they approve, call `complete_login`.

    (Public marketplace/search tools work without signing in; only org/pod tools
    and writes need it.)"""
    base = _base_url()
    try:
        res = httpx.post(f"{base}/api/auth/device/start", json={"client_name": client_name}, timeout=30)
    except httpx.HTTPError as e:
        raise SkillShareError(f"cannot reach {base} ({e.__class__.__name__})")
    if res.status_code >= 400:
        raise SkillShareError(f"could not start login (HTTP {res.status_code})")
    d = res.json()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_LOGIN_FILE.write_text(_json.dumps({
        "api_url": base, "device_code": d["device_code"], "interval": d.get("interval", 5),
    }))
    return {
        "verification_url": d["verification_uri_complete"],
        "user_code": d["user_code"],
        "expires_in": d["expires_in"],
        "instructions": (
            "Tell the user to open verification_url in their browser, sign in or create an account, "
            "and approve this device. Then call complete_login to finish."
        ),
    }


@mcp.tool()
def complete_login(wait_seconds: int = 60) -> dict:
    """Finish the sign-in started by `login`: polls until the user approves it in
    the browser. If it returns status "pending", ask the user to approve and call
    this again. On "approved" the MCP server is authenticated (credentials are
    stored and auto-refreshed)."""
    if not PENDING_LOGIN_FILE.exists():
        raise SkillShareError("No pending login — call `login` first.")
    pend = _json.loads(PENDING_LOGIN_FILE.read_text())
    base, device_code = pend["api_url"], pend["device_code"]
    interval = max(1, int(pend.get("interval", 5)))
    deadline = time.time() + max(5, min(int(wait_seconds), 110))
    while time.time() < deadline:
        res = httpx.post(f"{base}/api/auth/device/token", json={"device_code": device_code}, timeout=30)
        st = res.json() if res.status_code < 400 else {}
        status = st.get("status")
        if status == "approved":
            save_creds({
                "api_url": base,
                "access_token": st["access_token"],
                "refresh_token": st["refresh_token"],
                "expires_at": time.time() + int(st.get("expires_in", 900)),
                "username": (st.get("user") or {}).get("username"),
            })
            PENDING_LOGIN_FILE.unlink(missing_ok=True)
            return {
                "status": "approved",
                "user": st.get("user"),
                "tip": "You can show this user's install/push counts in Claude Code's status bar — "
                       "offer to run setup_statusline.",
            }
        if status in ("denied", "expired"):
            PENDING_LOGIN_FILE.unlink(missing_ok=True)
            return {"status": status, "hint": "Start again with the `login` tool."}
        time.sleep(interval)
    return {"status": "pending", "hint": "Not approved yet — approve in the browser, then call complete_login again."}


@mcp.tool()
def setup_statusline(scope: str = "user", remove: bool = False, force: bool = False) -> dict:
    """Add (or remove) the SkillShare status line in Claude Code's settings.json, so
    the user's install/push counts show in the status bar. Ask the user before
    enabling it (this edits their settings.json). Only Claude Code renders a status
    line, and the change takes effect the next time Claude Code starts.

    Args:
        scope: "user" (~/.claude/settings.json) or "project" (./.claude/settings.json).
        remove: remove the SkillShare status line instead of adding it.
        force: replace an existing different statusLine (or remove a non-SkillShare one).
    """
    from skillshare_cli import statusline as sl

    try:
        return sl.disable(scope=scope, force=force) if remove else sl.enable(scope=scope, force=force)
    except sl.StatusLineError as e:
        raise SkillShareError(str(e))


# ---------- read: marketplace (no auth needed) ----------

@mcp.tool()
def search_marketplace(
    query: str = "",
    resource_type: str = "",
    tag: str = "",
    sort: str = "stars",
) -> list[dict]:
    """Search the public SkillShare marketplace for skills, MCP servers, and notes.

    Args:
        query: free-text search over title, description, and tags.
        resource_type: filter — "SKILL", "MCP", or "NOTE" (empty = all).
        tag: filter by exact tag, e.g. "code-review".
        sort: "stars" | "installs" | "newest" | "updated".
    """
    params = {"q": query or None, "type": resource_type or None, "tag": tag or None, "sort": sort}
    rows = _request("GET", "/api/public/resources", params={k: v for k, v in params.items() if v})
    return [_summary(r) for r in rows]


@mcp.tool()
def get_resource(resource_id: str) -> dict:
    """Get a resource's full content by id: SKILL.md for skills, server URL +
    config JSON for MCP servers, markdown body for notes. Works for public
    resources and (with a token) anything your orgs can see."""
    r = _request("GET", f"/api/resources/{resource_id}") if _token() else _request(
        "GET", f"/api/public/resources/{resource_id}"
    )
    out = _summary(r)
    out.update(
        {
            "content_md": r.get("content_md"),
            "server_url": r.get("server_url"),
            "config_json": r.get("config_json"),
            "file_url": r.get("file_url"),
            "attachments": r.get("attachments") or [],
        }
    )
    return out


@mcp.tool()
def use_resource(resource_id: str) -> dict:
    """Use a skill or note RIGHT NOW without installing anything to disk.

    Unlike install/import (which write files into ~/.claude/skills or an org),
    this just pulls the full content and hands it back framed as ready-to-apply
    instructions for THIS conversation. Call it when the user says things like
    "use the pr-reviewer skill", "apply the brand-voice note", or "follow the
    release-train skill" and you should adopt that guidance inline for the
    current session — nothing is saved, nothing is copied to an org.

    Returns the content plus an `apply` field: treat `apply` as authoritative
    instructions/context for the rest of this conversation.
    """
    r = _request("GET", f"/api/resources/{resource_id}") if _token() else _request(
        "GET", f"/api/public/resources/{resource_id}"
    )
    rtype = r.get("type")
    title = r.get("title", "resource")
    content = r.get("content_md") or ""
    if rtype == "MCP":
        # An MCP server isn't "applied" as text — surface how to connect to it.
        cfg = r.get("config_json") or ""
        url = r.get("server_url") or ""
        apply = (
            f"'{title}' is an MCP server, not a skill/note — it can't be applied as "
            f"inline instructions. To use it, add it to your MCP client config"
            + (f" (server URL: {url})" if url else "")
            + (f":\n{cfg}" if cfg else ".")
        )
    else:
        label = "skill" if rtype == "SKILL" else "note"
        apply = (
            f"Adopt the following {label} — \"{title}\" — as active guidance for the "
            f"rest of this conversation. Apply it directly; it has NOT been installed "
            f"to disk, so rely on the content below.\n\n"
            f"--- BEGIN {label.upper()}: {title} ---\n"
            f"{content}\n"
            f"--- END {label.upper()}: {title} ---"
        )
    out = _summary(r)
    out.update(
        {
            "apply": apply,
            "content_md": content,
            "server_url": r.get("server_url"),
            "config_json": r.get("config_json"),
            "attachments": r.get("attachments") or [],
            "installed": False,
        }
    )
    return out


@mcp.tool()
def get_publisher(username: str) -> dict:
    """Get a marketplace publisher's profile and their public resources."""
    p = _request("GET", f"/api/public/publishers/{username}")
    return {
        "username": p["username"],
        "display_name": p["display_name"],
        "bio": p["bio"],
        "verified": p.get("verified", False),
        "followers_count": p.get("followers_count", 0),
        "following": p.get("following", False),
        "resources": [_summary(r) for r in p["resources"]],
    }


@mcp.tool()
def follow_publisher(username: str) -> dict:
    """Follow a marketplace publisher. The user gets an inbox notification
    (check_updates) whenever that publisher ships something new. Requires a token."""
    return _request("POST", f"/api/public/publishers/{username}/follow")


@mcp.tool()
def unfollow_publisher(username: str) -> dict:
    """Stop following a publisher. Requires a token."""
    return _request("DELETE", f"/api/public/publishers/{username}/follow")


@mcp.tool()
def follow_org(org_slug: str) -> dict:
    """Follow (watch) an organization — get an inbox notification when it
    publishes to the marketplace. Requires a token."""
    return _request("POST", f"/api/orgs/{org_slug}/follow")


@mcp.tool()
def unfollow_org(org_slug: str) -> dict:
    """Stop following an organization. Requires a token."""
    return _request("DELETE", f"/api/orgs/{org_slug}/follow")


# ---------- read: my orgs (token required) ----------

@mcp.tool()
def whoami() -> dict:
    """Who am I authenticated as, and do I have unread inbox items? (Requires
    SKILLSHARE_TOKEN.) `unread_notifications` counts skills/notes/MCP a teammate
    added in a scope you belong to — call check_updates to see them."""
    u = _request("GET", "/api/auth/me")
    try:
        unread = (_request("GET", "/api/notifications/unread_count") or {}).get("count", 0)
    except SkillShareError:
        unread = 0
    return {
        "username": u["username"],
        "display_name": u["display_name"],
        "email": u["email"],
        "is_publisher": u.get("is_publisher", False),
        "unread_notifications": unread,
    }


def _org_terms(o: dict) -> dict:
    """The org's Project/Pod labels (custom-terminology), defaulting to the
    standard words. Use these when talking to the user so you mirror how their
    org names the hierarchy (e.g. 'Team' instead of 'Pod')."""
    term = o.get("terminology") or {}
    return {"project": term.get("project") or "Project", "pod": term.get("pod") or "Pod"}


@mcp.tool()
def list_my_orgs() -> list[dict]:
    """List organizations the authenticated user belongs to, including each org's
    Project/Pod labels (`terminology`) so you can use the org's own wording."""
    return [
        {"slug": o["slug"], "name": o["name"], "role": o["my_role"], "plan": o["plan"],
         "terminology": _org_terms(o)}
        for o in _request("GET", "/api/orgs")
    ]


@mcp.tool()
def list_org_structure(org_slug: str) -> dict:
    """List an org's projects and pods (ids + names), so you can target
    list_pod_resources / create_note at the right scope. Includes the org's
    `terminology` (Project/Pod labels) — mirror those words to the user."""
    org = _request("GET", f"/api/orgs/{org_slug}")
    projects = _request("GET", f"/api/orgs/{org_slug}/projects")
    out = []
    for p in projects:
        pods = _request("GET", f"/api/projects/{p['id']}/pods")
        out.append(
            {
                "project_id": p["id"],
                "name": p["name"],
                "pods": [{"pod_id": x["id"], "name": x["name"], "purpose": x["purpose_tag"]} for x in pods],
            }
        )
    return {"org": org_slug, "terminology": _org_terms(org), "projects": out}


@mcp.tool()
def update_org(
    org_slug: str, name: str = "", description: str = "", project_label: str = "", pod_label: str = ""
) -> dict:
    """Rename an org or relabel its Projects/Pods (the org's "workspace naming").
    Set name/description to rename the org; set project_label and/or pod_label to
    change what "Project"/"Pod" are called org-wide (e.g. "Team"/"Squad").
    Relabelling requires the org's custom-terminology (premium) feature; you must
    be an org ADMIN. Only the fields you pass are changed."""
    body: dict = {}
    if name:
        body["name"] = name
    if description:
        body["description"] = description
    if project_label or pod_label:
        current = _org_terms(_request("GET", f"/api/orgs/{org_slug}"))
        body["terminology"] = {
            "project": project_label or current["project"],
            "pod": pod_label or current["pod"],
        }
    if not body:
        raise SkillShareError("Pass name, description, project_label, or pod_label")
    o = _request("PATCH", f"/api/orgs/{org_slug}", json=body)
    return {"slug": o["slug"], "name": o["name"], "terminology": _org_terms(o)}


# ---- member management (org / project / pod) ----
# Roles everywhere are ADMIN | MEMBER. Exactly one scope (org_slug / project_id /
# pod_id) identifies where to act. Adding by email auto-provisions a brand-new
# teammate; adding at a deeper level cascades the shallower levels as MEMBER.


def _members_path(org_slug: str, project_id: str, pod_id: str) -> tuple[str, str, str]:
    chosen = [(k, v) for k, v in (("org", org_slug), ("project", project_id), ("pod", pod_id)) if v]
    if len(chosen) != 1:
        raise SkillShareError("Specify exactly one of org_slug, project_id, or pod_id")
    kind, value = chosen[0]
    if kind == "org":
        return "org", f"/api/orgs/{value}/members", f"org {value}"
    return kind, f"/api/{kind}s/{value}/members", f"{kind} {value}"


def _norm_role(role: str) -> str:
    return "ADMIN" if (role or "").strip().upper() in ("ADMIN", "OWNER", "LEAD") else "MEMBER"


def _member_summary(m: dict) -> dict:
    u = m.get("user") or {}
    return {"user_id": m["user_id"], "role": m["role"],
            "username": u.get("username"), "display_name": u.get("display_name"), "email": u.get("email")}


@mcp.tool()
def list_members(org_slug: str = "", project_id: str = "", pod_id: str = "") -> list[dict]:
    """List the members (and their roles) of an org, project, or pod. Give
    exactly one of org_slug / project_id / pod_id."""
    _, path, _ = _members_path(org_slug, project_id, pod_id)
    return [_member_summary(m) for m in (_request("GET", path) or [])]


@mcp.tool()
def add_member(email: str, org_slug: str = "", project_id: str = "", pod_id: str = "", role: str = "member") -> dict:
    """Add a teammate by email to an org, project, or pod. Give exactly one of
    org_slug / project_id / pod_id, and role="admin" or "member".

    - If the email has no account yet, one is created and they're emailed a
      temporary password (they set their own on first sign-in).
    - Adding to a POD or PROJECT also makes them a MEMBER of the parent levels
      (a pod ADMIN is still just an org/project MEMBER).
    - For an ORG this sends an invitation (with an accept link).
    """
    kind, path, label = _members_path(org_slug, project_id, pod_id)
    r = _norm_role(role)
    if kind == "org":
        inv = _request("POST", path.replace("/members", "/invite"), json={"email": email, "role": r})
        return {"scope": label, "invited": inv["email"], "role": inv["role"]}
    m = _request("POST", path, json={"email": email, "role": r})
    return {"scope": label, "added": (m.get("user") or {}).get("email", email), "role": m["role"],
            "cascaded": "parent project + org as MEMBER" if kind == "pod" else "org as MEMBER"}


@mcp.tool()
def set_member_role(user_id: str, role: str, org_slug: str = "", project_id: str = "", pod_id: str = "") -> dict:
    """Change a member's role (admin | member) at a scope. Identify the member by
    user_id (from list_members) and give exactly one scope."""
    _, path, label = _members_path(org_slug, project_id, pod_id)
    m = _request("PATCH", f"{path}/{user_id}", json={"role": _norm_role(role)})
    return {"scope": label, "user_id": user_id, "role": m["role"]}


@mcp.tool()
def remove_member(user_id: str, org_slug: str = "", project_id: str = "", pod_id: str = "") -> dict:
    """Remove a member from an org, project, or pod. Identify the member by
    user_id (from list_members) and give exactly one scope."""
    _, path, label = _members_path(org_slug, project_id, pod_id)
    _request("DELETE", f"{path}/{user_id}")
    return {"scope": label, "removed": user_id, "ok": True}


@mcp.tool()
def list_org_resources(org_slug: str, query: str = "", resource_type: str = "") -> list[dict]:
    """List org-level resources (inherited by every project and pod)."""
    params = {"q": query or None, "type": resource_type or None}
    rows = _request("GET", f"/api/orgs/{org_slug}/resources", params={k: v for k, v in params.items() if v})
    return [_summary(r) for r in rows]


@mcp.tool()
def list_pod_resources(pod_id: str, scope: str = "all", query: str = "", resource_type: str = "") -> list[dict]:
    """List a pod's unified resource library — its own resources plus those
    inherited from the project and org (BACKEND_SPEC §5).

    Args:
        pod_id: the pod to query.
        scope: "all" | "pod" | "project" | "org" — filter by origin.
        query: free-text filter.
        resource_type: "SKILL" | "MCP" | "NOTE".
    """
    params = {"scope": scope, "q": query or None, "type": resource_type or None}
    rows = _request("GET", f"/api/pods/{pod_id}/resources", params={k: v for k, v in params.items() if v})
    return [_summary(r) for r in rows]


@mcp.tool()
def search_org(org_slug: str, query: str) -> dict:
    """Search one org across resources, projects, pods, and members
    (the same search the dashboard's ⌘K palette uses)."""
    res = _request("GET", f"/api/orgs/{org_slug}/search", params={"q": query})
    return {
        "resources": [_summary(r) for r in res["resources"]],
        "projects": [{"id": p["id"], "name": p["name"]} for p in res["projects"]],
        "pods": [{"id": p["id"], "name": p["name"]} for p in res["pods"]],
        "members": [{"username": u["username"], "name": u["display_name"]} for u in res["members"]],
    }


# ---------- write (token required) ----------

@mcp.tool()
def create_note(
    title: str,
    content_md: str,
    org_slug: str = "",
    project_id: str = "",
    pod_id: str = "",
    description: str = "",
    tags: list[str] | None = None,
    attachments: list[dict] | None = None,
    visibility: str = "",
) -> dict:
    """Create a NOTE resource — team knowledge in markdown. Target exactly one
    scope: org_slug (whole org), project_id (a project + its pods), or pod_id
    (one pod).

    `visibility` (audience) is who can READ it: "pod" | "project" | "org".
    Omit it to default to the home scope — a pod note is pod-members-only by
    default; widen to "project" or "org" so other pods / the whole org see it.
    Notifications go to whoever the audience is.

    Args:
        title: note title.
        content_md: markdown body.
        org_slug: target org slug for an org-level note.
        project_id: target project id for a project-level note.
        pod_id: target pod id for a pod-level note (most specific wins).
        description: one-line summary shown on cards.
        tags: e.g. ["process", "rag"].
        attachments: typed media, each {kind, url, title?, caption?} where kind
            is "link" | "image" | "video" | "file". Pass public URLs; to attach
            a local file, call upload_file first and use the returned file_url.
        visibility: "pod" | "project" | "org" — the audience (default: home scope).
    """
    scope = _resource_scope(org_slug, project_id, pod_id)
    if visibility:
        scope["visibility"] = visibility.upper()
    r = _request(
        "POST",
        "/api/resources",
        json={
            "type": "NOTE",
            "title": title,
            "description": description,
            "content_md": content_md,
            "tags": tags or [],
            "attachments": attachments or [],
            **scope,
        },
    )
    return _summary(r)


@mcp.tool()
def move_resource(resource_id: str, org_slug: str = "", project_id: str = "", pod_id: str = "", visibility: str = "") -> dict:
    """Move a resource to another home scope (pod ⇆ project ⇆ org). Give exactly
    one destination (org_slug / project_id / pod_id). The audience re-defaults to
    the destination scope unless `visibility` ("pod"|"project"|"org") is given.
    Requires you to be the author or an org admin, and a member at the destination."""
    scope = _resource_scope(org_slug, project_id, pod_id)
    body = {"scope_type": scope["scope_type"], "scope_id": scope["scope_id"]}
    if visibility:
        body["visibility"] = visibility.upper()
    return _summary(_request("POST", f"/api/resources/{resource_id}/move", json=body))


@mcp.tool()
def set_resource_visibility(resource_id: str, visibility: str) -> dict:
    """Change who can read a resource without moving it: visibility is
    "pod" | "project" | "org" (must be allowed by the resource's home scope —
    e.g. an org-scoped resource is always org-wide). Author or org admin only."""
    return _summary(_request("PATCH", f"/api/resources/{resource_id}", json={"visibility": visibility.upper()}))


@mcp.tool()
def upload_file(path: str, kind: str = "file") -> dict:
    """Upload a local file to SkillShare storage and return its public URL.

    Use this when you have a file on disk (e.g. a diagram or image you just
    generated) that you want to attach to a note. Pass the returned file_url
    in create_note's `attachments`.

    Args:
        path: local filesystem path to the file.
        kind: "image" (incl. diagrams), "video", or "file".
    """
    if kind not in ("image", "video", "file"):
        raise SkillShareError("kind must be image, video, or file")
    up = _upload(path, kind)
    return {"file_url": up["file_url"], "filename": up.get("filename"), "size_bytes": up.get("size_bytes")}


@mcp.tool()
def star_resource(resource_id: str) -> dict:
    """Star a resource so it shows on your profile's Stars tab."""
    return _summary(_request("POST", f"/api/resources/{resource_id}/star"))


@mcp.tool()
def pin_resource(resource_id: str) -> dict:
    """Pin a resource to the top of your profile (GitHub-style, max 6). Use this
    to feature standout skills/MCPs/notes — distinct from a star (endorsement)."""
    return _summary(_request("POST", f"/api/resources/{resource_id}/pin"))


@mcp.tool()
def unpin_resource(resource_id: str) -> dict:
    """Remove a resource from your profile's pinned set."""
    return _summary(_request("DELETE", f"/api/resources/{resource_id}/pin"))


@mcp.tool()
def list_pinned() -> list[dict]:
    """List the resources pinned to your profile, in pin order."""
    me = _request("GET", "/api/auth/me")
    return [_summary(r) for r in _request("GET", f"/api/users/{me['username']}/pinned")]


@mcp.tool()
def import_resource(resource_id: str, org_slug: str) -> dict:
    """Copy a public marketplace resource into one of your orgs
    (the dashboard's "Add to my org")."""
    return _summary(_request("POST", f"/api/resources/{resource_id}/import", json={"org_slug": org_slug}))


@mcp.tool()
def submit_feedback(
    message: str,
    rating: int = 0,
    category: str = "general",
    resource_id: str = "",
) -> dict:
    """Send the user's product feedback to the SkillShare team. Use this when the
    user expresses an opinion about SkillShare itself — a bug, a feature idea,
    praise, or a question — or wants to give feedback on a specific resource.
    Always confirm the wording with the user before sending; submit it as they
    said it, don't editorialize.

    Args:
        message: the feedback text (required).
        rating: optional 1-5 satisfaction rating (0 = omit).
        category: "bug" | "idea" | "praise" | "question" | "other" | "general".
        resource_id: set to attach the feedback to a specific resource; leave
            empty for general platform feedback.
    """
    if not message.strip():
        raise SkillShareError("feedback message is required")
    if rating and not 1 <= rating <= 5:
        raise SkillShareError("rating must be between 1 and 5")
    if category not in ("bug", "idea", "praise", "question", "other", "general"):
        raise SkillShareError("invalid category")
    body: dict = {"message": message.strip(), "category": category, "source": "mcp"}
    if rating:
        body["rating"] = rating
    if resource_id:
        body.update({"target_type": "resource", "target_id": resource_id})
    fb = _request("POST", "/api/feedback", json=body)
    return {
        "id": fb["id"],
        "rating": fb.get("rating"),
        "category": fb["category"],
        "target_type": fb["target_type"],
        "target_id": fb.get("target_id"),
        "status": fb["status"],
    }


@mcp.tool()
def check_updates(unread_only: bool = True, limit: int = 30) -> list[dict]:
    """Check the user's notification inbox — resources added or published in
    orgs/projects/pods they belong to (they are never notified of their own
    actions). Each item includes `install_command` (a ready-to-run CLI command,
    e.g. `skillshare pull <id>`) and `resource_id` so you can offer to install
    it via import_resource / get_resource. Surface these to the user proactively.

    Args:
        unread_only: only return unread notifications (default True).
        limit: max items to return.
    """
    params: dict = {"limit": limit}
    if unread_only:
        params["unread_only"] = "true"
    rows = _request("GET", "/api/notifications", params=params)
    # Pop a themed desktop card for anything new (deduped with `skillshare watch`
    # via a shared state file). Best-effort — never break the tool call.
    try:
        from skillshare_cli.notify import announce_inbox
        announce_inbox(rows)
    except Exception:
        pass
    return [
        {
            "id": n["id"],
            "verb": n["verb"],
            "title": n["object_title"],
            "resource_id": n["object_id"] if n["object_type"] == "resource" else None,
            "actor": (n.get("actor") or {}).get("username"),
            "read": n.get("read_at") is not None,
            "created_at": str(n.get("created_at")),
            "install_command": n.get("install_command"),
            "deep_link": n.get("deep_link"),
        }
        for n in rows
    ]


@mcp.tool()
def mark_notifications_read(notification_ids: list[str] | None = None, mark_all: bool = False) -> dict:
    """Mark notifications read. Pass `notification_ids` to clear specific ones,
    or `mark_all=True` to clear the whole inbox."""
    if not mark_all and not notification_ids:
        raise SkillShareError("Provide notification_ids or mark_all=True")
    body = {"all": True} if mark_all else {"ids": notification_ids}
    return _request("POST", "/api/notifications/read", json=body)


def _reconciled(sources: list[str] | None, notes_dir: str, include_dismissed: bool) -> list[dict]:
    cands = scan(sources=sources or list(DETECTORS), notes_dir=notes_dir or None)
    try:
        state = _request("GET", "/api/local-state")
    except SkillShareError:
        state = []
    rows = reconcile(cands, state)
    if not include_dismissed:
        rows = [r for r in rows if r["status"] != "dismissed"]
    return rows


@mcp.tool()
def scan_local(sources: list[str] | None = None, notes_dir: str = "", include_dismissed: bool = False) -> list[dict]:
    """Scan the local machine for skills, MCP servers, and notes that may not be
    on SkillShare yet, and reconcile them with what's already pushed/dismissed.

    Use this to proactively offer to back up / share the user's local artifacts.
    Each candidate includes its `status` (new | pushed | dismissed), a content
    `fingerprint`, the secret-`redaction` findings, and a redacted `preview` of
    exactly what would be uploaded. To contribute one, draft a good title/
    description/tags from the preview and call push_artifact; if the user says no,
    call dismiss_artifact so it isn't suggested again.

    Args:
        sources: limit to detectors (e.g. ["claude-code", "cursor"]); default all.
        notes_dir: also scan this directory's *.md files as notes.
        include_dismissed: include artifacts the user previously dismissed.
    """
    rows = _reconciled(sources, notes_dir, include_dismissed)
    # Pop a card if there are NEW local artifacts worth sharing (deduped via shared state).
    try:
        from skillshare_cli.notify import announce_shareable
        announce_shareable(rows)
    except Exception:
        pass
    out = []
    for r in rows:
        c = r["candidate"]
        payload, findings = redacted_payload(c)
        out.append({
            "fingerprint": r["fingerprint"],
            "status": r["status"],
            "kind": c.kind,
            "name": c.name,
            "source": c.source,
            "title": c.title,
            "description": c.description,
            "path": c.path,
            "resource_id": r["resource_id"],
            "redaction": findings,
            "preview": {k: payload.get(k) for k in ("content_md", "config_json", "server_url") if payload.get(k)},
        })
    return out


@mcp.tool()
def push_artifact(
    fingerprint: str,
    org_slug: str = "",
    project_id: str = "",
    pod_id: str = "",
    title: str = "",
    description: str = "",
    tags: list[str] | None = None,
    visibility: str = "",
) -> dict:
    """Push a local artifact (found via scan_local) to SkillShare. Secrets are
    redacted automatically before upload. Target exactly one scope (org_slug,
    project_id, or pod_id) and optionally override the auto-derived
    title/description/tags with better ones you drafted from the preview.

    Args:
        fingerprint: the candidate's fingerprint from scan_local (prefix ok).
        org_slug: target org for an org-level resource.
        project_id: target project for a project-level resource.
        pod_id: target pod (most specific wins).
        title/description/tags: optional overrides for the resource metadata.
    """
    rows = _reconciled(None, "", include_dismissed=True)
    match = next((r for r in rows if r["fingerprint"].startswith(fingerprint)), None)
    if not match:
        raise SkillShareError(f"No local artifact matching fingerprint {fingerprint}")
    c = match["candidate"]
    payload, findings = redacted_payload(c)
    if title:
        payload["title"] = title
    if description:
        payload["description"] = description
    if tags is not None:
        payload["tags"] = tags
    payload.update(_resource_scope(org_slug, project_id, pod_id))
    if visibility:
        payload["visibility"] = visibility.upper()
    created = _request("POST", "/api/resources", json=payload)
    _request("POST", "/api/local-state", json={
        "fingerprint": match["fingerprint"], "kind": c.kind, "status": "pushed",
        "source": c.source, "resource_id": created["id"], "name": c.name})
    return {"pushed": _summary(created), "redacted_secrets": findings}


@mcp.tool()
def import_from_github(
    url: str,
    org_slug: str = "",
    project_id: str = "",
    pod_id: str = "",
    select: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Import skill(s)/MCP server(s)/note(s) from a PUBLIC GitHub repo URL.

    Detection runs server-side over the GitHub API (no clone): it finds SKILL.md
    files (root, skills/*, .claude/skills/*), server.json / .mcp.json MCP configs,
    or falls back to the README as a note. Secrets in any MCP config are redacted
    before import.

    Workflow: call with dry_run=True first to see what was detected — each item
    has a `fingerprint`, `type`, `title`, `source_path`, and `redaction` findings.
    Then call again with a scope (org_slug, project_id, or pod_id) and optionally
    `select` (a list of fingerprints) to import a subset. Re-importing is
    idempotent: items already present in the scope are skipped, not duplicated.

    Args:
        url: github.com/owner/repo (optionally /tree/<ref>/<subpath>).
        org_slug: target org for an org-level import.
        project_id: target project for a project-level import.
        pod_id: target pod (most specific wins).
        select: fingerprints to import; omit/empty to import everything detected.
        dry_run: only detect and return the candidates; import nothing.
    """
    if dry_run:
        return _request("POST", "/api/resources/github/preview", json={"url": url})
    scope = _resource_scope(org_slug, project_id, pod_id)
    result = _request(
        "POST", "/api/resources/github/import",
        json={"url": url, "select": select or [], **scope},
    )
    return {
        "created": [_summary(r) for r in result.get("created", [])],
        "skipped": result.get("skipped", []),
        "repo": result.get("repo", {}),
    }


@mcp.tool()
def dismiss_artifact(fingerprint: str, kind: str = "skill", source: str = "", name: str = "") -> dict:
    """Remember NOT to recommend a local artifact again (the user declined to
    push it). Synced server-side so the decision carries across machines."""
    return _request("POST", "/api/local-state", json={
        "fingerprint": fingerprint, "kind": kind, "status": "dismissed", "source": source, "name": name})


def main() -> None:
    # Cheap, dependency-free arg handling so a frozen binary can be smoke-tested
    # (the stdio server itself blocks on stdin and has no argparse surface).
    if len(sys.argv) > 1:
        flag = sys.argv[1]
        if flag in ("--version", "-V"):
            try:
                from importlib.metadata import version

                print(f"skillshare-mcp {version('skillshare-mcp')}")
            except Exception:
                print("skillshare-mcp (dev)")
            return
        if flag in ("--help", "-h"):
            print(
                "skillshare-mcp — MCP server for the SkillShare registry.\n"
                "Run with no arguments to start the stdio server (used by your MCP client).\n"
                "  --version   print the version and exit\n"
                "  --help      show this message"
            )
            return
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
