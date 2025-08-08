# This file is part of Xpra.
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from typing import Any

from xpra.os_util import gi_import
from xpra.util.system import is_X11
from xpra.util.version import dict_version_trim
from xpra.scripts.config import InitExit
from xpra.common import FULL_INFO
from xpra.exit_codes import ExitCode
from xpra.gtk.versions import get_gtk_version_info
from xpra.server import features
from xpra.server.subsystem.stub import StubServerMixin
from xpra.log import Logger

log = Logger("server", "gtk")


def gdk_init() -> None:
    try:
        from xpra.x11.gtk.display_source import init_gdk_display_source
    except ImportError as e:
        log.warn(f"Warning: unable to initialize gdk display source: {e}")
        return
    if os.environ.get("NO_AT_BRIDGE") is None:
        os.environ["NO_AT_BRIDGE"] = "1"
    init_gdk_display_source()
    # inject Gtk into the windowicon lookup:
    try:
        from xpra.server.window import windowicon
        windowicon.get_default_window_icon = get_default_window_icon
    except ImportError:
        pass


def get_default_window_icon(size: int, wmclass_name: str):
    Gtk = gi_import("Gtk")
    it = Gtk.IconTheme.get_default()  # pylint: disable=no-member
    log("get_default_window_icon(%i) icon theme=%s, wmclass_name=%s", size, it, wmclass_name)
    for icon_name in (
            f"{wmclass_name}-color",
            wmclass_name,
            f"{wmclass_name}_{size}x{size}",
            f"application-x-{wmclass_name}",
            f"{wmclass_name}-symbolic",
            f"{wmclass_name}.symbolic",
    ):
        i = it.lookup_icon(icon_name, size, 0)
        log("lookup_icon(%s)=%s", icon_name, i)
        if not i:
            continue
        try:
            pixbuf = i.load_icon()
            log("load_icon()=%s", pixbuf)
            if pixbuf:
                w, h = pixbuf.props.width, pixbuf.props.height
                log("using '%s' pixbuf %ix%i", icon_name, w, h)
                return w, h, "RGBA", pixbuf.get_pixels()
        except Exception:
            log("%s.load_icon()", i, exc_info=True)
    return None


class GTKServer(StubServerMixin):

    def __init__(self):
        self.display = os.environ.get("DISPLAY", "")
        self.x11_filter = False

    def init(self, opts) -> None:
        from xpra.scripts.main import no_gtk
        no_gtk()
        if is_X11():
            from xpra.scripts.server import verify_gdk_display
            if not verify_gdk_display(self.display):
                raise InitExit(ExitCode.NO_DISPLAY, f"Gdk is unable to access display {self.display!r}")

    def watch_keymap_changes(self) -> None:
        # Set up keymap change notification:
        from xpra.gtk.keymap import get_default_keymap
        keymap = get_default_keymap()
        keymap.connect("keys-changed", self.keymap_changed)

    def setup(self) -> None:
        gdk_init()
        if features.window:
            Gdk = gi_import("Gdk")
            screen = Gdk.Screen.get_default()
            if screen:
                screen.connect("size-changed", self._screen_size_changed)
                screen.connect("monitors-changed", self._monitors_changed)
        from xpra.x11.gtk.bindings import init_x11_filter
        self.x11_filter = init_x11_filter()
        assert self.x11_filter

    def cleanup(self) -> None:
        if not self.x11_filter:
            return
        self.x11_filter = False
        from xpra.x11.gtk.bindings import cleanup_x11_filter
        cleanup_x11_filter()

    def late_cleanup(self, stop=True) -> None:
        from xpra.gtk.util import close_gtk_display
        close_gtk_display()
        from xpra.x11.gtk.display_source import close_gdk_display_source
        close_gdk_display_source()

    def get_caps(self, source) -> dict[str, Any]:
        if "versions" in source.wants and FULL_INFO >= 2:
            return {"versions": get_gtk_version_info()}
        return {}

    def get_info(self, _proto) -> dict[str, Any]:
        return {"versions": dict_version_trim(get_gtk_version_info())}
