# Kodi MCP Service (service.kodi_mcp)

Kodi-resident HTTP bridge service for MCP development workflows.

## What this addon does (current role)

- Thin HTTP bridge to Kodi (JSON-RPC + builtin helpers)
- Stores MCP server registration state locally (persisted under addon_data)
- Can stage a **dev repo zip** into Kodi-local storage for installation
- Provides a **user-guided** Developer setup flow (opens Kodi’s Install-from-zip UI)

It does **not** silently install zips or manage source repositories.

## MCP server requirement (for agent use)

- This addon is only the **Kodi-side bridge**.
- To enable agent-driven workflows, you must run the MCP server separately:
  https://github.com/kcook98765/kodi_mcp_server
- The MCP server provides the managed addon workflow, build/publish/stage loop, and the MCP tool interface for agents.
- Without the MCP server, this addon can be used manually via HTTP endpoints, but not for automated agent workflows.

## Repo Structure

This repo IS the addon. The root directory contains:

- `addon.xml` - Kodi addon manifest
- `service.py` - Main service entry point
- `http_bridge.py` - Local HTTP control surface

## Installation

1. Clone repo to Kodi addon directory or zip and install directly
2. Or add to Kodi via repository (if hosted on dev repo)

## Development

- Edit files at repo root
- Zip repo root and install as addon
- Restart Kodi to load service

## Files

- `addon.xml` - Version 0.2.15, points to `service.py` as entry point
- `service.py` - Runs HTTP bridge on port 8765
- `http_bridge.py` - Implements HTTP bridge with endpoints:
  - `/health` - Health check
  - `/status` - Version and runtime info
  - `/runtime/info` - Addon paths and configuration
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

### Milestone A endpoints

Protected (require `X-Kodi-MCP-Token`):
- `POST /mcp/register` — Register/refresh MCP server identity + TTL
- `GET /mcp/state` — Read persisted registration + staging state
- `POST /repo/stage` — Upload/stage dev repo zip to Kodi-local path

Unprotected:
- `POST /repo/refresh` — Ask Kodi to refresh repositories (best-effort)

### Developer setup flow (user-guided)

1) MCP server registers with the addon (`POST /mcp/register`)
2) MCP server stages a repo zip to Kodi-local storage (`POST /repo/stage`)
3) In Kodi, the user opens:
   **Kodi → Add-ons → Services → Kodi MCP Service → Configure**
4) Then:
   **Developer → Developer setup**
5) Kodi opens **Install from zip file**
6) The user must **manually browse** to the staged `special://...` path shown and select the staged repo zip.
