from __future__ import annotations

import importlib
import sys
import threading
import types


def _install_stubs(monkeypatch, tmp_path):
    created = {}

    class FakeControlImage:
        def __init__(self, x, y, w, h, texture, colorDiffuse=None):
            self.args = (x, y, w, h, texture, colorDiffuse)

    class FakeControlTextBox:
        def __init__(self, *args, **kwargs):
            self.text = ""

        def setText(self, text):
            self.text = text

    class FakeWindowDialog:
        def __init__(self):
            self.controls = []
            self.shown = False
            self.closed = False
            created["window"] = self

        def addControls(self, controls):
            self.controls.extend(controls)

        def show(self):
            self.shown = True

        def close(self):
            self.closed = True

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda: types.SimpleNamespace(
        getAddonInfo=lambda key: str(tmp_path),
    )
    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.WindowDialog = FakeWindowDialog
    xbmcgui.ControlImage = FakeControlImage
    xbmcgui.ControlTextBox = FakeControlTextBox
    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = lambda path: str(tmp_path)
    monkeypatch.setitem(sys.modules, "xbmcaddon", xbmcaddon)
    monkeypatch.setitem(sys.modules, "xbmcgui", xbmcgui)
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs)
    return created


def _module(monkeypatch, tmp_path):
    created = _install_stubs(monkeypatch, tmp_path)
    sys.modules.pop("subtitle_overlay", None)
    module = importlib.import_module("subtitle_overlay")
    monkeypatch.setattr(module, "_png_ready", False)
    return module, created


def test_show_creates_window_once_and_updates_text(monkeypatch, tmp_path):
    module, created = _module(monkeypatch, tmp_path)
    overlay = module.SubtitleOverlay()
    assert overlay.show(["Марат: привет", "Глаша · думаю"], ttl=0)
    window = created["window"]
    assert window.shown and not window.closed
    textbox = window.controls[-1]
    assert "Марат: привет" in textbox.text
    assert overlay.show(["Глаша · ищу"], ttl=0)
    assert created["window"] is window
    assert "Глаша · ищу" in textbox.text


def test_hide_closes_and_next_show_recreates(monkeypatch, tmp_path):
    module, created = _module(monkeypatch, tmp_path)
    overlay = module.SubtitleOverlay()
    overlay.show(["a"], ttl=0)
    overlay.hide()
    assert created["window"].closed
    overlay.show(["b"], ttl=0)
    assert created["window"] is not None
    assert created["window"].shown


def test_empty_lines_hide_overlay(monkeypatch, tmp_path):
    module, created = _module(monkeypatch, tmp_path)
    overlay = module.SubtitleOverlay()
    overlay.show(["a"], ttl=0)
    overlay.show(["   "], ttl=0)
    assert created["window"].closed


def test_ttl_timer_auto_hides(monkeypatch, tmp_path):
    module, created = _module(monkeypatch, tmp_path)
    overlay = module.SubtitleOverlay()
    fired = threading.Event()
    real_timer = threading.Timer

    def fast_timer(delay, fn, *args, **kwargs):
        timer = real_timer(0.01, lambda: (fn(*args, **kwargs), fired.set()))
        return timer

    monkeypatch.setattr(module.threading, "Timer", fast_timer)
    overlay.show(["a"], ttl=30)
    assert fired.wait(2)
    assert created["window"].closed
