# This file is part of Xpra.
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
from typing import Any

from xpra.common import BACKWARDS_COMPATIBLE
from xpra.net.common import Packet
from xpra.util.system import is_X11
from xpra.util.objects import typedict
from xpra.util.str_fn import Ellipsizer
from xpra.util.env import SilenceWarningsContext
from xpra.server.subsystem.stub import StubServerMixin
from xpra.log import Logger

log = Logger("cursor")


class CursorManager(StubServerMixin):
    """
    Mixin for servers that handle cursors.
    """
    PREFIX = "cursor"

    def __init__(self):
        self.cursors = False
        self.cursor_size = 0
        self.cursor_suspended: bool = False
        # x11:
        self.default_cursor_image = None
        self.last_cursor_serial = 0
        self.last_cursor_image = None
        self.send_cursor_pending = False

    def init(self, opts) -> None:
        self.cursors = opts.cursors

    def setup(self) -> None:
        if is_X11():
            from xpra.x11.error import xlog
            with xlog:
                from xpra.x11.bindings.fixes import XFixesBindings
                XFixes = XFixesBindings()
                if not XFixes.hasXFixes() and self.cursors:
                    log.error("Error: cursor forwarding support disabled")
                    self.cursors = False
                    return
                XFixes.selectCursorChange(True)
                self.default_cursor_image = XFixes.get_cursor_image()
                log("get_default_cursor=%s", Ellipsizer(self.default_cursor_image))

    def add_new_client(self, ss, c: typedict, send_ui: bool, share_count: int) -> None:
        if not send_ui:
            return
        if share_count > 0:
            self.cursor_size = 24
        else:
            self.cursor_size = c.intget("cursor.size", 0)

    def send_initial_data(self, ss, caps, send_ui: bool, share_count: int) -> None:
        if not send_ui:
            return
        self.send_initial_cursors(ss, share_count > 0)

    def send_initial_cursors(self, ss, sharing=False) -> None:
        log("send_initial_cursors(%s, %s)", ss, sharing)
        from xpra.server.source.cursor import CursorsConnection
        if isinstance(ss, CursorsConnection):
            ss.send_cursor()

    def get_caps(self, source) -> dict[str, Any]:
        caps: dict[str, Any] = {}
        if BACKWARDS_COMPATIBLE:
            caps["cursors"] = self.cursors
        Gdk = sys.modules.get("gi.repository.Gdk", None)
        display = Gdk.Display.get_default() if Gdk else None
        if display:
            max_size = tuple(display.get_maximal_cursor_size())
            caps["cursor"] = {
                "default_size": display.get_default_cursor_size(),
                "max_size": max_size,
            }
        if self.default_cursor_image and "default_cursor" in source.wants:
            ce = getattr(source, "cursor_encodings", ())
            if "default" not in ce:
                # we have to send it this way
                # instead of using send_initial_cursors()
                caps["cursor.default"] = self.default_cursor_image
        log("cursor caps=%s", caps)
        return caps

    def get_info(self, _proto) -> dict[str, Any]:
        return {
            CursorManager.PREFIX: {
                "": self.cursors,
                "size": self.cursor_size,
                "current": self.get_cursor_info(),
            },
        }

    def get_cursor_info(self) -> dict[str, Any]:
        # (NOT from UI thread)
        # copy to prevent race:
        cd = self.last_cursor_image
        if cd is None:
            return {}
        dci = self.default_cursor_image
        cinfo = {
            "is-default": bool(dci) and len(dci) >= 8 and len(cd) >= 8 and cd[7] == dci[7],
        }
        # all but pixels:
        for i, x in enumerate(("x", "y", "width", "height", "xhot", "yhot", "serial", None, "name")):
            if x:
                v = cd[i] or ""
                cinfo[x] = v
        return cinfo

    def get_ui_info(self, _proto, _client_uuids=None, *args) -> dict[str, Any]:
        # (from UI thread)
        # now cursor size info:
        Gdk = sys.modules.get("gi.repository.Gdk", None)
        if not Gdk:
            return {}
        display = Gdk.Display.get_default()
        if not display:
            return {}
        with SilenceWarningsContext(DeprecationWarning):
            pos = display.get_default_screen().get_root_window().get_pointer()
        info: dict[str, Any] = {"position": (pos.x, pos.y)}
        for prop, size in {
            "default": display.get_default_cursor_size(),
            "max": tuple(display.get_maximal_cursor_size()),
        }.items():
            if size is None:
                continue
            info[f"{prop}_size"] = size
        if is_X11():
            from xpra.x11.error import xswallow
            with xswallow:
                from xpra.x11.bindings.keyboard import X11KeyboardBindings
                from xpra.x11.bindings.fixes import XFixesBindings
                info |= {
                    "Xkb": X11KeyboardBindings().hasXkb(),
                    "XFixes": XFixesBindings().hasXFixes(),
                }
        return {CursorManager.PREFIX: info}

    def _process_set_cursors(self, proto, packet: Packet) -> None:
        self._process_cursor_set(proto, packet)

    def _process_cursor_set(self, proto, packet: Packet) -> None:
        assert self.cursors, "cannot toggle send_cursors: the feature is disabled"
        ss = self.get_server_source(proto)
        if ss:
            ss.send_cursors = packet.get_bool(1)

    def suspend_cursor(self, proto) -> None:
        # this is called by shadow and desktop servers
        # when we're receiving pointer events but the pointer
        # is no longer over the active window area,
        # so we have to tell the client to switch back to the default cursor
        if self.cursor_suspended:
            return
        self.cursor_suspended = True
        ss = self.get_server_source(proto)
        if ss:
            ss.cancel_cursor_timer()
            ss.send_empty_cursor()

    def restore_cursor(self, proto) -> None:
        # see suspend_cursor
        if not self.cursor_suspended:
            return
        self.cursor_suspended = False
        ss = self.get_server_source(proto)
        if ss and hasattr(ss, "send_cursor"):
            ss.send_cursor()

    def init_packet_handlers(self) -> None:
        self.add_packets(f"{CursorManager.PREFIX}-set")
        self.add_legacy_alias("set-cursors", f"{CursorManager.PREFIX}-set")
