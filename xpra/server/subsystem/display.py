# This file is part of Xpra.
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from typing import Any
from subprocess import Popen
from collections.abc import Sequence

from xpra.os_util import gi_import, POSIX, OSX
from xpra.util.rectangle import rectangle

from xpra.util.objects import typedict
from xpra.util.screen import log_screen_sizes
from xpra.util.str_fn import bytestostr
from xpra.util.env import OSEnvContext, envint, SilenceWarningsContext
from xpra.exit_codes import ExitCode
from xpra.net.common import Packet
from xpra.util.system import is_X11
from xpra.util.version import parse_version, dict_version_trim
from xpra.scripts.config import FALSE_OPTIONS, TRUE_OPTIONS, InitExit
from xpra.common import (
    get_refresh_rate_for_value, parse_env_resolutions, parse_resolutions,
    BACKWARDS_COMPATIBLE, FULL_INFO,
)
from xpra.platform.gui import get_display_name, get_display_size
from xpra.server.subsystem.stub import StubServerMixin
from xpra.log import Logger

GLib = gi_import("GLib")

log = Logger("screen")
gllog = Logger("opengl")


def run_opengl_probe(cmd: list[str], env: dict[str, str], display_name: str):
    props: dict[str, Any] = {}
    try:
        # pylint: disable=import-outside-toplevel
        from subprocess import Popen, PIPE
        # we want the output so we can parse it:
        env["XPRA_REDIRECT_OUTPUT"] = "0"
        gllog(f"query_opengl() using {cmd=}, {env=}")
        proc = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env)
        out, err = proc.communicate()
        gllog("out(%s)=%s", cmd, out)
        gllog("err(%s)=%s", cmd, err)
        if proc.returncode == 0:
            # parse output:
            for line in out.splitlines():
                parts = bytestostr(line).split("=")
                if len(parts) != 2:
                    continue
                k = parts[0].strip()
                v = parts[1].strip()
                if k in ("GLX", "GLU.version", "opengl", "pyopengl", "accelerate", "shading-language-version"):
                    props[k] = parse_version(v)
                else:
                    props[k] = v
            gllog("opengl props=%s", props)
            if props:
                glprops = typedict(props)
                if glprops.strget("success").lower() in TRUE_OPTIONS:
                    gllog.info(f"OpenGL is supported on display {display_name!r}")
                    renderer = glprops.strget("renderer").split(";")[0]
                    if renderer:
                        gllog.info(f" using {renderer!r} renderer")
                else:
                    gllog.info("OpenGL is not supported on this display")
                    probe_err = glprops.strget("error")
                    if probe_err:
                        gllog.info(f" {probe_err}")
            else:
                gllog.info("No OpenGL information available")
        else:
            error = bytestostr(err).strip("\n\r")
            for x in str(err).splitlines():
                if x.startswith("RuntimeError: "):
                    error = x[len("RuntimeError: "):]
                    break
                if x.startswith("ImportError: "):
                    error = x[len("ImportError: "):]
                    break
            props["error"] = error
            log.warn("Warning: OpenGL support check failed:")
            log.warn(f" {error}")
    except Exception as e:
        gllog("query_opengl()", exc_info=True)
        gllog.error("Error: OpenGL support check failed")
        gllog.error(f" {e!r}")
        props["error"] = str(e)
    gllog("OpenGL: %s", props)
    return props


def load_opengl() -> dict[str, Any]:
    with OSEnvContext(XPRA_VERIFY_MAIN_THREAD="0"):
        try:
            # import OpenGL directly
            import OpenGL
            assert OpenGL
            gllog("found pyopengl version %s", OpenGL.__version__)
            # this may trigger an `AttributeError` if libGLX / libOpenGL are not installed:
            from OpenGL import GL
            assert GL
            gllog("loaded `GL` bindings: %s", GL)
        except (ImportError, AttributeError) as e:
            return {
                'error': f'OpenGL is not available: {e}',
                'success': False,
            }
        try:
            from xpra.opengl import backing
            assert backing
        except ImportError:
            return {
                'error': '`xpra.opengl` is not available',
                'success': False,
            }
    return {}


def check_xvfb(xvfb: Popen | None, timeout=0) -> bool:
    if xvfb is None:
        return True
    assert POSIX
    from xpra.x11.vfb_util import check_xvfb_process
    if not check_xvfb_process(xvfb, timeout=timeout):
        return False
    return True


def _get_root_int(prop: str) -> int:
    from xpra.x11.bindings.core import get_root_xid
    from xpra.x11.prop import prop_get
    try:
        xid = get_root_xid()
        return prop_get(xid, prop, "u32")
    except Exception:
        return 0


def _set_root_int(prop: str = "_XPRA_RANDR_EXACT_SIZE", i: int = 0) -> None:
    from xpra.x11.bindings.core import get_root_xid
    from xpra.x11.prop import prop_set
    xid = get_root_xid()
    prop_set(xid, prop, "u32", i)


def get_display_pid() -> int:
    # perhaps this is an upgrade from an older version?
    # try harder to find the pid:
    return _get_root_int("XPRA_XVFB_PID") or _get_root_int("_XPRA_SERVER_PID")


class DisplayManager(StubServerMixin):
    """
    Mixin for servers that handle displays.
    """
    DEFAULT_REFRESH_RATE = 0
    PREFIX = "display"

    def __init__(self):
        self.xvfb: Popen | None = None
        self.display = os.environ.get("DISPLAY", "")
        self.display_options = ""
        self.display_pid: int = 0
        self.randr_sizes_added: list[tuple[int, int]] = []
        self.initial_resolutions: Sequence[tuple[int, int, int]] = ()
        self.screen_size_changed_timer = 0
        self.randr = False
        self.randr_exact_size = False
        self.bell = False
        self.default_dpi = 96
        self.bit_depth = 24
        self.icc_profile = b""
        self.dpi = 0
        self.xdpi = 0
        self.ydpi = 0
        self.antialias: dict[str, Any] = {}
        self.double_click_time = -1
        self.double_click_distance = -1, -1
        self.opengl = "no"
        self.opengl_props: dict[str, Any] = {}
        self.refresh_rate = "auto"
        self.original_desktop_display = None

    def init(self, opts) -> None:
        self.init_display_pid()
        self.opengl = opts.opengl
        self.bell = opts.bell
        self.default_dpi = int(opts.dpi)
        self.refresh_rate = opts.refresh_rate
        onoff = sizes = opts.resize_display
        if opts.resize_display.find(":") > 0:
            # ie: "off:1080p"
            onoff, sizes = opts.resize_display.split(":", 1)
        try:
            self.initial_resolutions = parse_resolutions(sizes, opts.refresh_rate) or ()
        except ValueError:
            self.initial_resolutions = ()
        self.randr = onoff.lower() not in FALSE_OPTIONS
        self.randr_exact_size = False
        self.check_xvfb()

    def check_xvfb(self) -> None:
        if not check_xvfb(self.xvfb):
            raise InitExit(ExitCode.NO_DISPLAY, "xvfb process has terminated")

    def setup(self) -> None:
        self.check_xvfb()
        if is_X11():
            from xpra.x11.bindings.display_source import get_display_ptr, init_display_source
            if not get_display_ptr():
                init_display_source()
            from xpra.scripts.server import verify_display
            if not verify_display(xvfb=self.xvfb, display_name=self.display):
                raise InitExit(ExitCode.NO_DISPLAY, f"unable to access display {self.display!r}")
            self.session_files += [
                "xvfb.pid",
                "xauthority",
                "Xorg.log",
                "Xorg.log.old",
                "xorg.conf.d/*",
                "xorg.conf.d",
            ]
        from xpra.platform.gui import init as gui_init
        log("gui_init()")
        gui_init()
        self.check_xvfb()
        if not self.display_pid:
            self.display_pid = get_display_pid()
        self.bit_depth = self.get_display_bit_depth()
        if self.randr and is_X11():
            self.init_randr()
            self.set_initial_resolution()
            self.save_server_pid()
        GLib.idle_add(self.print_screen_info)

    def init_randr(self) -> None:
        from xpra.x11.error import xlog
        with xlog:
            from xpra.x11.bindings.randr import RandRBindings
            RandR = RandRBindings()
            if not RandR.has_randr():
                self.randr = False
            log("randr=%s", self.randr)
            if not self.randr:
                return
            # check the property first,
            # because we may be inheriting this display,
            # in which case the screen sizes list may be longer than 1
            eprop = _get_root_int("_XPRA_RANDR_EXACT_SIZE")
            log("_XPRA_RANDR_EXACT_SIZE=%s", eprop)
            self.randr_exact_size = eprop == 1 or RandR.get_version() >= (1, 6)
            if not self.randr_exact_size:
                # ugly hackish way of detecting Xvfb with randr,
                # assume that it has only one resolution pre-defined:
                sizes = RandR.get_xrr_screen_sizes()
                if len(sizes) == 1:
                    self.randr_exact_size = True
                    _set_root_int("_XPRA_RANDR_EXACT_SIZE",1)
                elif not sizes:
                    # xwayland?
                    self.randr = False
                    self.randr_exact_size = False
            log(f"randr enabled: {self.randr}, exact size={self.randr_exact_size}")
            if not self.randr:
                log.warn("Warning: no X11 RandR support on %r", os.environ.get("DISPLAY", ""))

    def set_initial_resolution(self) -> None:
        log(f"set_initial_resolution() randr={self.randr}, initial_resolutions={self.initial_resolutions}")
        if self.randr and self.initial_resolutions and is_X11():
            from xpra.x11.error import xlog
            from xpra.x11.vfb_util import set_initial_resolution
            DEFAULT_VFB_RESOLUTIONS = parse_env_resolutions(default_refresh_rate=self.refresh_rate)
            dpi = self.dpi or self.default_dpi
            resolutions = self.initial_resolutions or DEFAULT_VFB_RESOLUTIONS
            with xlog:
                set_initial_resolution(resolutions, dpi)

    def save_server_pid(self) -> None:
        from xpra.x11.error import xlog
        from xpra.x11.prop import prop_set
        with xlog:
            from xpra.x11.bindings.core import get_root_xid
            prop_set(get_root_xid(), "XPRA_SERVER_PID", "u32", os.getpid())

    def init_display_pid(self) -> None:
        pid = envint("XVFB_PID", 0)
        if not pid:
            log.info("xvfb pid not found")
        else:
            log.info(f"xvfb pid {pid}")
        self.display_pid = pid

    def cleanup(self) -> None:
        self.cancel_screen_size_changed_timer()

    def cancel_screen_size_changed_timer(self):
        ssct = self.screen_size_changed_timer
        if ssct:
            self.screen_size_changed_timer = 0
            GLib.source_remove(ssct)

    def late_cleanup(self, stop=True) -> None:
        if stop and POSIX:
            self.kill_display()
        elif self.display_pid:
            log.info("not cleaning up Xvfb %i", self.display_pid)

    def kill_display(self) -> None:
        if not self.display_pid:
            log("unable to kill display: no display pid")
            return
        from xpra.x11.vfb_util import kill_xvfb
        kill_xvfb(self.display_pid)

    def print_screen_info(self) -> None:
        for x in self.get_display_description().split("\n"):
            log.info(x)

    def get_display_description(self) -> str:
        # try the `get_display_name()` platform function first,
        # then the instance method, which may be overriden (see `GTKServer`)
        dinfo = get_display_name() or self.get_display_name()
        dtype = self.get_display_type()
        dinfo = f"{dtype} display {dinfo}"      #ie: "X11 display :0"
        size = self.get_display_size()
        if size:
            w, h = size
            dinfo += f" with display size {w}x{h}"
        bit_depth = self.get_display_bit_depth()
        if bit_depth:
            dinfo += f"\n with {bit_depth} bit colors"
        return dinfo

    def get_display_name(self) -> str:
        return get_display_name()

    def get_display_type(self) -> str:
        if POSIX and not OSX:
            from xpra.util.system import is_Wayland
            if is_Wayland():
                return "Wayland"
            return "X11"
        return "Main"

    def get_display_bit_depth(self) -> int:
        return 0

    def get_refresh_rate_for_value(self, invalue) -> int:
        return get_refresh_rate_for_value(self.refresh_rate, invalue)

    def parse_hello(self, ss, caps, send_ui: bool):
        if send_ui:
            self.parse_screen_info(ss)

    def add_new_client(self, ss, c: typedict, send_ui: bool, share_count: int) -> None:
        if not send_ui:
            return
        # a bit of explanation:
        # normally these things are synchronized using xsettings, which we handle already,
        # but non-posix clients have no such thing,
        # and we don't want to expose that as an interface
        # (it's not very nice, and it is very X11 specific)
        # also, clients may want to override what is in their xsettings..
        # so if the client specifies what it wants to use, we patch the xsettings with it
        # (the actual xsettings part is done in `update_all_server_settings` in the X11 specific subclasses)
        if share_count > 0:
            log.info("sharing with %s other client(s)", share_count)
            self.dpi = 0
            self.xdpi = 0
            self.ydpi = 0
            self.double_click_time = -1
            self.double_click_distance = -1, -1
            self.antialias = {}
        else:
            dpi_caps = c.get("dpi")
            if isinstance(dpi_caps, int):
                # legacy mode, ie: html5 client
                self.dpi = self.xpdi = self.ydpi = int(dpi_caps)
            else:
                tdpi = typedict(c.dictget("dpi") or {})
                self.dpi = tdpi.intget("", 0)
                self.xdpi = tdpi.intget("x", self.xdpi)
                self.ydpi = tdpi.intget("y", self.ydpi)
            self.double_click_time = c.intget("double_click.time", -1)
            self.double_click_distance = c.intpair("double_click.distance", (-1, -1))
            self.antialias = c.dictget("antialias", {})
        log("dpi=%s, dpi.x=%s, dpi.y=%s, antialias=%s",
            self.dpi, self.xdpi, self.ydpi, self.antialias)
        log("double-click time=%s, distance=%s", self.double_click_time, self.double_click_distance)
        # if we're not sharing, reset all the settings:
        reset = share_count == 0
        self.update_all_server_settings(reset)

    def last_client_exited(self) -> None:
        self.reset_icc_profile()

    def threaded_setup(self) -> None:
        self.opengl_props = self.query_opengl()

    def query_opengl(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        if self.opengl.lower() == "noprobe" or self.opengl.lower() in FALSE_OPTIONS:
            gllog("query_opengl() skipped because opengl=%s", self.opengl)
            return props
        err = load_opengl()
        if err:
            return err
        from xpra.platform.paths import get_xpra_command
        cmd = self.get_full_child_command(get_xpra_command() + ["opengl", "--opengl=force"])
        return run_opengl_probe(cmd, self.get_child_env(), self.display)

    def get_caps(self, source) -> dict[str, Any]:
        caps: dict[str, Any] = {
            "bell": self.bell,
        }
        if "display" in source.wants:
            root_size = self.get_display_size()
            if root_size:
                caps |= {
                    "actual_desktop_size": root_size,
                    "root_window_size": root_size,
                    "desktop_size": self._get_desktop_size_capability(source, *root_size),
                }
            name = get_display_name()
            if name:
                caps["name"] = name
        if FULL_INFO and self.opengl_props:
            caps["opengl"] = dict_version_trim(self.opengl_props)
        if not BACKWARDS_COMPATIBLE:
            return {"display": caps}
        caps["display"] = caps.get("name", "")
        return caps

    def get_server_features(self, source) -> dict[str, Any]:
        features: dict[str, Any] = {}
        if source and "display" in source.wants:
            max_size = self.get_max_screen_size()
            if max_size:
                features["max_desktop_size"] = max_size
            display = os.environ.get("DISPLAY", "")
            if display:
                features["display"] = display
        return features

    def get_ui_info(self, proto, client_uuids=None, *args) -> dict[str, Any]:
        max_size = self.get_max_screen_size()
        if max_size:
            return {"server": {"max_desktop_size": max_size}}
        return {}

    def get_info(self, _proto) -> dict[str, Any]:
        i = {
            "randr": self.randr,
            "bell": self.bell,
            "double-click": {
                "time": self.double_click_time,
                "distance": self.double_click_distance,
            },
            "dpi": {
                "default": self.default_dpi,
                "value": self.dpi,
                "x": self.xdpi,
                "y": self.ydpi,
            },
            "antialias": self.antialias,
            "depth": self.bit_depth,
            "refresh-rate": self.refresh_rate,
        }
        if self.display_pid:
            i["pid"] = self.display_pid
        if self.original_desktop_display:
            i["original-desktop-display"] = self.original_desktop_display
        if self.opengl_props:
            i["opengl"] = self.opengl_props
        return {
            "display": i,
        }

    def _process_set_bell(self, proto, packet: Packet) -> None:
        assert self.bell, "cannot toggle send_bell: the feature is disabled"
        ss = self.get_server_source(proto)
        if ss:
            ss.send_bell = packet.get_bool(1)

    ######################################################################
    # display / screen / root window:
    def get_display_size(self) -> tuple[int, int]:
        return get_display_size()

    def set_screen_geometry_attributes(self, w: int, h: int) -> None:
        # by default, use the screen as desktop area:
        self.set_desktop_geometry_attributes(w, h)

    def set_desktop_geometry_attributes(self, w: int, h: int) -> None:
        self.calculate_desktops()
        self.calculate_workarea(w, h)
        self.set_desktop_geometry(w, h)

    def parse_screen_info(self, ss) -> tuple[int, int]:
        return self.do_parse_screen_info(ss, ss.desktop_size)

    def do_parse_screen_info(self, ss, desktop_size) -> tuple[int, int]:
        log("do_parse_screen_info%s", (ss, desktop_size))
        dw, dh = None, None
        if desktop_size:
            try:
                dw, dh = desktop_size
                log.info(" client root window size is %sx%s", dw, dh)
                if ss.screen_sizes:
                    log_screen_sizes(dw, dh, ss.screen_sizes)
            except Exception:
                dw, dh = None, None
        best = self.configure_best_screen_size()
        if not best:
            return desktop_size
        sw, sh = best
        # we will tell the client about the size chosen in the hello we send back,
        # so record this size as the current server desktop size to avoid change notifications:
        ss.desktop_size_server = sw, sh
        # prefer desktop size, fallback to screen size:
        w = dw or sw
        h = dh or sh
        # clamp to max supported:
        max_size = self.get_max_screen_size()
        if max_size:
            maxw, maxh = max_size
            w = min(w, maxw)
            h = min(h, maxh)
        self.set_desktop_geometry_attributes(w, h)
        self.set_icc_profile()
        self.apply_refresh_rate(ss)
        log("configure_best_screen_size()=%s", (w, h))
        return w, h

    def set_icc_profile(self) -> None:
        log("set_icc_profile() not implemented")

    def reset_icc_profile(self) -> None:
        log("reset_icc_profile() not implemented")

    def _monitors_changed(self, screen) -> None:
        log(f"_monitors_changed({screen})")
        self.schedule_screen_changed(screen)

    def _screen_size_changed(self, screen) -> None:
        log(f"_screen_size_changed({screen})")
        self.schedule_screen_changed(screen)

    def schedule_screen_changed(self, screen):
        self.cancel_screen_size_changed_timer()
        self.screen_size_changed_timer = GLib.timeout_add(10, self.screen_size_changed, screen)

    def screen_size_changed(self, screen) -> bool:
        self.screen_size_changed_timer = 0
        self.do_screen_changed(screen)
        self.notify_screen_changed(screen)
        return False

    def do_screen_changed(self, screen) -> None:
        log("do_screen_changed(%s)", screen)
        self.screen_size_changed_timer = 0
        with SilenceWarningsContext(DeprecationWarning):
            w, h = screen.get_width(), screen.get_height()
        log("new screen dimensions: %ix%i", w, h)
        self.set_screen_geometry_attributes(w, h)

    def notify_screen_changed(self, screen) -> None:
        log("notify_screen_changed(%s)", screen)
        GLib.idle_add(self.send_updated_screen_size)

    def send_updated_screen_size(self) -> None:
        root_size = self.get_display_size()
        if not root_size:
            return
        root_w, root_h = root_size
        max_size = self.get_max_screen_size()
        if not max_size:
            return
        max_w, max_h = max_size
        root_w = min(root_w, max_w)
        root_h = min(root_h, max_h)
        count = 0
        for ss in self._server_sources.values():
            if ss.updated_desktop_size(root_w, root_h, max_w, max_h):
                count += 1
        if count > 0:
            log.info("sent updated screen size to %s clients: %sx%s (max %sx%s)",
                     count, root_w, root_h, max_w, max_h)

    def get_max_screen_size(self) -> tuple[int, int]:
        return self.get_display_size()

    def _get_desktop_size_capability(self, server_source, root_w: int, root_h: int) -> tuple[int, int]:
        client_size = server_source.desktop_size
        log("client resolution is %s, current server resolution is %sx%s", client_size, root_w, root_h)
        if not client_size:
            # client did not specify size, just return what we have
            return root_w, root_h
        client_w, client_h = client_size
        w = min(client_w, root_w)
        h = min(client_h, root_h)
        return w, h

    def configure_best_screen_size(self) -> tuple[int, int]:
        return self.get_display_size()

    def apply_refresh_rate(self, ss) -> int:
        rrate = self.get_client_refresh_rate(ss)
        log(f"apply_refresh_rate({ss}) rate={rrate}")
        if rrate > 0:
            self.set_window_refresh_rate(ss, rrate)
        return rrate

    def set_window_refresh_rate(self, ss, rrate: int):
        if hasattr(ss, "default_batch_config"):
            ss.default_batch_config.match_vrefresh(rrate)
        if hasattr(ss, "global_batch_config"):
            ss.global_batch_config.match_vrefresh(rrate)
        if hasattr(ss, "all_window_sources"):
            for window_source in ss.all_window_sources():
                bc = window_source.batch_config
                if bc:
                    bc.match_vrefresh(rrate)

    def get_client_refresh_rate(self, ss) -> int:
        vrefresh = []
        # use the refresh-rate value from the monitors
        # (value is pre-multiplied by 1000!)
        if ss.monitors:
            for mdef in ss.monitors.values():
                v = mdef.get("refresh-rate", 0)
                if v:
                    vrefresh.append(v)
        if not vrefresh and getattr(ss, "vrefresh", 0) > 0:
            vrefresh.append(ss.vrefresh * 1000)
        if not vrefresh:
            vrefresh.append(self.DEFAULT_REFRESH_RATE)
        rrate = 0
        if vrefresh:
            rrate = min(vrefresh)
            if self.refresh_rate:
                rrate = get_refresh_rate_for_value(self.refresh_rate, rrate, multiplier=1000)
            rrate //= 1000
        log("get_client_refresh_rate(%s)=%s (from %s)", ss, rrate, vrefresh)
        return rrate

    def _process_desktop_size(self, proto, packet: Packet) -> None:
        log("new desktop size from %s: %s", proto, packet)
        ss = self.get_server_source(proto)
        if ss is None:
            return
        width = packet.get_u16(1)
        height = packet.get_u16(2)
        ss.desktop_size = (width, height)
        if len(packet) >= 12:
            ss.set_monitors(packet[11])
        elif len(packet) >= 11:
            # fallback to the older global attribute:
            v = packet[10]
            if 0 < v < 240 and hasattr(ss, "vrefresh") and getattr(ss, "vrefresh") != v:
                ss.vrefresh = v
        if len(packet) >= 10:
            # added in 0.16 for scaled client displays:
            xdpi = packet.get_u16(8)
            ydpi = packet.get_u16(9)
            if xdpi != self.xdpi or ydpi != self.ydpi:
                self.xdpi, self.ydpi = xdpi, ydpi
                log("new dpi: %ix%i", self.xdpi, self.ydpi)
                self.dpi = round((self.xdpi + self.ydpi) / 2)
                self.dpi_changed()
        if len(packet) >= 8:
            # added in 0.16 for scaled client displays:
            dsw = packet.get_u16(6)
            dsh = packet.get_u16(7)
            ss.desktop_size_unscaled = (dsw, dsh)
        if len(packet) >= 6:
            desktops = packet.get_u8(4)
            desktop_names = packet.get_strs(5)
            ss.set_desktops(desktops, desktop_names)
            self.calculate_desktops()
        if len(packet) >= 4:
            ss.set_screen_sizes(packet[3])
        log("client requesting new size: %sx%s", width, height)
        self.set_screen_size(width, height)
        self.set_desktop_geometry_attributes(width, height)
        if len(packet) >= 4:
            log.info("received updated display dimensions")
            log.info("client display size is %sx%s", width, height)
            log_screen_sizes(width, height, ss.screen_sizes)
            self.calculate_workarea(width, height)
        self.apply_refresh_rate(ss)
        # ensures that DPI and antialias information gets reset:
        self.update_all_server_settings()

    def _process_configure_display(self, proto, packet: Packet) -> None:
        ss = self.get_server_source(proto)
        if ss is None:
            return
        attrs = typedict(packet.get_dict(1))
        desktop_size = attrs.intpair("desktop-size")
        if desktop_size:
            ss.desktop_size = desktop_size
        desktop_size_unscaled = attrs.intpair("desktop-size-unscaled")
        if desktop_size_unscaled:
            ss.desktop_size_unscaled = desktop_size_unscaled
        # vrefresh may be overridden in 'monitors' data:
        vrefresh = attrs.intget("vrefresh")
        if 0 < vrefresh < 240 and hasattr(ss, "vrefresh") and getattr(ss, "vrefresh") != vrefresh:
            ss.vrefresh = vrefresh
        monitors = attrs.dictget("monitors")
        if monitors:
            ss.set_monitors(monitors)
        if desktop_size:
            width, height = desktop_size
            log("client requesting new size: %sx%s", width, height)
            self.set_screen_size(width, height)
            log.info("received updated display dimensions")
            log.info(f"client display size is {width}x{height}")
            log_screen_sizes(width, height, ss.screen_sizes)
            self.calculate_workarea(width, height)
            self.set_desktop_geometry_attributes(width, height)
        # DPI
        dpi = 0
        dpi_caps = attrs.get("dpi")
        # unprefixed legacy mode:
        if isinstance(dpi_caps, int):
            dpi = int(dpi_caps)
        dpix = attrs.intget("dpi.x", dpi)
        dpiy = attrs.intget("dpi.y", dpi)
        # namespaced caps:
        if isinstance(dpi_caps, dict):
            tdpi = typedict(dpi_caps)
            dpix = tdpi.intget("x", dpix)
            dpiy = tdpi.intget("y", dpiy)
        if dpix and dpiy and (dpix != self.xdpi or dpiy != self.ydpi):
            self.xdpi, self.ydpi = dpix, dpiy
            log("new dpi: %ix%i", dpix, dpiy)
            self.dpi = round((dpix + dpiy) / 2)
            self.dpi_changed()
        desktop_names = attrs.strtupleget("desktop-names")
        if desktop_names:
            ss.set_desktops(attrs.intget("desktops", len(desktop_names)), desktop_names)
            self.calculate_desktops()
        iccd = attrs.dictget("icc")
        if iccd:
            iccd = typedict(iccd)
            ss.icc = iccd.get("global", ss.icc)
            ss.display_icc = iccd.get("display", ss.display_icc)
            self.set_icc_profile()
        self.apply_refresh_rate(ss)
        # ensures that DPI and antialias information gets reset:
        self.update_all_server_settings()

    def dpi_changed(self) -> None:
        """
        The x11 servers override this method
        to also update the XSettings.
        """

    def calculate_desktops(self):
        """ seamless servers can update the desktops """

    def calculate_workarea(self, maxw: int, maxh: int) -> None:
        log("calculate_workarea(%s, %s)", maxw, maxh)
        workarea = rectangle(0, 0, maxw, maxh)
        for ss in self._server_sources.values():
            screen_sizes = ss.screen_sizes
            log("calculate_workarea() screen_sizes(%s)=%s", ss, screen_sizes)
            if not screen_sizes:
                continue
            for display in screen_sizes:
                # avoid error with old/broken clients:
                if not display or not isinstance(display, (list, tuple)):
                    continue
                # display: [':0.0', 2560, 1600, 677, 423, [['DFP2', 0, 0, 2560, 1600, 646, 406]], 0, 0, 2560, 1574]
                if len(display) >= 10:
                    work_x, work_y, work_w, work_h = display[6:10]
                    display_workarea = rectangle(work_x, work_y, work_w, work_h)
                    log("calculate_workarea() found %s for display %s", display_workarea, display[0])
                    workarea = workarea.intersection_rect(display_workarea)
                    if not workarea:
                        log.warn("Warning: failed to calculate workarea")
                        log.warn(" as intersection of %s and %s", (maxw, maxh), (work_x, work_y, work_w, work_h))
        # sanity checks:
        log("calculate_workarea(%s, %s) workarea=%s", maxw, maxh, workarea)
        max_dim = 32768 - 8192
        if workarea.width == 0 or workarea.height == 0 or workarea.width >= max_dim or workarea.height >= max_dim:
            log.warn("Warning: failed to calculate a common workarea")
            log.warn(f" using the full display area: {maxw}x{maxh}")
            workarea = rectangle(0, 0, maxw, maxh)
        self.set_workarea(workarea)

    def set_workarea(self, workarea) -> None:
        """ overridden by seamless servers """

    ######################################################################
    # screenshots:
    def _process_screenshot(self, proto, _packet: Packet) -> None:
        packet = self.make_screenshot_packet()
        ss = self.get_server_source(proto)
        if packet and ss:
            ss.send(*packet)

    def make_screenshot_packet(self) -> Packet:
        with log.trap_error("Error making screenshot packet"):
            return self.do_make_screenshot_packet()

    def do_make_screenshot_packet(self) -> Packet:
        raise NotImplementedError("no screenshot capability in %s" % type(self))

    def send_screenshot(self, proto) -> None:
        # this is a screenshot request, handle it and disconnect
        try:
            packet = self.make_screenshot_packet()
            if not packet:
                self.send_disconnect(proto, "screenshot failed")
                return
            proto.send_now(packet)
            GLib.timeout_add(5 * 1000, self.send_disconnect, proto, "screenshot sent")
        except Exception as e:
            log.error("failed to capture screenshot", exc_info=True)
            self.send_disconnect(proto, "screenshot failed: %s" % e)

    def init_packet_handlers(self) -> None:
        self.add_packets("set-bell", "desktop_size", "configure-display", "screenshot", main_thread=True)
