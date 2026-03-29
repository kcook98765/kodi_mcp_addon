# -*- coding: utf-8 -*-
"""Minimal executable test addon for Kodi MCP workflow verification."""

import xbmc
import xbmcaddon

TEST_MARKER = "KODI_MCP_TEST_ADDON_EXECUTED_V0_0_9"


def main():
    addon = xbmcaddon.Addon()
    marker = "[%s][TEST] version=%s marker=%s" % (
        addon.getAddonInfo("id"),
        addon.getAddonInfo("version"),
        TEST_MARKER,
    )
    xbmc.log(marker, xbmc.LOGINFO)


if __name__ == "__main__":
    main()
