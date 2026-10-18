"""Wayland-native screen capture via xdg-desktop-portal ScreenCast + PipeWire.

mss cannot read the screen on a Wayland compositor (X11 apps get black frames),
so on Linux/Wayland we go through the portal: a one-time permission dialog whose
choice is remembered via a persistent restore_token, then a PipeWire stream we
read with GStreamer's pipewiresrc into BGR numpy frames.

GStreamer / PyGObject (`gi`) are distro packages, not pip wheels, so they aren't
in the venv. We import `gi` and, if that fails, add the system site-packages
(same Python minor) before retrying — keeping the venv otherwise pure.
"""

from __future__ import annotations

import os
import secrets
import sys
import sysconfig
from pathlib import Path

import numpy as np

from aldur_appraiser.config import config_dir


def _debug(msg: str) -> None:
    if os.environ.get("ALDUR_PORTAL_DEBUG"):
        print(f"[portal] {msg}", file=sys.stderr, flush=True)


def _ensure_gi() -> None:
    """Make the distro's PyGObject/GStreamer importable from inside the venv."""
    try:
        import gi  # noqa: F401

        return
    except ImportError:
        pass
    # Append the system interpreter's site-packages for the matching minor.
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        Path(sysconfig.get_path("purelib").replace(sys.prefix, "/usr")),
        Path(f"/usr/lib64/python{ver}/site-packages"),
        Path(f"/usr/lib/python{ver}/site-packages"),
    ]
    for p in candidates:
        if p.exists() and str(p) not in sys.path:
            sys.path.append(str(p))
    import gi  # noqa: F401  # re-raise with a clear message if still missing


class PortalError(RuntimeError):
    pass


def _token_path() -> Path:
    return config_dir() / "screencast_restore_token"


def _restore_disabled_path() -> Path:
    return config_dir() / "screencast_no_restore"


# In-memory only: if a restore token stalls we skip it for the rest of THIS
# process, but try again next launch — modern KDE/Plasma 6 handles restore fine,
# so a one-off hiccup must not permanently force the screen-picker dialog every
# run (which is what the old persistent flag did).
_RESTORE_DISABLED = False


def _restore_disabled() -> bool:
    """True if a restore token stalled earlier in this process (not persisted).
    A stale persistent flag from older versions also counts, but is cleared so
    the next launch retries restore."""
    global _RESTORE_DISABLED
    if _restore_disabled_path().exists():
        _restore_disabled_path().unlink(missing_ok=True)  # heal the old sticky flag
        _RESTORE_DISABLED = True
    return _RESTORE_DISABLED


def _disable_restore() -> None:
    global _RESTORE_DISABLED
    _RESTORE_DISABLED = True


def _read_restore_token() -> str | None:
    p = _token_path()
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def _write_restore_token(token: str) -> None:
    if token:
        _token_path().write_text(token, encoding="utf-8")


class PortalScreenCast:
    """Owns the portal session + GStreamer pipeline; yields BGR frames.

    Interface mirrors ScreenCapture (grab/assert_capturable/close) so app.py is
    backend-agnostic. The portal hands us a whole output; region cropping is done
    on the numpy frame, same as the mss path.
    """

    def __init__(self):
        _ensure_gi()
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        # Importing GstApp registers the AppSink type so try_pull_sample binds
        # on the element returned by get_by_name().
        from gi.repository import Gio, GLib, Gst, GstApp

        self._Gio = Gio
        self._GLib = GLib
        self._Gst = Gst
        self._GstApp = GstApp
        Gst.init(None)

        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
        self._session_handle: str | None = None
        self._pipeline = None
        self._appsink = None

    # --- portal D-Bus handshake ---------------------------------------------

    def _new_token(self) -> str:
        return "aldur_" + secrets.token_hex(8)

    def _request(self, method: str, args, options: dict, *, timeout_s: int = 60):
        """Call a ScreenCast method and block until its Response signal fires.

        Returns the results dict from the org.freedesktop.portal.Request.Response
        signal (response code 0 = success).
        """
        _debug(f"{method}: calling (timeout {timeout_s}s)")
        GLib = self._GLib
        Gio = self._Gio

        handle_token = self._new_token()
        options["handle_token"] = GLib.Variant("s", handle_token)
        request_path = (
            f"/org/freedesktop/portal/desktop/request/{self._sender}/{handle_token}"
        )

        loop = GLib.MainLoop()
        result: dict = {}

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            code, results = params.unpack()
            result["code"] = code
            result["results"] = results
            loop.quit()

        sub_id = self._bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            request_path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
        )
        try:
            full_args = tuple(args) + (options,)
            variant = self._pack_args(method, full_args)
            self._bus.call_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.ScreenCast",
                method,
                variant,
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            GLib.timeout_add_seconds(timeout_s, loop.quit)
            loop.run()
        finally:
            self._bus.signal_unsubscribe(sub_id)

        if "code" not in result:
            raise PortalError(
                f"{method}: timed out after {timeout_s}s waiting for the portal. "
                "If a screen-share dialog appeared, approve it (alt-tab out of the game)."
            )
        if result["code"] != 0:
            # 1 = user cancelled, 2 = ended; anything non-zero is a failure here.
            raise PortalError(f"{method}: portal request failed (code {result['code']})")
        _debug(f"{method}: response ok")
        return result["results"]

    def _pack_args(self, method: str, args: tuple):
        """Build the GLib.Variant tuple for each ScreenCast method signature."""
        GLib = self._GLib
        if method == "CreateSession":
            return GLib.Variant("(a{sv})", args)
        if method == "SelectSources":
            return GLib.Variant("(oa{sv})", args)
        if method == "Start":
            return GLib.Variant("(osa{sv})", args)
        raise PortalError(f"unknown method {method}")

    def connect(self) -> None:
        """Run (or restore) the portal session and start the PipeWire pipeline."""
        # Skip the restore token entirely once we've learned it stalls here
        # (KDE) — avoids a doomed ~3s attempt and a delayed dialog every run.
        token = None if _restore_disabled() else _read_restore_token()
        try:
            self._handshake(token)
        except PortalError as exc:
            if token is not None:
                _debug(f"restore token failed ({exc}); disabling restore + retrying fresh")
                _disable_restore()
                _token_path().unlink(missing_ok=True)
                self._reset_session()
                self._handshake(None)
            else:
                raise

    def _reset_session(self) -> None:
        self._session_handle = None
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None

    def _handshake(self, token: str | None) -> None:
        GLib = self._GLib

        _debug("CreateSession")
        res = self._request(
            "CreateSession",
            (),
            {"session_handle_token": GLib.Variant("s", self._new_token())},
            timeout_s=30,
        )
        self._session_handle = res["session_handle"]

        select_opts = {
            "types": GLib.Variant("u", 1),          # 1 = MONITOR
            "cursor_mode": GLib.Variant("u", 1),     # 1 = hidden
            "persist_mode": GLib.Variant("u", 2),    # 2 = persist until revoked
        }
        if token:
            select_opts["restore_token"] = GLib.Variant("s", token)
        _debug(f"SelectSources (restore_token={'yes' if token else 'no'})")
        self._request("SelectSources", (self._session_handle,), select_opts, timeout_s=30)

        # Start may show the interactive dialog on first run -> allow longer.
        _debug("Start")
        start_res = self._request(
            "Start", (self._session_handle, ""), {}, timeout_s=180
        )
        if start_res.get("restore_token") and not _restore_disabled():
            _write_restore_token(start_res["restore_token"])

        streams = start_res.get("streams") or []
        if not streams:
            raise PortalError("portal returned no streams")
        node_id = streams[0][0]
        _debug(f"stream node id={node_id}")

        fd = self._open_pipewire_remote()
        _debug(f"pipewire fd={fd}")
        self._build_pipeline(fd, node_id)

    def _open_pipewire_remote(self) -> int:
        Gio = self._Gio
        GLib = self._GLib
        ret, fd_list = self._bus.call_with_unix_fd_list_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self._session_handle, {})),
            GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )
        fd_index = ret.unpack()[0]
        return fd_list.get(fd_index)

    # --- GStreamer pipeline --------------------------------------------------

    def _build_pipeline(self, fd: int, node_id: int) -> None:
        Gst = self._Gst
        desc = (
            f"pipewiresrc fd={fd} path={node_id} always-copy=true ! "
            "videoconvert ! video/x-raw,format=BGR ! "
            "appsink name=sink max-buffers=1 drop=true sync=false enable-last-sample=true"
        )
        self._pipeline = Gst.parse_launch(desc)
        self._appsink = self._pipeline.get_by_name("sink")
        self._pipeline.set_state(Gst.State.PLAYING)
        # A healthy pipeline reaches PLAYING almost instantly (in-game the screen
        # is always changing). If it's still PAUSED after a few seconds,
        # pipewiresrc got no data — typically a stale KDE restore token.
        ret, state, _ = self._pipeline.get_state(self._Gst.SECOND * 3)
        _debug(f"pipeline state change: ret={ret.value_nick} state={state.value_nick}")
        if state != Gst.State.PLAYING:
            raise PortalError(
                f"capture pipeline stalled in {state.value_nick}: {self._drain_bus_error()}"
            )
        # Prime so last-sample is populated for the first grab.
        if self._appsink.try_pull_sample(self._Gst.SECOND * 5) is None:
            _debug("prime pull returned no sample (may still arrive via last-sample)")

    def _drain_bus_error(self) -> str:
        """Pop any pending error/warning off the bus for a useful message."""
        Gst = self._Gst
        bus = self._pipeline.get_bus()
        msgs = []
        while True:
            msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING)
            if msg is None:
                break
            if msg.type == Gst.MessageType.ERROR:
                err, _dbg = msg.parse_error()
                msgs.append(f"error: {err.message}")
            else:
                err, _dbg = msg.parse_warning()
                msgs.append(f"warning: {err.message}")
        return "; ".join(msgs) or "no bus error reported"

    def grab(self, region=None) -> np.ndarray:
        """Return the most recent frame as BGR; crop to region if given.

        The portal stream is damage-based: on a static screen no *new* buffers
        arrive, so we read the appsink's retained last-sample (the current
        screen) rather than blocking for a fresh one. A short pull keeps it
        current when the screen is changing.
        """
        if self._appsink is None:
            raise PortalError("not connected; call connect() first")
        fresh = self._appsink.try_pull_sample(self._Gst.MSECOND * 50)
        sample = fresh or self._appsink.get_property("last-sample")
        if sample is None:
            raise PortalError("no frame available from PipeWire stream")
        frame = self._sample_to_bgr(sample)
        if region is not None:
            frame = frame[
                region.top : region.top + region.height,
                region.left : region.left + region.width,
            ]
        return frame

    def _sample_to_bgr(self, sample) -> np.ndarray:
        caps = sample.get_caps().get_structure(0)
        w = caps.get_value("width")
        h = caps.get_value("height")
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(self._Gst.MapFlags.READ)
        if not ok:
            raise PortalError("failed to map GStreamer buffer")
        try:
            # BGR -> 3 bytes/px; rows may be padded, so derive stride.
            stride = mapinfo.size // h
            arr = np.frombuffer(mapinfo.data, dtype=np.uint8, count=h * stride)
            arr = arr.reshape(h, stride)[:, : w * 3].reshape(h, w, 3)
            return np.ascontiguousarray(arr)
        finally:
            buf.unmap(mapinfo)

    def assert_capturable(self) -> None:
        frame = self.grab()
        if frame.size == 0 or int(frame.max()) == 0:
            raise PortalError("portal capture returned an empty/black frame")

    def close(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._pipeline = None
        # Also close the portal session, otherwise the screencast stays "active"
        # from the compositor's view and keeps disturbing other monitors.
        if self._session_handle is not None:
            try:
                self._bus.call_sync(
                    "org.freedesktop.portal.Desktop",
                    self._session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    self._Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except Exception:  # noqa: BLE001 - best effort teardown
                pass
            self._session_handle = None

    def __enter__(self) -> PortalScreenCast:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
