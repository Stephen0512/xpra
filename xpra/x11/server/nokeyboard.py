# This file is part of Xpra.
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.log import Logger

log = Logger("x11", "server", "keyboard")


class NoKeyboardDevice:
    __slots__ = ()

    def __repr__(self):
        return "NoKeyboardDevice"

    @staticmethod
    def press_key(keycode: int, press: bool) -> None:
        log("press_key%s", (keycode, press))
