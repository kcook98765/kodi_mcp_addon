# kodi_mcp_addon

Kodi-resident bridge addon packages for the Kodi MCP system.

## Ownership

- `packages/service.kodi_mcp` owns the Kodi HTTP bridge addon.
- `packages/repository.kodi_mcp_dev` owns the private development repository addon.
- `packages/script.kodi_mcp_test` owns a small executable addon used for workflow checks.

## Bridge Surface

`service.kodi_mcp` listens on `0.0.0.0:8765` inside Kodi and exposes:

- `/health`, `/status`, `/runtime/info`
- `/addon/info`, `/addon/ensure-enabled`, `/addon/execute`, `/addon/version-check`
- `/log/tail`, `/log/markers`, `/log/marker`
- `/files/read`, `/debug/addon-db`, `/debug/ping`
- `/mcp/register`, `/mcp/state`, `/repo/stage`

The `/mcp/*` and `/repo/stage` endpoints use the standard bridge envelope expected by `kodi_mcp_server`. If the Kodi addon setting `mcp_token` is configured, callers must send the same value in the `X-Kodi-MCP-Token` header.

## Local Verification

From this repo:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile packages/service.kodi_mcp/http_bridge.py packages/service.kodi_mcp/service.py packages/script.kodi_mcp_test/default.py
```

No GitHub push should happen until the local Kodi workflow has been smoke-tested and the operator explicitly asks to push.
