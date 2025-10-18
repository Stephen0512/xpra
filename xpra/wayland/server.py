#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2025 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from collections.abc import Sequence

from xpra.codecs.image import ImageWrapper
from xpra.util.gobject import to_gsignals
from xpra.wayland.compositor import WaylandCompositor, add_event_listener
from xpra.wayland.models.window import Window
from xpra.server.base import ServerBase
from xpra.net.common import Packet
from xpra.common import noop
from xpra.os_util import gi_import
from xpra.log import Logger

log = Logger("server", "wayland")

GObject = gi_import("GObject")
GLib = gi_import("GLib")


class WaylandSeamlessServer(GObject.GObject, ServerBase):
    __gsignals__ = to_gsignals(ServerBase.__signals__)

    def __init__(self):
        GObject.GObject.__init__(self)
        ServerBase.__init__(self)
        self.session_type: str = "wayland"
        self.compositor = WaylandCompositor()
        self.wayland_fd_source = 0
        self.focused = 0
        self.pointer_focus = 0
        self.register_events()
        os.environ["GDK_BACKEND"] = "wayland"

    def register_events(self) -> None:
        add_event_listener("new-surface", self._new_surface)
        add_event_listener("metadata", self._metadata)
        add_event_listener("surface-image", self._surface_image)
        add_event_listener("map", self._map)
        add_event_listener("unmap", self._unmap)
        add_event_listener("commit", self._commit)
        add_event_listener("destroy", self._destroy)

    def make_keyboard_device(self):
        return self.compositor.get_keyboard_device()

    @staticmethod
    def get_keyboard_config(props=None):
        # p = typedict(props or {})
        from xpra.wayland.keyboard_config import KeyboardConfig
        keyboard_config = KeyboardConfig()
        log("get_keyboard_config(..)=%s", keyboard_config)
        return keyboard_config

    def make_pointer_device(self):
        return self.compositor.get_pointer_device()

    @staticmethod
    def get_clipboard_class():
        return None  # TODO: WaylandClipboard

    def get_surface(self, wid: int) -> int:
        window = self._id_to_window.get(wid)
        if not window:
            return 0
        return window._gproperties.get("surface", 0)

    def _focus(self, _server_source, wid: int, modifiers) -> None:
        if self.focused == wid:
            return
        log("_focus(%s, %s) current focus=%i", wid, modifiers, self.focused)
        for window_id, state in {
            self.focused: False,        # unfocus
            wid: True,                  # focus
        }.items():
            if not window_id:
                continue
            window = self._id_to_window.get(window_id)
            surface = self.get_surface(window_id)
            if window and surface:
                self.compositor.focus(surface, state)
        self.focused = wid

    def set_pointer_focus(self, wid: int, pointer: Sequence) -> None:
        log("set_pointer_focus(%i) current focus=%i", wid, self.pointer_focus)
        if self.pointer_focus == wid:
            # no change
            return
        if self.pointer_focus and wid == 0:
            # no window has the focus:
            self.pointer_device.leave_surface()
            self.pointer_focus = 0
            return
        surface = self.get_surface(wid)
        log("surface(%i)=%#x", wid, surface)
        if surface and len(pointer) >= 4:
            x, y = pointer[2:4]
            if self.pointer_device.enter_surface(surface, x, y):
                self.pointer_focus = wid

    def do_process_mouse_common(self, proto, device_id: int, wid: int, pointer, props) -> bool:
        self.set_pointer_focus(wid, pointer)
        log("pointer: %r",pointer)
        return super().do_process_mouse_common(proto, device_id, wid, pointer, props)

    def _process_map_window(self, proto, packet: Packet) -> None:
        wid = packet.get_wid()
        window = self._id_to_window.get(wid)
        surface = self.get_surface(wid)
        if not (window and surface):
            return
        w = packet.get_i16(4)
        h = packet.get_i16(5)
        self.compositor.resize(surface, w, h)
        self.refresh_window(window)

    def _process_configure_window(self, proto, packet: Packet) -> None:
        wid = packet.get_wid()
        window = self._id_to_window.get(wid)
        surface = self.get_surface(wid)
        if not (window and surface):
            return
        w = packet.get_u16(4)
        h = packet.get_u16(5)
        self.compositor.resize(surface, w, h)
        self.refresh_window(window)

    def _new_surface(self, surface: int, wid: int, title: str, app_id: str, size: tuple[int, int]) -> None:
        geom = (0, 0, size[0], size[1])
        window = Window()
        window.setup()
        window._internal_set_property("surface", surface)
        window._internal_set_property("title", title)
        window._internal_set_property("app-id", app_id)
        window._internal_set_property("iconic", True)
        window._internal_set_property("geometry", geom)
        window._internal_set_property("image", None)
        window._internal_set_property("depth", 32)
        window._internal_set_property("decorations", False)
        self.do_add_new_window_common(wid, window)
        if size != (0, 0):
            self._do_send_new_window_packet("new-window", window, geom)
        #surface = self.get_surface(wid)
        #log.warn("surface(%i)=%#x", wid, surface)
        #self.pointer_device.enter_surface(surface, 23, 1)

    def _metadata(self, wid: int, prop: str, value) -> None:
        window = self._id_to_window.get(wid)
        if not window:
            log.warn("Warning: cannot set metadata %s=%r", prop, value)
            log.warn(" window %i not found!", wid)
            return
        assert prop in ("title", "role")
        window._internal_set_property(prop, value)

    def _surface_image(self, wid: int, image: ImageWrapper) -> None:
        window = self._id_to_window.get(wid)
        if not window:
            log.warn("Warning: cannot update window %i: not found!", wid)
            return
        log("new surface image for window %i: %s", wid, image)
        # don't free this image after use,
        # we will replace it with a new one when needed
        image.free = noop
        window._updateprop("image", image)
        # we can't free the previous image, which may still be referenced by the window compression thread

    def _map(self, wid: int, title: str, app_id: str, size: tuple[int, int]) -> None:
        window = self._id_to_window.get(wid)
        if not window:
            log.warn("Warning: cannot map window %i: not found!", wid)
            return
        old_geom = window.get_property("geometry")
        geom = (old_geom[0], old_geom[1], size[0], size[1])
        window._updateprop("geometry", geom)
        window._updateprop("title", title)
        window._updateprop("app-id", app_id)
        if old_geom == (0, 0, 0, 0):
            self._do_send_new_window_packet("new-window", window, geom)

    def _unmap(self, wid: int) -> None:
        window = self._id_to_window.get(wid)
        if not window:
            return
        window.set_property("iconic", True)

    def _commit(self, wid: int, mapped: bool, rects: Sequence[tuple[int, int, int, int]]) -> None:
        window = self._id_to_window.get(wid)
        if not window:
            return
        options = {
            "damage": True,
        }
        last = len(rects) - 1
        for i, (x, y, w, h) in enumerate(rects):
            options["more"] = i != last
            self.refresh_window_area(window, x, y, w, h, options=options)

    def _destroy(self, wid: int) -> None:
        self._remove_wid(wid)

    @staticmethod
    def get_cursor_data() -> None:
        return None

    @staticmethod
    def set_desktop_geometry(w: int, h: int) -> None:
        pass

    @staticmethod
    def get_display_size():
        return 3840, 2160

    @staticmethod
    def get_display_description() -> str:
        return "Wayland Display (details missing)"

    def wayland_io_callback(self, fd: int, condition):
        log("wayland_io_callback%s", (fd, condition))
        if condition & GLib.IO_IN:
            self.compositor.process_events()
        elif condition & GLib.IO_ERR:
            log.error("Error: IO_ERR on wayland compositor fd %i", fd)
        return GLib.SOURCE_CONTINUE

    def setup(self) -> None:
        self.compositor.initialize()
        super().setup()

    def do_run(self) -> None:
        log("WaylandSeamlessServer.do_run()")
        fd = self.compositor.get_event_loop_fd()
        conditions = GLib.IO_IN | GLib.IO_ERR
        log("wayland compositor event loop fd=%i", fd)
        self.wayland_fd_source = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, fd, conditions, self.wayland_io_callback)
        super().do_run()

    def cleanup(self):
        fd = self.wayland_fd_source
        if fd:
            self.wayland_fd_source = 0
            GLib.source_remove(fd)
        c = self.compositor
        if c:
            c.cleanup()
            self.compositor = None


GObject.type_register(WaylandSeamlessServer)
