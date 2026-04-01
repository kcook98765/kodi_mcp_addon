# Kodi MCP Service (service.kodi_mcp)

Kodi-resident HTTP bridge service for MCP development workflows.

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
