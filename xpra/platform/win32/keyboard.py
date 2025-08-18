# This file is part of Xpra.
# Copyright (C) 2010 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2011 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from typing import Final
import ctypes
from ctypes.wintypes import HANDLE
from ctypes import create_string_buffer, byref
from ctypes.wintypes import DWORD, BYTE
from collections.abc import Callable, Sequence

from xpra.os_util import gi_import
from xpra.platform.win32.common import (
    ActivateKeyboardLayout,
    GetKeyState, GetKeyboardLayoutList, GetKeyboardLayout,
    GetIntSystemParametersInfo, GetKeyboardLayoutName,
    GetWindowThreadProcessId,
    GetKeyboardState, SetKeyboardState, MapVirtualKeyW, keybd_event,
)
from xpra.platform.win32 import constants as win32con
from xpra.platform.keyboard_base import KeyboardBase
from xpra.keyboard.common import KeyEvent
from xpra.keyboard.layouts import WIN32_LAYOUTS, WIN32_KEYBOARDS
from xpra.gtk.keymap import KEY_TRANSLATIONS
from xpra.util.str_fn import csv, bytestostr
from xpra.util.env import envint, envbool
from xpra.log import Logger

log = Logger("keyboard")

LAYOUT_MASKS: dict[int, Sequence[int]] = {
    0xffffffff: (0, 16),
    0xffff: (0,),
    0x3ff: (0,),
}

MAPVK_VK_TO_VSC: Final[int] = 0


def _GetKeyboardLayoutList() -> list[int]:
    max_items = 32
    # PHANDLE = ctypes.POINTER(HANDLE)
    # noinspection PyTypeChecker,PyCallingNonCallable
    handle_list = (HANDLE * max_items)()
    # noinspection PyTypeChecker
    GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(HANDLE * max_items)]
    count = GetKeyboardLayoutList(max_items, ctypes.byref(handle_list))
    layouts: list[int] = []
    for i in range(count):
        layouts.append(int(handle_list[i]))
    return layouts


def get_layout_defs() -> dict[str, int]:
    layouts_defs: dict[str, int] = {}
    try:
        hkl_list = _GetKeyboardLayoutList()
        log("GetKeyboardLayoutList()=%s", csv(hex(v) for v in hkl_list))
        for hkl in hkl_list:
            for mask, bitshifts in LAYOUT_MASKS.items():
                kbid = 0
                for bitshift in bitshifts:
                    kbid = (hkl & mask) >> bitshift
                    if kbid in WIN32_LAYOUTS:
                        break
                win32_layout = WIN32_LAYOUTS.get(kbid)
                if win32_layout:
                    code, _, _, _, layout, variants = win32_layout
                    log("found keyboard layout '%s' / %#x with variants=%s, code '%s' for kbid=%#x",
                        layout, kbid, variants, code, hkl)
                    if layout and layout not in layouts_defs:
                        layouts_defs[layout] = hkl
                        break
    except Exception as e:
        log("get_layout_spec()", exc_info=True)
        log.error("Error: failed to detect keyboard layouts using GetKeyboardLayoutList:")
        log.estr(e)
    return layouts_defs


def get_layout_name_value() -> int:
    KL_NAMELENGTH = 9
    name_buf = create_string_buffer(KL_NAMELENGTH)
    if GetKeyboardLayoutName(name_buf):
        log("get_layout_spec() GetKeyboardLayoutName()=%s", bytestostr(name_buf.value))
        try:
            # win32 API returns a hex string
            return int(name_buf.value, 16)
        except ValueError:
            log.warn("Warning: failed to parse keyboard layout code '%s'", bytestostr(name_buf.value))
    return 0


def get_window_layout() -> int:
    pid = DWORD(0)
    GetWindowThreadProcessId(0, byref(pid))
    tid = GetWindowThreadProcessId(0, pid)
    hkl = GetKeyboardLayout(tid)
    log("GetKeyboardLayout(%i)=%#x", tid, hkl)
    return hkl


def x11_layouts_to_win32_hkl() -> dict[str, int]:
    layout_to_hkl: dict[str, int] = {}
    max_items = 32
    try:
        # noinspection PyTypeChecker,PyCallingNonCallable
        handle_list = (HANDLE * max_items)()
        count = GetKeyboardLayoutList(max_items, ctypes.byref(handle_list))
        for i in range(count):
            hkl = handle_list[i]
            hkli = int(hkl)
            for mask, bitshifts in LAYOUT_MASKS.items():
                kbid = 0
                for bitshift in bitshifts:
                    kbid = (hkli & mask) >> bitshift
                    if kbid in WIN32_LAYOUTS:
                        break
                if kbid in WIN32_LAYOUTS:
                    code, _, _, _, _layout, _variants = WIN32_LAYOUTS[kbid]
                    log("found keyboard layout '%s' / %#x with variants=%s, code '%s' for kbid=%#x",
                        _layout, kbid, _variants, code, hkli)
                    if _layout and _layout not in layout_to_hkl:
                        layout_to_hkl[_layout] = hkli
                        break
    except Exception:
        log("x11_layouts_to_win32_hkl()", exc_info=True)
    return layout_to_hkl


EMULATE_ALTGR = envbool("XPRA_EMULATE_ALTGR", True)
EMULATE_ALTGR_CONTROL_KEY_DELAY = envint("XPRA_EMULATE_ALTGR_CONTROL_KEY_DELAY", 50)


def clear_keys_pressed() -> None:
    # noinspection PyCallingNonCallable,PyTypeChecker
    keystate = (BYTE * 256)()
    if GetKeyboardState(keystate):
        vknames = {}
        for vkconst in (x for x in dir(win32con) if x.startswith("VK_")):
            vknames[getattr(win32con, vkconst)] = vkconst[3:]
        pressed = []
        for i in range(256):
            if keystate[i]:
                pressed.append(vknames.get(i, i))
        log("keys still pressed: %s", csv(pressed))
        for x in (
                win32con.VK_LSHIFT, win32con.VK_RSHIFT, win32con.VK_SHIFT,
                win32con.VK_LCONTROL, win32con.VK_RCONTROL, win32con.VK_CONTROL,
                win32con.VK_LMENU, win32con.VK_RMENU, win32con.VK_MENU,
                win32con.VK_LWIN, win32con.VK_RWIN,
        ):
            keystate[x] = 0
        SetKeyboardState(keystate)


def fake_key(keycode, press):
    if keycode <= 0:
        log.warn("no keycode found for %s", keycode)
        return
    # KEYEVENTF_SILENT = 0X4
    flags = 0
    if not press:
        flags |= win32con.KEYEVENTF_KEYUP
    # get the scancode:
    scancode = MapVirtualKeyW(keycode, MAPVK_VK_TO_VSC)
    # see: http://msdn.microsoft.com/en-us/library/windows/desktop/ms646304(v=vs.85).aspx
    log("fake_key(%s, %s) calling keybd_event(%s, %s, %s, 0)", keycode, press, keycode, scancode, flags)
    keybd_event(keycode, scancode, flags, 0)


class Win32Keyboard:

    @staticmethod
    def press_key(keycode, press):
        fake_key(keycode, press)

    @staticmethod
    def clear_keys_pressed(_keycodes):
        clear_keys_pressed()


def get_keyboard_device():
    return Win32Keyboard()


class Keyboard(KeyboardBase):
    """ This is for getting keys from the keyboard on the client side.
        Deals with GTK bugs and oddities:
        * missing 'Num_Lock'
        * simulate 'Alt_Gr'
    """

    def init_vars(self) -> None:
        super().init_vars()
        self.num_lock_modifier = ""
        self.altgr_modifier = ""
        self.delayed_event: tuple[Callable, int, KeyEvent] | None = None
        self.last_layout_message = ""
        # workaround for "period" vs "KP_Decimal" with gtk2 (see ticket #586):
        # translate "period" with keyval=46 and keycode=110 to KP_Decimal:
        KEY_TRANSLATIONS[("period", 46, 110)] = "KP_Decimal"
        # workaround for "fr" keyboards, which use a different key name under X11:
        KEY_TRANSLATIONS[("dead_tilde", 65107, 50)] = "asciitilde"
        KEY_TRANSLATIONS[("dead_grave", 65104, 55)] = "grave"
        self.__x11_layouts_to_win32_hkl = x11_layouts_to_win32_hkl()

    def set_platform_layout(self, layout: str) -> None:
        hkl = self.__x11_layouts_to_win32_hkl.get(layout)
        if hkl is None:
            log(f"asked layout ({layout}) has no corresponding registered keyboard handle")
            return
        # https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-activatekeyboardlayout
        # KLF_SETFORPROCESS|KLF_REORDER = 0x108
        if not ActivateKeyboardLayout(hkl, 0x108):
            log(f"ActivateKeyboardLayout: cannot change layout to {layout}")

    def __repr__(self):
        return "win32.Keyboard"

    def set_modifier_mappings(self, mappings: dict[str, str]) -> None:
        super().set_modifier_mappings(mappings)
        self.num_lock_modifier = self.modifier_keys.get("Num_Lock") or ""
        log("set_modifier_mappings found 'Num_Lock' with modifier value: %s", self.num_lock_modifier)
        for x in ("ISO_Level3_Shift", "Mode_switch"):
            mod: str = self.modifier_keys.get(x) or ""
            if mod:
                self.altgr_modifier = mod
                log("set_modifier_mappings found 'AltGr'='%s' with modifier value: %s", x, self.altgr_modifier)
                break

    def mask_to_names(self, mask) -> list[str]:
        """ Patch NUMLOCK and AltGr """
        names = super().mask_to_names(mask)
        if EMULATE_ALTGR:
            rmenu = GetKeyState(win32con.VK_RMENU)
            # log("GetKeyState(VK_RMENU)=%s", rmenu)
            if rmenu not in (0, 1):
                self.AltGr_modifiers(names)
        if self.num_lock_modifier:
            try:
                numlock = GetKeyState(win32con.VK_NUMLOCK)
                if numlock and self.num_lock_modifier not in names:
                    names.append(self.num_lock_modifier)
                elif not numlock and self.num_lock_modifier in names:
                    names.remove(self.num_lock_modifier)
                log("mask_to_names(%s) GetKeyState(VK_NUMLOCK)=%s, names=%s", mask, numlock, names)
            except Exception:
                log("mask_to_names error modifying numlock", exc_info=True)
        else:
            log("mask_to_names(%s)=%s", mask, names)
        return names

    def AltGr_modifiers(self, modifiers, pressed=True):
        add = []
        clear = ["mod1", "mod2", "control"]
        if self.altgr_modifier:
            if pressed:
                add.append(self.altgr_modifier)
            else:
                clear.append(self.altgr_modifier)
        log("AltGr_modifiers(%s, %s) AltGr=%s, add=%s, clear=%s", modifiers, pressed, self.altgr_modifier, add, clear)
        for x in add:
            if x not in modifiers:
                modifiers.append(x)
        for x in clear:
            if x in modifiers:
                modifiers.remove(x)

    def get_keymap_modifiers(self) -> tuple[dict, list[str], list[str]]:
        """
            ask the server to manage numlock, and lock can be missing from mouse events
            (or maybe this is virtualbox causing it?)
        """
        return {}, [], ["lock"]

    def get_all_x11_layouts(self) -> dict[str, str]:
        x11_layouts = {}
        for win32_layout in WIN32_LAYOUTS.values():
            # ("ARA", "Saudi Arabia",   "Arabic",                   1356,   "ar", []),
            x11_layout = win32_layout[4]
            if not x11_layout:
                continue
            if x11_layout in x11_layouts:
                continue
            name = win32_layout[2]
            x11_layouts[x11_layout] = name
        return x11_layouts

    def get_layout_spec(self) -> tuple[str, str, Sequence[str], str, Sequence[str], str]:
        model = ""
        layout = ""
        layouts_defs = get_layout_defs()
        variant = ""
        variants: Sequence[str] = ()
        options = ""
        layout_code = 0

        descr = None
        ival = get_layout_name_value()
        if ival:
            sublang = (ival & 0xfc00) >> 10
            log("sublang(%#x)=%#x", ival, sublang)
            for mask in LAYOUT_MASKS:
                val = ival & mask
                kbdef = WIN32_KEYBOARDS.get(val, ())
                log("get_layout_spec() WIN32_KEYBOARDS[%#x]=%s", val, kbdef)
                if kbdef:
                    _layout, _descr = kbdef
                    if _layout == "??":
                        log.warn("Warning: the X11 codename for %#x is not known", val)
                        log.warn(" only identified as '%s'", _descr)
                        log.warn(" please file a bug report")
                        continue
                    if _layout not in layouts_defs:
                        layouts_defs[_layout] = ival
                    if not layout:
                        layout = _layout
                        descr = _descr
                        layout_code = ival
                        break
            if not layout:
                log.warn("Warning: unknown keyboard layout %#x", ival)
                log.warn(" please file a bug report")
                self.last_layout_message = layout

        with log.trap_error("Error: failed to detect keyboard layout using GetKeyboardLayout"):
            hkl = get_window_layout()
            for mask in LAYOUT_MASKS:
                kbid = hkl & mask
                win32_layout = WIN32_LAYOUTS.get(kbid)
                if not win32_layout:
                    log("unknown win32 layout %#x", kbid)
                    continue
                code, _, _, _, layout0, variants = win32_layout
                log("found keyboard layout '%s' / %#x with variants=%s, code '%s' for kbid=%i (%#x)",
                    layout0, kbid, variants, code, kbid, hkl)
                if layout0 not in layouts_defs:
                    layouts_defs[layout0] = hkl
                # only override "layout" if unset:
                if not layout and layout0:
                    layout = layout0
                    layout_code = hkl

        layouts = list(layouts_defs.keys())
        if layouts and not layout:
            layout = layouts[0]
            layout_code = layouts_defs.get(layout, 0)

        if layout and self.last_layout_message != layout:
            if descr:
                log.info(f"keyboard layout {descr!r} : {layout!r} ({layout_code:#x})")
            else:
                log.info(f"keyboard layout {layout!r} ({layout_code:#x})")
            self.last_layout_message = layout
        return model, layout, layouts, variant, list(variants), options

    def get_keyboard_repeat(self) -> tuple[int, int] | None:
        try:
            _delay = GetIntSystemParametersInfo(win32con.SPI_GETKEYBOARDDELAY)
            _speed = GetIntSystemParametersInfo(win32con.SPI_GETKEYBOARDSPEED)
            # now we need to normalize those weird win32 values:
            # 0=250, 3=1000:
            delay = (_delay + 1) * 250
            # 0=1000/30, 31=1000/2.5
            _speed = min(31, max(0, _speed))
            speed = int(1000 / (2.5 + 27.5 * _speed / 31))
            log("keyboard repeat speed(%s)=%s, delay(%s)=%s", _speed, speed, _delay, delay)
            return delay, speed
        except Exception as e:
            log.error("failed to get keyboard rate: %s", e)
        return None

    def process_key_event(self, send_key_action_cb: Callable, wid: int, key_event: KeyEvent) -> None:
        """ Caps_Lock and Num_Lock don't work properly: they get reported more than once,
            they are reported as not pressed when the key is down, etc
            So we just ignore those and rely on the list of "modifiers" passed
            with each keypress to let the server set them for us when needed.
        """
        if key_event.keyval == 2 ** 24 - 1 and key_event.keyname == "VoidSymbol":
            log("process_key_event: ignoring %s", key_event)
            return
        # self.modifier_mappings = None       #{'control': [(37, 'Control_L'), (105, 'Control_R')], 'mod1':
        # self.modifier_keys = {}             #{"Control_L" : "control", ...}
        # self.modifier_keycodes = {}         #{"Control_R" : [105], ...}
        # self.modifier_keycodes = {"ISO_Level3_Shift": [108]}
        # we can only deal with 'Alt_R' and simulate AltGr (ISO_Level3_Shift)
        # if we have modifier_mappings
        if EMULATE_ALTGR and self.altgr_modifier and self.modifier_mappings:
            rmenu = GetKeyState(win32con.VK_RMENU)
            if key_event.keyname == "Control_L":
                log("process_key_event: %s pressed=%s, with GetKeyState(VK_RMENU)=%s",
                    key_event.keyname, key_event.pressed, rmenu)
                # AltGr key events are often preceded by a spurious "Control_L" event
                # delay this one a little bit so we can skip it if an "AltGr" does come through next:
                if rmenu in (0, 1):
                    self.delayed_event = (send_key_action_cb, wid, key_event)
                    # needed for altgr emulation timeouts:
                    glib = gi_import("GLib")
                    glib.timeout_add(EMULATE_ALTGR_CONTROL_KEY_DELAY, self.send_delayed_key)
                return
            if key_event.keyname == "Alt_R":
                log("process_key_event: Alt_R pressed=%s, with GetKeyState(VK_RMENU)=%s", key_event.pressed, rmenu)
                if rmenu in (0, 1):
                    # cancel "Control_L" if one was due:
                    self.delayed_event = None
                # modify the key event so that it will only trigger the modifier update,
                # and not not the key event itself:
                key_event.string = ""
                key_event.keyname = ""
                key_event.group = -1
                key_event.keyval = -1
                key_event.keycode = -1
                self.AltGr_modifiers(key_event.modifiers)
        self.send_delayed_key()
        super().process_key_event(send_key_action_cb, wid, key_event)

    def send_delayed_key(self) -> None:
        # timeout: this must be a real one, send it now
        dk = self.delayed_event
        log("send_delayed_key() delayed_event=%s", dk)
        if dk:
            self.delayed_event = None
            rmenu = GetKeyState(win32con.VK_RMENU)
            log("send_delayed_key() GetKeyState(VK_RMENU)=%s", rmenu)
            if rmenu in (0, 1):
                super().process_key_event(*dk)
