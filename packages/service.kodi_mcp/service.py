# -*- coding: utf-8 -*-
"""Kodi MCP Service main entry point."""

import xbmc

from http_bridge import BRIDGE_BIND_HOST, BRIDGE_BIND_PORT, KodiBridgeServer


class KodiMCPService(xbmc.Monitor):
    """Main service class."""

    def __init__(self):
        super().__init__()
        self.bridge = KodiBridgeServer(host=BRIDGE_BIND_HOST, port=BRIDGE_BIND_PORT)
        xbmc.log(
            "[service.kodi_mcp] Service starting (bind=%s:%s)" % (BRIDGE_BIND_HOST, BRIDGE_BIND_PORT),
            xbmc.LOGINFO,
        )
        self.bridge.start()

    def onSystemWake(self):
        xbmc.log("[service.kodi_mcp] System wake detected", xbmc.LOGINFO)

    def monitor(self):
        xbmc.log("[service.kodi_mcp] Entering main service loop", xbmc.LOGINFO)
        while not self.abortRequested():
            if self.waitForAbort(1):
                break
        xbmc.log("[service.kodi_mcp] Service stopping", xbmc.LOGINFO)
        self.bridge.stop()


if __name__ == "__main__":
    service = KodiMCPService()
    service.monitor()
