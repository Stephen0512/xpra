#!/usr/bin/env python3
# Copyright (C) 2025 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from time import monotonic
from typing import Tuple

from xpra.log import Logger

from libc.string cimport memset
from libc.stdint cimport uintptr_t, uint64_t, uint32_t, int32_t
from xpra.wayland.wlroots cimport (
    wlr_seat, wlr_surface, wlr_xdg_surface, wlr_keyboard, wlr_keyboard_impl,
    wlr_seat_keyboard_notify_key, wlr_seat_keyboard_notify_modifiers,
    wlr_seat_keyboard_notify_enter, wlr_seat_keyboard_clear_focus,
    wlr_keyboard_modifiers, wlr_keyboard_set_repeat_info,
    WL_KEYBOARD_KEY_STATE_PRESSED, WL_KEYBOARD_KEY_STATE_RELEASED,
    xkb_context, xkb_context_new, xkb_context_unref,
    xkb_keymap, xkb_keymap_unref, wlr_keyboard_set_keymap, xkb_rule_names, xkb_keymap_new_from_names,
    XKB_CONTEXT_NO_FLAGS, XKB_KEYMAP_COMPILE_NO_FLAGS,
)

log = Logger("wayland", "keyboard")

base_time = monotonic()


cdef inline uint32_t get_time_msec() noexcept:
    return round((monotonic() - base_time) * 1000)


cdef inline bytes b(s: str):
    if not s:
        return b""
    return s.encode("latin1")


cdef void virtual_keyboard_led_update(wlr_keyboard *wlr_kb, uint32_t leds) noexcept:
    """Called when LED state changes (Caps Lock, Num Lock, etc.)"""
    log.info("led-update: %#x", leds)


# Global keyboard implementation struct
cdef wlr_keyboard_impl keyboard_impl
keyboard_impl.name = b"xpra-virtual-keyboard"
keyboard_impl.led_update = virtual_keyboard_led_update


cdef class WaylandKeyboard:
    cdef wlr_seat *seat
    cdef wlr_keyboard *keyboard

    def __init__(self, uintptr_t seat_ptr):
        self.seat = <wlr_seat*>seat_ptr
        if not seat_ptr:
            raise ValueError("seat pointer is NULL")
        # self.keyboard = wlr_keyboard_create(&keyboard_impl, b"xpra-virtual-keyboard")
        # if self.keyboard == NULL:
        #    raise RuntimeError("Failed to create wlr_keyboard")
        # self.set_layout()

    def cleanup(self) -> None:
        if self.keyboard != NULL:
            # wlr_keyboard_destroy(self.keyboard)
            self.keyboard = NULL

    def set_layout(self, layout="us", model="pc105", variant="", options="") -> None:
        cdef xkb_context *context = xkb_context_new(XKB_CONTEXT_NO_FLAGS)
        if context == NULL:
            raise RuntimeError("failed to create new xkb context")
        cdef xkb_rule_names rules
        memset(&rules, 0, sizeof(xkb_rule_names))
        rules.rules = NULL
        bmodel = b(model)
        blayout = b(layout)
        rules.model = bmodel
        rules.layout = blayout
        if variant:
            bvariant = b(variant)
            rules.variant = bvariant
        if options:
            boptions = b(options)
            rules.options = boptions
        cdef xkb_keymap *keymap = xkb_keymap_new_from_names(context, &rules, XKB_KEYMAP_COMPILE_NO_FLAGS)
        if keymap == NULL:
            raise RuntimeError(f"failed to set keymap layout {layout!r}")
        wlr_keyboard_set_keymap(self.keyboard, keymap)
        xkb_keymap_unref(keymap)
        xkb_context_unref(context)

    def press_key(self, keycode: int, press: bool) -> None:
        log("press_key%s", (keycode, press))
        cdef uint32_t time_msec = get_time_msec()
        cdef uint32_t state = WL_KEYBOARD_KEY_STATE_PRESSED if press else WL_KEYBOARD_KEY_STATE_RELEASED
        wlr_seat_keyboard_notify_key(self.seat, time_msec, keycode, state)

    def clear_keys_pressed(self, keycodes) -> None:
        """ this is not a real keyboard """

    def set_repeat_rate(self, delay: int, interval: int) -> None:
        if self.keyboard != NULL:
            wlr_keyboard_set_repeat_info(self.keyboard, delay, interval)

    def get_keycodes_down(self) -> Sequence[int]:
        return ()

    def get_layout_group(self) -> int:
        return 0

    def _update_modifiers(self, uint32_t depressed=0, uint32_t latched=0,
                         uint32_t locked=0, uint32_t group=0):
        """
            depressed: Currently pressed modifiers
            latched: Latched modifiers
            locked: Locked modifiers (Caps Lock, etc.)
            group: Keyboard layout group
        """
        cdef wlr_keyboard_modifiers mods
        mods.depressed = depressed
        mods.latched = latched
        mods.locked = locked
        mods.group = group
        wlr_seat_keyboard_notify_modifiers(self.seat, &mods)

    def focus(self, uintptr_t xdg_surface_ptr) -> None:
        if not xdg_surface_ptr:
            wlr_seat_keyboard_clear_focus(self.seat)
            log("focus(%#x) cleared focus", xdg_surface_ptr)
            return
        cdef wlr_xdg_surface *xdg_surface = <wlr_xdg_surface*> xdg_surface_ptr
        cdef wlr_surface *surface = xdg_surface.surface
        if not surface:
            log("surface is NULL, cleared focus")
            return

        wlr_seat_keyboard_notify_enter(self.seat, surface, NULL, 0, NULL)
        log.warn("keyboard.focus(%#x) done", xdg_surface_ptr)
