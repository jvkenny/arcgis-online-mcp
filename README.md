# arcgis-online-mcp

**Give Claude (or any MCP agent) your ArcGIS Online or ArcGIS Enterprise portal.**
Search content, read web maps, inspect layers, query and aggregate features, geocode, and, if you
turn it on, edit features, publish layers, build web maps, and manage sharing.

[![tests](https://github.com/jvkenny/arcgis-online-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jvkenny/arcgis-online-mcp/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![license](https://img.shields.io/badge/license-MIT-green)

- **Zero dependencies.** Pure Python standard library. Nothing to `pip install`, nothing to audit but this repo.
- **Read-only by default.** Write tools don't exist until you set `ARCGIS_ALLOW_WRITES=true`.
- **Works with your login.** OAuth (including SAML/enterprise logins), API keys, built-in accounts, or anonymous.
- **ArcGIS Online and Enterprise.** Point it at any portal URL.
- **Tested against the live service** on Linux, macOS, and Windows, plus a weekly run to catch REST API drift.

```text
You:    Which 5 Illinois counties have the most census tracts, and how many people live in them?
Claude: search_content → describe_layer → summarize_features(group_by=["COUNTY_FIPS"], …)
        Cook County (031) leads with 1,331 tracts and 5.26M people, then DuPage (043)
        with 219 tracts and 931K, Will (197) with 172 …
```

---

## Install

You need **Python 3.9+** (`python3 --version`). Pick your client:

### Claude Desktop (Chat and Cowork): one click

1. Download **[arcgis-online-mcp.mcpb](https://github.com/jvkenny/arcgis-online-mcp/releases/latest/download/arcgis-online-mcp.mcpb)**.
2. Double-click it (or **Settings → Extensions → Install Extension…**).
3. Fill in the settings. Leave everything blank to try it on public data first.

### Claude Code: one command

```bash
claude mcp add arcgis -- uvx --from git+https://github.com/jvkenny/arcgis-online-mcp arcgis-online-mcp
```

Add credentials with `-e`, e.g. `claude mcp add arcgis -e ARCGIS_CLIENT_ID=abc123 -- uvx …`.
Add `-s user` to make it available in every project.

No `uv`? Clone instead:

```bash
git clone https://github.com/jvkenny/arcgis-online-mcp && cd arcgis-online-mcp && claude
```

The repo's `.mcp.json` registers the server automatically. Approve it when Claude Code asks.

### Any other MCP client (Codex, Cursor, VS Code, Windsurf, Goose…)

It's a standard stdio server. The command is either
`uvx --from git+https://github.com/jvkenny/arcgis-online-mcp arcgis-online-mcp` or
`python3 /path/to/arcgis-online-mcp/server.py`.

<details>
<summary>Claude Desktop JSON config (instead of the .mcpb)</summary>

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "arcgis": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jvkenny/arcgis-online-mcp", "arcgis-online-mcp"],
      "env": { "ARCGIS_CLIENT_ID": "your-client-id" }
    }
  }
}
```
</details>

<details>
<summary>OpenAI Codex (~/.codex/config.toml)</summary>

```toml
[mcp_servers.arcgis]
command = "uvx"
args = ["--from", "git+https://github.com/jvkenny/arcgis-online-mcp", "arcgis-online-mcp"]
env = { ARCGIS_CLIENT_ID = "your-client-id" }
```
</details>

<details>
<summary>Cursor (~/.cursor/mcp.json) / VS Code (.vscode/mcp.json)</summary>

```json
{ "mcpServers": { "arcgis": { "command": "uvx",
  "args": ["--from", "git+https://github.com/jvkenny/arcgis-online-mcp", "arcgis-online-mcp"] } } }
```

VS Code uses `"servers"` instead of `"mcpServers"` and wants `"type": "stdio"`.
</details>

Check your setup from a terminal at any time:

```bash
python3 server.py --check
```

---

## Sign in

Pick one. If several are set, the first match in this table wins.

| Method | Env vars | Best for | Notes |
|---|---|---|---|
| Existing token | `ARCGIS_TOKEN` | Scripts that already have one | Expires; no refresh |
| **API key** | `ARCGIS_API_KEY` | Read-only automation, shared bots | Scoped by the key's privileges; can't own content |
| Built-in account | `ARCGIS_USERNAME` + `ARCGIS_PASSWORD` | Personal built-in ArcGIS logins | **Doesn't work with SAML/SSO** |
| **OAuth (recommended for orgs)** | `ARCGIS_CLIENT_ID` | People in an org, including SAML/SSO | Browser sign-in; tokens refresh automatically |
| Anonymous | *(nothing)* | Public data, Living Atlas, trying it out | Search, query, and geocode public content |

Set `ARCGIS_PORTAL_URL` for ArcGIS Enterprise (e.g. `https://gis.example.gov/portal`). It defaults to `https://www.arcgis.com`.

### Setting up OAuth (once per org, about 2 minutes)

1. In ArcGIS Online: **Content → New item → Developer credentials → OAuth 2.0 credentials**.
2. Add the redirect URL **`http://127.0.0.1:7071/callback`**.
3. Save, then copy the **Client ID**. It isn't a secret, so you can share it with your whole team.
4. Set `ARCGIS_CLIENT_ID` to that value. In chat, ask Claude to **"sign in to ArcGIS"**. Your browser
   opens the normal ArcGIS login page (SSO included), and you're done.

Tokens are cached in `~/.config/arcgis-online-mcp/` (file mode `600`) and refresh for two weeks.
Ask Claude to "sign out of ArcGIS" to delete them. Use `ARCGIS_REDIRECT_URI` to change the port.

> **Keep secrets out of config files.** Use your client's secret storage (the `.mcpb` stores the API
> key and password in the OS keychain), or launch with a secrets manager, e.g.
> `op run --env-file=.env -- claude`.

---

## Tools

**Read (always on)**

| Tool | What it does |
|---|---|
| `whoami` | Portal, auth mode, user, role, and key privileges (can publish? can share public?) |
| `search_content` | Search items with ArcGIS search syntax plus type/owner/group/org/bbox filters |
| `get_item` | Item metadata; layers inside a web map (URLs, filters); layers and tables of a service |
| `get_item_data` | Raw JSON behind a web map, app, or dashboard |
| `list_user_content` | A user's folders and items |
| `search_groups` | Your groups, or search all groups |
| `describe_service` | A Feature/Map/Image service's layers, tables, capabilities, and spatial reference |
| `describe_layer` | Fields, types, coded-value domains, subtypes, relationships, record count, WGS84 extent |
| `query_features` | SQL `where` + bbox/near-point filters, paging past server limits, GeoJSON output, `save_to` .geojson/.csv for up to 100k rows |
| `summarize_features` | Server-side count/sum/avg/min/max/stddev with GROUP BY; also lists unique values |
| `geocode` / `reverse_geocode` | ArcGIS World Geocoder, or your portal's own geocoder |

**Write (only with `ARCGIS_ALLOW_WRITES=true`)**

| Tool | What it does | Guardrail |
|---|---|---|
| `apply_edits` | Add/update/delete features in one transaction | `dry_run` preview; deletes need `confirm_delete`; ≤1000 per call; rolls back on any failure |
| `create_hosted_layer` | Publish GeoJSON as a hosted feature layer | Created **private** |
| `create_webmap` | Build a web map from layers on a basemap; returns a Map Viewer link | Created **private** |
| `update_item` | Title, summary, description, tags, credits, terms of use | |
| `share_item` | Private / org / public, and group sharing | Public needs `confirm_public` |

There is **no delete-item tool**, on purpose. Deleting portal content stays a human decision.

**OAuth only:** `sign_in`, `sign_out`.

---

## Safety model

What a GIS manager will want to know before approving this:

- **Read-only unless you opt in.** Without `ARCGIS_ALLOW_WRITES=true`, the write tools are never
  listed to the agent at all. It can't call what it can't see.
- **It can only do what your account can do.** Every request uses your token, so portal roles,
  privileges, and sharing rules still apply.
- **Your token only goes to ArcGIS.** It's sent only over HTTPS to your portal and `*.arcgis.com`.
  If a web map points at a third-party server, the token isn't sent there. Federated servers can be
  allow-listed with `ARCGIS_TOKEN_HOSTS`.
- **Tokens go in POST bodies**, never in URLs, so they don't end up in server logs.
- **No telemetry, no third parties, no dependencies.** The only network calls are to your portal and
  its ArcGIS services.
- **Destructive edits take two steps.** Deletes are previewed and need explicit confirmation.
  Every `apply_edits` call is all-or-nothing (`rollbackOnFailure`).
- **Prompt-injection aware.** The server tells the agent that item descriptions and attribute values
  are third-party data, not instructions.
- **Costs.** Geocoding with an org account or API key may consume ArcGIS credits, the same as it
  does in the ArcGIS UI. Querying your own hosted layers doesn't.

Found a security issue? See [SECURITY.md](SECURITY.md).

---

## Things to ask

- *"What's in our org that mentions stormwater? Which layers were edited in the last 30 days?"*
- *"Open the 'Parks Master Plan' web map and tell me every layer, where it comes from, and its filters."*
- *"How many hydrants are within 500 m of 123 Main St, and what's their status breakdown?"*
- *"Export all parcels in this bounding box to parcels.geojson."*
- *"List every distinct value of ZONE_CLASS and how many parcels have each."*
- *"Geocode these 20 addresses, publish them as a hosted layer, and make me a web map of them."* (writes on)

**Tips for agents and the people prompting them:** call `describe_layer` before writing `where`
clauses so field names and coded values are real. Prefer `summarize_features` over pulling rows to
count things. Use `save_to` for anything big.

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `ARCGIS_PORTAL_URL` | `https://www.arcgis.com` | ArcGIS Online, or an Enterprise portal URL |
| `ARCGIS_CLIENT_ID` | | OAuth app client ID (enables `sign_in`) |
| `ARCGIS_REDIRECT_URI` | `http://127.0.0.1:7071/callback` | Must match the OAuth app's redirect URL |
| `ARCGIS_API_KEY` | | API key / developer credential |
| `ARCGIS_USERNAME` / `ARCGIS_PASSWORD` | | Built-in account |
| `ARCGIS_TOKEN` | | Use an existing token as-is |
| `ARCGIS_ALLOW_WRITES` | `false` | Expose the write tools |
| `ARCGIS_TOKEN_HOSTS` | | Extra hosts trusted to receive the token (comma-separated) |
| `ARCGIS_GEOCODE_URL` | portal default | Use a specific geocode service |
| `ARCGIS_TIMEOUT` | `60` | HTTP timeout in seconds |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` | macOS python.org installs: run *Install Certificates.command* in `/Applications/Python 3.x/`, or `pip install certifi` |
| `python3` not found (Windows) | Install Python from python.org with "Add to PATH" checked. The `.mcpb` uses `python` on Windows. |
| `Token Required (499)` | The content isn't public. Configure auth, or call `sign_in` if using OAuth. |
| `Token not sent: … isn't a trusted host` | A federated ArcGIS Server: add its hostname to `ARCGIS_TOKEN_HOSTS` |
| SAML login fails with username/password | Expected: SAML needs OAuth. See [Setting up OAuth](#setting-up-oauth-once-per-org-about-2-minutes) |
| `Unknown field(s)` | The error lists the real field names. The agent should retry with one of those. |

## Development

```bash
python3 -m unittest discover -s tests -v        # offline + live (public data, no account)
ARCGIS_SKIP_LIVE=1 python3 -m unittest discover -s tests
npx @anthropic-ai/mcpb pack . dist/arcgis-online-mcp.mcpb   # build the Desktop extension
```

The code layout is small on purpose: `client.py` (HTTP + auth), `tools.py` (every tool, one
decorator each), `server.py` (MCP stdio transport), `geometry.py` (Esri JSON ↔ GeoJSON). Adding a
tool is one decorated function. PRs are welcome, and new tools should come with a live test against
public data.

---

Independent open-source project. Not affiliated with, endorsed by, or supported by Esri.
ArcGIS and ArcGIS Online are trademarks of Esri. MIT licensed.
