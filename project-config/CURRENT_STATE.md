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
- `packages/script.kodi_mcp_setup`: user-facing setup helper for initial MCP server repository onboarding.
- `packages/repository.kodi_mcp_dev`: private development repository addon.
- `packages/script.kodi_mcp_test`: small test addon for deployment/update checks.

## User-Facing Setup Addon

`script.kodi_mcp_setup` is the preferred first-time user flow for connecting Kodi to the MCP server repository without involving an agent.

Current setup behavior:

- Reads the MCP server URL from setup addon settings or from the bridge `/mcp/state` registration when available.
- Checks server `/health` and `/repo/info`.
- Downloads the reported `repository_addon_zip` as `repository.kodi-mcp-latest.zip` under `~/Downloads/Kodi MCP` when available, with setup addon profile data as a fallback.
- Opens Kodi's Add-on browser so the user can choose **Install from zip file** and select the prepared repository add-on zip.
- Leaves Kodi security/install prompts under user control.
- Live Kodi install testing verified `script.kodi_mcp_setup` version `0.1.1` can be installed/enabled from zip, prepare the repository zip, open the native install-from-zip browser, and show labeled setup settings.

## Current Bridge Capabilities

`service.kodi_mcp` version `0.2.17` exposes the basic bridge/debug endpoints plus the Milestone A endpoints expected by `kodi_mcp_server`:

- `POST /mcp/register`
- `GET /mcp/state`
- `POST /repo/stage`
- `GET /capabilities`
- `GET /control/capabilities`
- `POST /gui/action`
- `GET /gui/screenshot`

Token behavior:

- If the Kodi addon setting `mcp_token` is empty, bridge requests are accepted without a token.
- If `mcp_token` is set, all non-health/status/capabilities endpoints require the same value in `X-Kodi-MCP-Token`, including GUI screenshots/actions, logs, addon execution, uploads, `/mcp/state`, and `/repo/stage`.

Repo staging behavior:

- `/repo/stage` accepts a zip upload, validates optional `X-Content-SHA256`, and stores the zip under the addon's profile data directory.
- `/mcp/state` reports whether registration is present, whether registration is stale, whether a staged repo zip exists, whether developer setup is available, and an install hint that points to **Install from repository**. The staged `dev-repo.zip` is repository content, not an installable Kodi add-on zip.
- Staged repo zip metadata is rehydrated after service restart when the default staged zip still exists.
- GUI actions support `up`, `down`, `left`, `right`, `select`, `back`, `home`, `context`, and `info`.
- Screenshot capture writes PNG files under addon profile data and can return base64 PNG data for agent vision use.

## Verification

Run from the repo root:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile packages/service.kodi_mcp/http_bridge.py packages/service.kodi_mcp/service.py packages/script.kodi_mcp_setup/default.py packages/script.kodi_mcp_test/default.py scripts/build_addon.py scripts/build_service_addon.py
python3 scripts/build_service_addon.py
python3 scripts/build_addon.py script.kodi_mcp_setup
```

For live validation, use the Kodi agent stack's existing host-control workflow after packaging/installing this addon in Kodi.

## Live Smoke Result

Completed after installing the freshly built `service.kodi_mcp-0.2.16.zip` into local Kodi and restarting Kodi:

- `/health`: ok
- `/status`: reports `service.kodi_mcp` `0.2.16`
- `/capabilities` and `/control/capabilities`: ok
- `/mcp/state`: ok, includes registration, staged repo zip state, `dev_setup_available=true`, and install hint
- MCP managed-addon smoke with `script.kodi_mcp_test`:
  - package/upload/publish succeeded
  - repo staging via `/repo/stage` succeeded
  - initial first-install gate was cleared through Kodi UI
  - post-initial managed apply updated `script.kodi_mcp_test` to repo version `0.0.9`
- Attempted `InstallAddon(script.kodi_mcp_test)` through the bridge before first install; Kodi accepted the builtin request but the addon remained uninstalled after polling, confirming the first install still requires Kodi UI.
- `mcp_token` settings metadata now includes explicit `level`, `allowempty`, and edit-control heading metadata to avoid Kodi default-value warnings.
- Added and live-smoked GUI bridge helpers:
  - `POST /gui/action` with `down` and `back`: ok
  - `GET /gui/screenshot`: ok, returned a non-empty PNG under addon profile screenshots
- Fixed binary reads for screenshots and staged repo zip rehydration; Kodi's `xbmcvfs.File(..., "rb")` attempted UTF-8 decoding for binary data.
- Source now builds `service.kodi_mcp-0.2.17.zip` with stricter token enforcement for non-health/status/capabilities endpoints; install/live smoke this package before pushing release notes.

## Future TODO

- Keep addon version, docs, and smoke-test notes aligned for each bridge behavior change.
- Consider adding a custom setup window later if Kodi's native Add-on browser still requires too many clicks.
- Confirm repository URLs in `repository.kodi_mcp_dev` match the target server host before release.
- Keep local env files, zips, logs, caches, and backup files out of Git.
