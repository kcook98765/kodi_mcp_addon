# -*- coding: utf-8 -*-
"""Developer onboarding UI actions for service.kodi_mcp (Milestone A).

Invoked from Settings -> Developer -> action buttons.

This script is intentionally thin:
- reads existing persisted state.json via http_bridge helpers
- shows operator-facing dialogs
- opens Kodi's native Addons browser so user can run "Install from zip file"
"""

import sys

import xbmc
import xbmcgui


def _get_action() -> str:
    # Called via RunScript(path,setup) so argv[1] is expected.
    if len(sys.argv) >= 2:
        return str(sys.argv[1] or "").strip().lower()
    return "setup"


def _open_addons_browser_install_from_zip_flow() -> None:
    """Open Kodi's native "Install from zip file" flow."""

    xbmc.executebuiltin("InstallFromZip")


def _format_unavailable_message(missing_conditions: list[str]) -> list[str]:
    lines = ["Developer setup is not available.", ""]
    if missing_conditions:
        lines.append("Reasons:")
        for reason in missing_conditions:
            lines.append(f"- {reason}")
    else:
        lines.append("Reasons: unknown")
    return lines


def _show_status_dialog(state: dict) -> None:
    title = "Developer status"
    lines = [
        f"MCP registration present: {'yes' if state.get('registration_present') else 'no'}",
        f"MCP registration stale: {'yes' if state.get('registration_stale') else 'no'}",
        f"Repo zip metadata present: {'yes' if state.get('repo_zip_present_in_state') else 'no'}",
        f"Repo zip file exists: {'yes' if state.get('repo_zip_file_exists') else 'no'}",
        "",
        f"Developer setup available: {'yes' if state.get('dev_setup_available') else 'no'}",
    ]
    repo_path = state.get("repo_zip_special_path")
    if repo_path:
        lines.extend(["", f"Repo zip path: {repo_path}"])

    xbmcgui.Dialog().ok(title, "\n".join(lines))


def _handle_setup_action(state: dict) -> None:
    dialog = xbmcgui.Dialog()

    if not state.get("dev_setup_available"):
        lines = _format_unavailable_message(state.get("missing_conditions") or [])
        dialog.ok("Developer setup", "\n".join(lines))
        return

    repo_path = state.get("repo_zip_special_path") or "(unknown)"
    ok = dialog.yesno(
        "Developer setup",
        "Developer repo zip is staged and ready.",
        f"Location: {repo_path}",
        "Kodi will now open: Install from zip file",
    )
    if not ok:
        return

    # Open native flow (user-driven install)
    _open_addons_browser_install_from_zip_flow()

    dialog.ok(
        "Developer setup",
        "Install from zip file opened.",
        "You still need to browse to the staged zip manually.",
        f"Path: {repo_path}",
    )


def main() -> None:
    action = _get_action()

    # Import within Kodi runtime.
    from http_bridge import get_ui_dev_setup_state

    state = get_ui_dev_setup_state()

    if action == "status":
        _show_status_dialog(state)
        return

    _handle_setup_action(state)


if __name__ == "__main__":
    main()
