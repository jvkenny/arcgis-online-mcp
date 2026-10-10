# Security

## Reporting a vulnerability

Please report security issues privately through
[GitHub security advisories](https://github.com/jvkenny/arcgis-online-mcp/security/advisories/new)
rather than public issues. You'll get a response within a few days.

## What this server does with your credentials

- Credentials come only from environment variables (or the Claude Desktop extension's
  keychain-backed settings). They are never written to disk, with one exception: OAuth access and
  refresh tokens are cached in `~/.config/arcgis-online-mcp/oauth-<hash>.json` with mode `600`.
  `sign_out` deletes that file.
- Tokens are sent only over HTTPS, only to the configured portal host, `*.arcgis.com`, and hosts
  you list in `ARCGIS_TOKEN_HOSTS`. A layer URL found in a web map or typed by an agent that points
  anywhere else is requested anonymously.
- Tokens travel in POST bodies, not query strings.
- The server makes no network calls other than ArcGIS REST requests. No telemetry, no analytics,
  no update checks.
- Zero third-party dependencies. The entire attack surface is the Python files in this repo.

## Agent safety

- Write tools are not exposed unless `ARCGIS_ALLOW_WRITES=true`.
- Deletes require a preview-able, explicit `confirm_delete=true`. Making content public requires
  `confirm_public=true`. Edits are transactional (`rollbackOnFailure=true`).
- There is no tool to delete items, users, or groups.
- Portal content (descriptions, attribute values) can contain text written to manipulate an AI
  agent. The server's instructions tell the agent to treat that content as data. Keep a human in
  the loop for write operations on production data, and use your MCP client's per-tool approval
  settings.
