# Kodi MCP Service (service.kodi_mcp)

Kodi-resident HTTP bridge service for MCP development workflows.

## What this addon does (current role)

- Thin HTTP bridge to Kodi (JSON-RPC + builtin helpers)
- Stores MCP server registration state locally (persisted under addon_data)
- Receives a **dev repo zip** from the MCP server and stores it in Kodi-local storage for installation
- Provides a **user-guided** Developer setup flow (opens Kodi’s Install-from-zip UI)
- Installs only the canonical staged `repository.kodi-mcp` bootstrap when the
  zero-argument authenticated bootstrap operation is requested by the MCP server
- Reports bounded read-only readiness for the fixed installed
  `repository.kodi-mcp`, including Kodi-side metadata, checksum, and package probes

It does **not** expose a generic ZIP/path/URL/addon installer or manage source repositories.

## MCP server requirement (for agent use)

- This addon is only the **Kodi-side bridge**.
- To enable agent-driven workflows, you must run the MCP server separately:
  https://github.com/kcook98765/kodi_mcp_server
- The MCP server provides the managed addon workflow, build/publish/stage loop, and the MCP tool interface for agents.
- Without the MCP server, this addon can be used manually via HTTP endpoints, but not for automated agent workflows.

## First-time setup (recommended flow)

1) Install + enable **Kodi MCP Service** (`service.kodi_mcp`)
2) Set the shared token:
   **Kodi → Add-ons → Services → Kodi MCP Service → Configure → Kodi MCP → MCP shared token**
3) Start the MCP server
4) Wait ~5–15 seconds: the MCP server will **auto-register** with the addon and **auto-stage** the dev repo zip
   You should see **Developer status** become ready.
5) In Kodi: **Developer → Developer setup → Install from zip file**

No separate/manual staging step is required for first-time readiness.

## Repo workflow (publish/install/update)

The repo publish/install/update behavior is documented in the **server runbook**:

- https://github.com/kcook98765/kodi_mcp_server/blob/main/project-config/REPO_WORKFLOW_RUNBOOK.md

Key rule:
- A **brand-new addon** published into the repo requires a **one-time manual install** by the user in Kodi UI.
- After the addon has been installed once, updates can be automated via the MCP server.

## Repo Structure

This repo IS the addon. The root directory contains:

- `addon.xml` - Kodi addon manifest
- `service.py` - Main service entry point
- `http_bridge.py` - Local HTTP control surface

## Installation

1. Clone repo to Kodi addon directory or zip and install directly.
2. Or add to Kodi via repository if hosted on a dev repo.
3. Configure the shared token when Kodi is reachable from another host:
   **Kodi → Add-ons → Services → Kodi MCP Service → Configure → Kodi MCP → MCP shared token**
4. Set the same value on the MCP server as `KODI_BRIDGE_TOKEN`.

For split-host deployments, the MCP server should use `KODI_BRIDGE_BASE_URL=http://<kodi-host>:8765`.

## Development

- Edit files at repo root
- Zip repo root and install as addon
- Restart Kodi to load service

### Deterministic release bundle

Release artifacts must be built from exact committed source, not mutable checkout
bytes. From a clean checkout, run:

```bash
python build_bridge_release.py build \
  --commit "$(git rev-parse HEAD)" \
  --output-dir dist
python build_bridge_release.py verify \
  --manifest dist/bridge-bootstrap.json
```

The builder reads blobs from the named Git commit, sorts paths, uses the fixed ZIP
timestamp `1980-01-01 00:00:00`, normalizes regular-file permissions to `0644`,
and uses `ZIP_STORED`. Checkout location, mtimes, timezone, umask, and file creation
order therefore do not affect the result. Dirty working trees fail closed.

The source fingerprint is SHA-256 over each committed regular file in canonical
POSIX path order, feeding `path`, a NUL byte, file bytes, and another NUL byte.
Generated `build_manifest.json`, ZIP metadata, VCS data, caches, and runtime files
are not part of that source fingerprint. Symlinks, special files, unsafe paths,
case-fold collisions, and duplicate normalized paths are rejected.

The ZIP's `build_manifest.json` contains only its builder format, exact source
commit, and source fingerprint. The external deterministic
`bridge-bootstrap.json` binds those values to the addon ID/version and final ZIP
filename, SHA-256, and size. Keeping the final ZIP hash outside the ZIP avoids a
self-referential hash cycle. Schema version 1 remains compatible with the current
server bootstrap consumer; the additional size and builder-format fields are
ignored by older schema-1 consumers.

The current source version remains `0.2.39`. Deterministic artifacts produced
while validating this builder are test evidence only and must not be published as
a replacement `0.2.39` build. The next formal corrected release is expected to
bump the addon to `0.2.40`; this repository does not claim that version is released.

## Files

- `addon.xml` - Addon manifest, points to `service.py` as entry point
- `service.py` - Runs HTTP bridge on port 8765
- `http_bridge.py` - Implements HTTP bridge with endpoints:
  - `/health` - Health check
  - `/status` - Version and runtime info
  - `/runtime/info` - Addon paths and configuration
  - `/capabilities`, `/control/capabilities` - Bridge capabilities
  - `/gui/action` - Basic GUI navigation actions
  - `/gui/state` - Current Kodi GUI/window/player state
  - `/gui/screenshot` - Captures a Kodi screenshot
  - `/debug/ping` - Liveness check with timestamp
  - `/addon/*` - Addon management and version checking
  - `/log/*` - Log inspection and marker logging
  - `/files/read` - Read allowed files
  - `/debug/addon-db` - Inspect addons database

### Auth / configuration (operator)

- Shared token is configured in Kodi addon settings:
  - **service.kodi_mcp → mcp_token**
- Protected endpoints require header:
  - **`X-Kodi-MCP-Token: <token>`**
- If `mcp_token` is blank, protected endpoints are accepted without a token for local development.
- If `mcp_token` is set, all non-health/status/runtime/capabilities endpoints require the token.

### Milestone A endpoints

Protected (require `X-Kodi-MCP-Token`):
- `POST /mcp/register` — Register/refresh MCP server identity + TTL
- `GET /mcp/state` — Read persisted registration + staging state
- `POST /repo/stage` — Upload/stage dev repo zip to Kodi-local path
- `POST /repo/bootstrap/install` — Install only the validated canonical
  `repository.kodi-mcp` ZIP from the fixed bridge-owned staging slot; no caller
  arguments are accepted. The authenticated server supplies version and SHA-256
  staging metadata, and the bridge requires both to match the fixed-ID ZIP.
- `GET /repo/readiness` — Inspect only installed `repository.kodi-mcp` and probe
  its configured repository URLs from Kodi; accepts no query arguments.
- `POST /gui/action` — Send `up`, `down`, `left`, `right`, `select`, `back`, `home`, `context`, or `info`
- `GET /gui/state` — Return compact Kodi GUI/window/player state for automated verification
- `GET /gui/screenshot` — Capture a PNG screenshot under addon profile data, optionally with base64 image content

Unprotected:
- `GET /health`, `GET /status`, `GET /runtime/info`, `GET /capabilities`, `GET /control/capabilities`

When the MCP server stores screenshots server-side, remote clients receive a server `/screenshots/<id>.png` URL instead of a large inline image by default.

### Repository bootstrap flow

1) MCP server automatically registers and stages the dev repo zip (refreshes `POST /mcp/register` and `POST /repo/stage` as needed)
2) The zero-argument public MCP repository-bootstrap operation invokes the
   authenticated bounded install endpoint.
3) The bridge verifies the fixed staging slot, fixed addon ID, exact ZIP layout,
   metadata/ZIP version agreement, and SHA-256 before installing or upgrading.

The bridge contains no independent canonical repository version. It accepts the
version bound by authenticated staging metadata only when that version exactly
matches `addon.xml`; newer installed versions are never downgraded.
