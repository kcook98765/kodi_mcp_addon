# -*- coding: utf-8 -*-
"""Modeless bottom-screen subtitle overlay rendered inside Kodi.

The HTTP bridge calls show()/hide(); a single daemon timer auto-hides the
window after the requested TTL so a crashed caller cannot leave text stuck
on screen. All GUI mutations happen under one lock because the threading
HTTP server may service concurrent requests.
"""

from __future__ import annotations

import os
import struct
import threading
import zlib

import xbmcaddon
import xbmcgui
import xbmcvfs

BOX_X = 64
BOX_Y = 540
BOX_W = 1152
BOX_H = 160
PAD = 20
MAX_CHARS = 800
DEFAULT_TTL = 8.0

_png_ready = False
_png_path = ""
_singleton = None
_singleton_lock = threading.Lock()


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _ensure_background_png() -> str:
    global _png_ready, _png_path
    if _png_ready:
        return _png_path
    addon = xbmcaddon.Addon()
    profile = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    os.makedirs(profile, exist_ok=True)
    path = os.path.join(profile, "subtitle_bg.png")
    raw = b"\x00\xff\xff\xff"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )
    with open(path, "wb") as handle:
        handle.write(png)
    _png_path = path
    _png_ready = True
    return path


class SubtitleOverlay:
    def __init__(self, *, on_error=None) -> None:
        self._lock = threading.Lock()
        self._window = None
        self._textbox = None
        self._timer = None
        self._on_error = on_error

    def show(self, lines, ttl: float = DEFAULT_TTL) -> bool:
        text = "\n".join(str(line) for line in lines if str(line).strip())[:MAX_CHARS]
        if not text:
            self.hide()
            return True
        try:
            with self._lock:
                self._cancel_timer_locked()
                if self._window is None:
                    window = xbmcgui.WindowDialog()
                    background = xbmcgui.ControlImage(
                        BOX_X, BOX_Y, BOX_W, BOX_H,
                        _ensure_background_png(), colorDiffuse="D0000000",
                    )
                    textbox = xbmcgui.ControlTextBox(
                        BOX_X + PAD, BOX_Y + PAD, BOX_W - 2 * PAD, BOX_H - 2 * PAD,
                        font="font16", textColor="FFFFFFFF",
                    )
                    window.addControls([background, textbox])
                    window.show()
                    self._window = window
                    self._textbox = textbox
                self._textbox.setText(text)
                if ttl > 0:
                    self._timer = threading.Timer(ttl, self.hide)
                    self._timer.daemon = True
                    self._timer.start()
            return True
        except Exception as exc:
            self._report(exc)
            return False

    def hide(self) -> None:
        try:
            with self._lock:
                self._cancel_timer_locked()
                window = self._window
                self._window = None
                self._textbox = None
                if window is not None:
                    window.close()
        except Exception as exc:
            self._report(exc)

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _report(self, exc: Exception) -> None:
        if self._on_error is not None:
            self._on_error(exc)


def get_overlay() -> SubtitleOverlay:
    with _singleton_lock:
        global _singleton
        if _singleton is None:
            _singleton = SubtitleOverlay()
        return _singleton
