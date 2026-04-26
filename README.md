# kodi_mcp_addon

Kodi-resident bridge addon packages for the Kodi MCP system.

## Ownership

- `packages/service.kodi_mcp` owns the Kodi HTTP bridge addon.
- `packages/repository.kodi_mcp_dev` owns the private development repository addon.
- `packages/script.kodi_mcp_test` owns a small executable addon used for workflow checks.

## Bridge Surface

`service.kodi_mcp` listens on `0.0.0.0:8765` inside Kodi and exposes:

- `/health`, `/status`, `/runtime/info`
- `/capabilities`, `/control/capabilities`
- `/gui/action`, `/gui/screenshot`
- `/addon/info`, `/addon/ensure-enabled`, `/addon/execute`, `/addon/version-check`
- `/log/tail`, `/log/markers`, `/log/marker`
- `/files/read`, `/debug/addon-db`, `/debug/ping`
- `/mcp/register`, `/mcp/state`, `/repo/stage`

The `/mcp/*` and `/repo/stage` endpoints use the standard bridge envelope expected by `kodi_mcp_server`. If the Kodi addon setting `mcp_token` is configured, callers must send the same value in the `X-Kodi-MCP-Token` header.
`/mcp/state` includes staged repo archive state when a repo zip has been staged. The staged `dev-repo.zip` is repository content for the server/bridge refresh loop, not an installable Kodi add-on zip; first install of target add-ons should use **Add-ons → Install from repository → Kodi MCP Repository** after `repository.kodi-mcp` is installed once.
`/gui/action` supports `up`, `down`, `left`, `right`, `select`, `back`, `home`, `context`, and `info`.
`/gui/screenshot` captures a Kodi screenshot into the addon's profile data directory and can optionally include base64 PNG data.

## Local Verification

From this repo:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile packages/service.kodi_mcp/http_bridge.py packages/service.kodi_mcp/service.py packages/script.kodi_mcp_test/default.py scripts/build_service_addon.py
python3 scripts/build_service_addon.py
```

No GitHub push should happen until the local Kodi workflow has been smoke-tested and the operator explicitly asks to push.
