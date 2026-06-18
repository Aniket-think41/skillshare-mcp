# SkillShare MCP Server

[Model Context Protocol](https://modelcontextprotocol.io) server for the [SkillShare](https://skillshare.think41.com) registry.

Lets AI agents use the registry directly: search the marketplace, read SKILL.md / MCP configs / notes, browse your org's pods, create notes, star, import — over the stdio transport.

## Install

```bash
pip install skillshare-cli    # shared auth + local-scan core
pip install skillshare-mcp    # MCP server
```

Or download a standalone binary from the [releases page](https://github.com/Aniket-think41/skillshare-mcp/releases):

```bash
curl -L https://github.com/Aniket-think41/skillshare-mcp/releases/latest/download/skillshare-mcp-linux-amd64 -o skillshare-mcp
chmod +x skillshare-mcp
sudo mv skillshare-mcp /usr/local/bin/skillshare-mcp
```

## Auth

Set two environment variables (or run `skillshare login` from the CLI first):

```bash
export SKILLSHARE_API_URL=https://skillshare-backend-1081098542602.us-central1.run.app
export SKILLSHARE_TOKEN=skst_...     # optional — marketplace tools work without it
```

The token is a SkillShare **Personal Access Token** (mint one in the dashboard, or from `~/.config/skillshare/credentials.json` after running `skillshare login`).

## Connect to Claude Code

```bash
claude mcp add skillshare \
  --env SKILLSHARE_API_URL=https://skillshare-backend-1081098542602.us-central1.run.app \
  -- skillshare-mcp
```

## Connect to Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "skillshare": {
      "command": "skillshare-mcp",
      "env": {
        "SKILLSHARE_API_URL": "https://skillshare-backend-1081098542602.us-central1.run.app"
      }
    }
  }
}
```

## Tools

| Tool | Auth | Description |
| ---- | ---- | ----------- |
| `search_marketplace(query, resource_type, tag, sort)` | – | search public skills / MCPs / notes |
| `get_resource(resource_id)` | –/✓ | full content: SKILL.md, MCP url + config JSON, note body, attachments |
| `get_publisher(username)` | – | publisher profile + their public resources |
| `login(client_name)` | – | start browser sign-in (device flow) |
| `complete_login(wait_seconds)` | – | finish login after browser approval |
| `setup_statusline(scope, remove, force)` | – | show install/push counts in Claude Code's status bar |
| `whoami()` | ✓ | the authenticated user |
| `list_my_orgs()` | ✓ | orgs + role + plan |
| `list_org_structure(org_slug)` | ✓ | projects → pods map (ids for targeting) |
| `list_org_resources(org_slug, query, resource_type)` | ✓ | org-level resources |
| `list_pod_resources(pod_id, scope, query, resource_type)` | ✓ | pod library incl. inherited |
| `search_org(org_slug, query)` | ✓ | ⌘K-style search: resources/projects/pods/members |
| `create_note(title, content_md, org_slug \| pod_id, …, attachments)` | ✓ | write a NOTE |
| `upload_file(path, kind)` | ✓ | store a local file → returns URL for attachments |
| `star_resource(resource_id)` | ✓ | star |
| `pin_resource(resource_id)` | ✓ | pin to profile |
| `unpin_resource(resource_id)` | ✓ | remove from pinned set |
| `list_pinned()` | ✓ | list pinned resources |
| `import_resource(resource_id, org_slug)` | ✓ | "Add to my org" |
| `submit_feedback(message, rating, category, resource_id)` | ✓ | send product feedback |
| `check_updates(unread_only, limit)` | ✓ | inbox: resources added/published in your scopes |
| `mark_notifications_read(notification_ids, mark_all)` | ✓ | clear inbox items |
| `scan_local(sources, notes_dir, include_dismissed)` | ✓ | find local skills/MCP/notes with status + redacted preview |
| `push_artifact(fingerprint, org_slug \| pod_id, title?, …)` | ✓ | push a scanned artifact (secrets redacted) |
| `import_from_github(url, org_slug \| pod_id, select, dry_run)` | ✓ | import from a public GitHub repo |
| `dismiss_artifact(fingerprint, kind, source, name)` | ✓ | remember not to recommend it again |
| `follow_publisher(username)` | ✓ | follow a publisher |
| `unfollow_publisher(username)` | ✓ | unfollow a publisher |
| `follow_org(org_slug)` | ✓ | follow (watch) an org |
| `unfollow_org(org_slug)` | ✓ | unfollow an org |

The server instructions tell the agent to call `check_updates` **proactively at the start of a conversation** and surface anything new before other work.

## Configuration

- `SKILLSHARE_API_URL` — backend base URL (default: `https://skillshare-backend-1081098542602.us-central1.run.app`)
- `SKILLSHARE_TOKEN` — bearer token (overrides stored credentials)

## License

MIT
