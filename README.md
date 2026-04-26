# kodi_mcp_addon

Kodi-resident bridge addon packages for the Kodi MCP system.

## Ownership

- `packages/service.kodi_mcp` owns the Kodi HTTP bridge addon.
- `packages/script.kodi_mcp_setup` owns the user-facing setup helper.
- `packages/repository.kodi_mcp_dev` owns the private development repository addon.
- `packages/script.kodi_mcp_test` owns a small executable addon used for workflow checks.

## User Onboarding

Initial MCP server repository setup is handled inside Kodi by `script.kodi_mcp_setup`; agents are not required for this flow.

Install prerequisites:

- Kodi is running and can install add-ons from zip.
- The MCP server is reachable from the Kodi host, for example `http://server.local:8010`.
- If the bridge shared token is configured, the same value must be set in the MCP server as `KODI_BRIDGE_TOKEN`.

User flow:

1. Build or download `service.kodi_mcp-<version>.zip`.
2. In Kodi, install the service zip with **Add-ons → Install from zip file**.
3. Enable `service.kodi_mcp`.
4. Configure **Kodi MCP → MCP shared token** if the bridge should reject unauthenticated control requests.
5. Start the MCP server with `KODI_BRIDGE_BASE_URL=http://<kodi-host>:8765` and matching `KODI_BRIDGE_TOKEN` when a token is set.
6. Build or download `script.kodi_mcp_setup-<version>.zip`, install it in Kodi, and launch **Kodi MCP Setup**.
7. Confirm or enter the MCP server URL.
8. Choose **Prepare repository add-on zip**. The setup addon saves it to `~/Downloads/Kodi MCP` when that folder is available, otherwise to the setup addon's profile data.
9. Choose **Open Install from zip file**.
10. In Kodi's Add-on browser, use **Install from zip file** and select the prepared `repository.kodi-mcp-latest.zip`.
11. Confirm Kodi's required security/install prompts.

After `repository.kodi-mcp` is installed, first installs of target addons use Kodi's normal **Install from repository → Kodi MCP Repository** flow, and later updates can be handled by the MCP server workflow.

Local packaging commands:

```bash
python3 scripts/build_service_addon.py
python3 scripts/build_addon.py script.kodi_mcp_setup
```

## Bridge Surface

`service.kodi_mcp` listens on `0.0.0.0:8765` inside Kodi and exposes:

- `/health`, `/status`, `/runtime/info`
- `/capabilities`, `/control/capabilities`
- `/gui/action`, `/gui/screenshot`
- `/addon/info`, `/addon/ensure-enabled`, `/addon/execute`, `/addon/version-check`
- `/log/tail`, `/log/markers`, `/log/marker`
- `/files/read`, `/debug/addon-db`, `/debug/ping`
- `/mcp/register`, `/mcp/state`, `/repo/stage`

The `/mcp/*` and `/repo/stage` endpoints use the standard bridge envelope expected by `kodi_mcp_server`. If the Kodi addon setting `mcp_token` is configured, all non-health/status/capabilities endpoints require the same value in the `X-Kodi-MCP-Token` header.
`/mcp/state` includes staged repo archive state when a repo zip has been staged. The staged `dev-repo.zip` is repository content for the server/bridge refresh loop, not an installable Kodi add-on zip; first install of target add-ons should use **Add-ons → Install from repository → Kodi MCP Repository** after `repository.kodi-mcp` is installed once.
`/gui/action` supports `up`, `down`, `left`, `right`, `select`, `back`, `home`, `context`, and `info`.
`/gui/screenshot` captures a Kodi screenshot into the addon's profile data directory and can optionally include base64 PNG data. The MCP server can store the returned image server-side and expose it through `/screenshots/<id>.png`, which is the preferred remote-client flow.

## Local Verification

From this repo:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile packages/service.kodi_mcp/http_bridge.py packages/service.kodi_mcp/service.py packages/script.kodi_mcp_setup/default.py packages/script.kodi_mcp_test/default.py scripts/build_addon.py scripts/build_service_addon.py
python3 scripts/build_service_addon.py
python3 scripts/build_addon.py script.kodi_mcp_setup
```

No GitHub push should happen until the local Kodi workflow has been smoke-tested and the operator explicitly asks to push.
