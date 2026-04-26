# kodi_mcp_addon - Current State

**Last updated:** 2026-04-26

## Summary

This repo owns the Kodi-resident addon packages for the Kodi MCP stack.

- Repo path: `/srv/openclaw-projects/kodi_mcp_addon/workspace/project`
- Git remote: `git@github.com:kcook98765/kodi_mcp_addon.git`
- Work branch for this review: `review/kodi-mcp-addon-hygiene-20260425`
- Do not push until explicitly requested.

## Packages

- `packages/service.kodi_mcp`: Kodi HTTP bridge service addon.
- `packages/repository.kodi_mcp_dev`: private development repository addon.
- `packages/script.kodi_mcp_test`: small test addon for deployment/update checks.

## Current Bridge Capabilities

`service.kodi_mcp` version `0.2.16` exposes the basic bridge/debug endpoints plus the Milestone A endpoints expected by `kodi_mcp_server`:

- `POST /mcp/register`
- `GET /mcp/state`
- `POST /repo/stage`

Token behavior:

- If the Kodi addon setting `mcp_token` is empty, bridge requests are accepted without a token.
- If `mcp_token` is set, callers must send the same value in `X-Kodi-MCP-Token`.

Repo staging behavior:

- `/repo/stage` accepts a zip upload, validates optional `X-Content-SHA256`, and stores the zip under the addon's profile data directory.
- `/mcp/state` reports whether registration is present, whether registration is stale, whether a staged repo zip exists, and whether developer setup is available.

## Verification

Run from the repo root:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile packages/service.kodi_mcp/http_bridge.py packages/service.kodi_mcp/service.py packages/script.kodi_mcp_test/default.py
```

For live validation, use the Kodi agent stack's existing host-control workflow after packaging/installing this addon in Kodi.

## Future TODO

- Smoke-test `service.kodi_mcp` `0.2.16` in Kodi with the MCP server's managed-addon flow.
- Keep addon version, docs, and smoke-test notes aligned for each bridge behavior change.
- Confirm repository URLs in `repository.kodi_mcp_dev` match the target server host before release.
- Keep local env files, zips, logs, caches, and backup files out of Git.
