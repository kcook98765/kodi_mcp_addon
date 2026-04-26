# kodi_mcp_addon

Purpose:
- Kodi addon-side code for the Kodi MCP system.
- Owns the Kodi-resident packages under `packages/`.

Scope:
- service.kodi_mcp
- script.kodi_mcp_test
- repository.kodi_mcp_dev

Do:
- make changes only inside the addon project files unless explicitly instructed
- keep addon package metadata and code coherent
- preserve Kodi addon structure

Do not:
- modify MCP server code here
- assume server repo files are available
- place local virtualenvs in the repo

Key paths:
- working project tree: `project/`
- addon packages: `project/packages/`
- protocol examples: `project/protocol/`

Current bridge contract:
- `service.kodi_mcp` must provide `/health`, `/status`, `/runtime/info`, log helpers, addon helpers, and Milestone A endpoints `/mcp/register`, `/mcp/state`, and `/repo/stage`.
- If `mcp_token` is configured in Kodi addon settings, server calls must include `X-Kodi-MCP-Token`.
- The MCP server repo remains separate; this repo should only contain Kodi-side addon code and static tests for that code.
