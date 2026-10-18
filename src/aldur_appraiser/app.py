"""Orchestration loop: capture -> detect -> OCR -> price -> render.

The per-frame work is split into AppraiserLoop.step() (pure, testable with a
fed frame) and run() (the timed capture loop). Rendering goes through callbacks
so the same loop drives the console now and an overlay later.

A frame-diff gate avoids re-running OCR (and re-rendering) while the panel
content is unchanged, which keeps CPU low at the poll rate.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from aldur_appraiser.pipeline import Appraisal, appraise_roi_rows, rows_to_result
from aldur_appraiser.pricing.client import PriceTable
from aldur_appraiser.pricing.valuation import EvalResult, divine_rate, format_value
from aldur_appraiser.vision import ocr
from aldur_appraiser.vision.detect import PanelDetector

# ROI is downsampled to this many cells per side for the change signature.
_SIG_SIZE = 24
_SIG_EPS = 4.0  # mean abs per-cell delta above which the ROI is "changed"


def _signature(roi: np.ndarray) -> np.ndarray:
    import cv2

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi
    return cv2.resize(gray, (_SIG_SIZE, _SIG_SIZE), interpolation=cv2.INTER_AREA).astype(np.float32)


def _changed(a: np.ndarray | None, b: np.ndarray) -> bool:
    return a is None or a.shape != b.shape or float(np.abs(a - b).mean()) > _SIG_EPS


@dataclass
class AppraiserLoop:
    prices: PriceTable
    detector: PanelDetector
    engine: ocr.OcrEngine
    # on_result(appraisal): carries the ranked result (corner/console), per-row
    # data (inline overlay), and the anchor mapping ROI pixels to the screen.
    on_result: Callable[[Appraisal], None]
    on_hide: Callable[[], None]
    # on_busy(anchor): fired when a (changed) panel is detected and OCR is about
    # to run, so the overlay can show a spinner during the ~OCR delay.
    on_busy: Callable[[tuple], None] = lambda _anchor: None
    score_cutoff: int = 80

    _last_sig: np.ndarray | None = None
    _panel_visible: bool = False

    def step(self, frame: np.ndarray) -> EvalResult | None:
        """Process one frame. Returns the result if (re)evaluated, else None."""
        panel = self.detector.find_panel(frame)
        if panel is None:
            if self._panel_visible:
                self._panel_visible = False
                self._last_sig = None
                self.on_hide()
            return None

        rect = panel.reward_roi()
        roi = self.detector.reward_image(frame, panel)
        sig = _signature(roi)
        if not _changed(self._last_sig, sig):
            return None  # panel open but unchanged -> skip OCR

        self._last_sig = sig
        self._panel_visible = True
        anchor = (rect.left, rect.top, rect.width, rect.height, frame.shape[1], frame.shape[0])
        self.on_busy(anchor)  # "appraising…" spinner before the (slow) OCR
        rows = appraise_roi_rows(
            roi, self.prices, engine=self.engine, score_cutoff=self.score_cutoff
        )
        result = rows_to_result(rows)
        self.on_result(Appraisal(result=result, rows=rows, anchor=anchor))
        return result

    def run(self, capture, *, poll_fps: float = 3.0, region=None) -> None:
        """Timed capture loop until interrupted."""
        interval = 1.0 / poll_fps
        while True:
            t0 = time.monotonic()
            frame = capture.grab(region)
            self.step(frame)
            dt = time.monotonic() - t0
            if dt < interval:
                time.sleep(interval - dt)


# --- console rendering -------------------------------------------------------


def _fmt(total: float, base: str, dr: float | None) -> str:
    val, unit = format_value(total, dr, base_unit=base)
    return f"{val:.2f} {unit}"


def render_console(
    result: EvalResult, *, base: str, stale: bool = False, dr: float | None = None
) -> str:
    if not result.items:
        return "Panel open — no reward options recognised."
    lines = [f"Reward ranking (base={base}){' [STALE PRICES]' if stale else ''}:"]
    for v in result.items:
        marker = "  <-- BEST" if v.is_best else ""
        if v.known:
            lines.append(f"  {v.qty}x {v.name:<26} {_fmt(v.total, base, dr):>14}{marker}")
        else:
            lines.append(f"  {v.qty}x {v.name:<26} {'unknown':>14}{marker}")
    if result.incomplete:
        lines.append("  (comparison incomplete: an option has no market price)")
    for v in result.bonus_items:
        val = _fmt(v.total, base, dr) if v.known else "unknown"
        lines.append(f"  + bonus (always paid): {v.qty}x {v.name} — {val}")
    return "\n".join(lines)


@dataclass
class _Setup:
    pc: object
    cached: object
    poll_fps: float
    backend: str


def _prepare(backend: str | None, *, refresh: bool = False) -> _Setup:
    from aldur_appraiser.config import load_config
    from aldur_appraiser.pricing.cache import get_or_fetch
    from aldur_appraiser.vision.capture import default_backend

    cfg = load_config()
    pc = cfg.pricing
    cached = get_or_fetch(
        pc.league,
        pc.base,
        ttl_minutes=0 if refresh else pc.cache_ttl_minutes,
        realm=pc.realm,
        categories=pc.categories,
    )
    poll_fps = float(cfg.raw.get("vision", {}).get("poll_fps", 3))
    return _Setup(pc=pc, cached=cached, poll_fps=poll_fps, backend=backend or default_backend())


def _make_loop(s: _Setup, on_result, on_hide, on_busy=lambda _a: None) -> AppraiserLoop:
    return AppraiserLoop(
        prices=s.cached.table,
        detector=PanelDetector(),
        engine=ocr.get_engine(),
        on_result=on_result,
        on_hide=on_hide,
        on_busy=on_busy,
    )


def run_console(*, backend: str | None = None, refresh: bool = False) -> int:
    from aldur_appraiser.vision.capture import open_capture

    s = _prepare(backend, refresh=refresh)
    dr = divine_rate(s.cached.table)

    def _print(appraisal):
        print(render_console(appraisal.result, base=s.pc.base, stale=s.cached.stale, dr=dr) + "\n")

    loop = _make_loop(s, on_result=_print, on_hide=lambda: print("(panel closed)\n"))
    print(f"aldur-appraiser running (backend={s.backend}, league={s.pc.league}, base={s.pc.base}).")
    print("Open a Runeshape Combinations panel in-game. Ctrl+C to stop.\n")
    try:
        with open_capture(backend=s.backend) as cap:
            if hasattr(cap, "assert_capturable"):
                cap.assert_capturable()
            loop.run(cap, poll_fps=s.poll_fps)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def _tray_icon(icon_path):
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

    if icon_path is not None:
        return QIcon(str(icon_path))
    pm = QPixmap(32, 32)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#d9c89a"))
    p.setPen(QColor("#5a4a2a"))
    p.drawEllipse(3, 3, 26, 26)
    p.end()
    return QIcon(pm)


def run_overlay(*, backend: str | None = None, style: str = "corner", refresh: bool = False) -> int:
    """Tray-first app: launch shows only a tray icon; the user starts the
    Price-Checker and/or opens the Temple Planner from its menu."""
    import os
    import signal
    import subprocess
    import threading

    # opencv-python hijacks QT_QPA_PLATFORM_PLUGIN_PATH (breaks the xcb plugin);
    # drop it. On Wayland xcb honors always-on-top/click-through more reliably.
    if "cv2" in os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH", ""):
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    from PySide6.QtCore import QObject, QTimer, QUrl, Signal
    from PySide6.QtGui import QAction, QCursor, QDesktopServices, QIcon
    from PySide6.QtWidgets import QApplication, QInputDialog, QMenu, QSystemTrayIcon

    from aldur_appraiser import __version__, updates
    from aldur_appraiser import settings as user_settings
    from aldur_appraiser.desktop_hotkey import accel_to_portal, bind_gnome_shortcut
    from aldur_appraiser.resources import resource_path

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)  # live in the tray, not tied to a window
    app_icon = resource_path("assets/icon.png")
    app_icon = str(app_icon) if app_icon.exists() else None
    if app_icon:
        app.setWindowIcon(QIcon(app_icon))

    class _Bridge(QObject):
        error = Signal(str)
        update = Signal(str)     # a newer release is available
        uptodate = Signal(str)   # manual check: already current
        trigger = Signal()       # appraise-now request (hotkey / tray), GUI thread
        shown = Signal()         # a result was shown -> (re)start the auto-hide timer

    bridge = _Bridge()
    # price-checker state (lazy). `cap` is a capture session kept open while the
    # checker runs so Wayland's portal only prompts once, not on every check.
    pc: dict = {"built": False, "enabled": False, "auto": False, "cap": None}

    def _build_pricechecker() -> None:
        """Load prices + build the overlay/capture pipeline on first use."""
        from aldur_appraiser.icons import currency_icon_paths
        from aldur_appraiser.vision.capture import open_capture
        from aldur_appraiser.vision.overlay import build_inline_overlay, build_overlay

        s = _prepare(backend, refresh=refresh)
        stale = s.cached.stale
        dr = divine_rate(s.cached.table)
        if style == "inline":
            icons = currency_icon_paths(s.pc.realm, s.pc.league)
            overlay = build_inline_overlay(s.pc.base, icon_paths=icons, divine_rate=dr)
            on_result = lambda a: overlay.post_rows(a.rows, a.anchor)  # noqa: E731
            on_busy = overlay.post_busy
        else:
            overlay = build_overlay(s.pc.base)
            on_result = lambda a: overlay.post_result(a.result, stale, a.anchor, dr)  # noqa: E731
            on_busy = lambda _a: None  # noqa: E731
        loop = _make_loop(s, on_result=on_result, on_hide=overlay.post_hide, on_busy=on_busy)
        busy = threading.Event()

        def acquire_cap():
            """Open the capture session once and keep it. Reusing one session means
            the Wayland portal only shows its screen-share dialog on the first
            grab, not on every appraisal."""
            if pc.get("cap") is None:
                cap = open_capture(backend=s.backend)
                if hasattr(cap, "connect"):
                    cap.connect()  # portal backend: run the D-Bus handshake now
                if hasattr(cap, "assert_capturable"):
                    cap.assert_capturable()
                pc["cap"] = cap
            return pc["cap"]

        def release_cap() -> None:
            cap = pc.get("cap")
            pc["cap"] = None
            if cap is not None:
                try:
                    cap.close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass

        def appraise_once() -> None:
            if not pc["enabled"] or pc.get("auto") or busy.is_set():
                return
            busy.set()

            def work() -> None:
                try:
                    cap = acquire_cap()
                    loop._last_sig = None  # force a fresh evaluation each request
                    if loop.step(cap.grab()) is None:
                        overlay.post_hide()
                    else:
                        bridge.shown.emit()
                except Exception as exc:  # noqa: BLE001 - surface to the tray, keep running
                    print(f"capture error: {type(exc).__name__}: {exc}", file=sys.stderr)
                    bridge.error.emit(f"{type(exc).__name__}: {exc}")
                    release_cap()  # drop a broken session so the next try reconnects
                finally:
                    busy.clear()

            threading.Thread(target=work, daemon=True).start()

        def auto_worker(stop: threading.Event) -> None:
            """Continuous appraisal loop (the pre-hotkey 'auto' behaviour): keep
            grabbing and re-evaluate whenever the reward panel's content changes."""
            interval = 1.0 / max(0.5, s.poll_fps)
            try:
                cap = acquire_cap()
            except Exception as exc:  # noqa: BLE001
                print(f"capture error: {type(exc).__name__}: {exc}", file=sys.stderr)
                bridge.error.emit(f"{type(exc).__name__}: {exc}")
                return
            loop._last_sig = None
            while not stop.is_set():
                t0 = time.monotonic()
                try:
                    loop.step(cap.grab())
                except Exception as exc:  # noqa: BLE001
                    print(f"capture error: {type(exc).__name__}: {exc}", file=sys.stderr)
                    bridge.error.emit(f"{type(exc).__name__}: {exc}")
                    release_cap()
                    break
                stop.wait(max(0.0, interval - (time.monotonic() - t0)))

        hide_ms = int(os.environ.get("ALDUR_HIDE_MS", "12000"))
        ht = QTimer()
        ht.setSingleShot(True)
        ht.timeout.connect(overlay.post_hide)
        if hide_ms > 0:
            # Auto mode keeps a result up until the panel actually closes; only the
            # on-demand (hotkey/menu) result auto-hides after the timeout.
            bridge.shown.connect(lambda: None if pc.get("auto") else ht.start(hide_ms))
        pc.update(built=True, appraise_once=appraise_once, overlay=overlay,
                  hide_timer=ht, release_cap=release_cap, auto_worker=auto_worker)

    def _set_price_enabled(on: bool) -> None:
        if on and not pc["built"]:
            _build_pricechecker()
        pc["enabled"] = on
        if not on:
            _set_auto(False)  # stopping the checker also stops auto mode
            if pc.get("overlay"):
                pc["overlay"].post_hide()
            if pc.get("release_cap"):
                pc["release_cap"]()  # close the capture session (drops the portal share)
        price_action.setText("Price-Checker stoppen" if on else "Price-Checker starten")

    def _set_auto(on: bool) -> None:
        if on:
            if not pc["enabled"]:
                _set_price_enabled(True)
            if pc.get("auto"):
                return
            pc["auto"] = True
            stop = threading.Event()
            pc["auto_stop"] = stop
            threading.Thread(target=lambda: pc["auto_worker"](stop), daemon=True).start()
        else:
            pc["auto"] = False
            if pc.get("auto_stop"):
                pc["auto_stop"].set()
            if pc.get("overlay"):
                pc["overlay"].post_hide()
        if auto_action is not None:
            auto_action.setChecked(pc["auto"])

    def _on_trigger() -> None:
        # an appraise request (hotkey / tray) auto-starts the checker if needed
        if not pc["enabled"]:
            _set_price_enabled(True)
        if pc.get("auto"):
            return  # auto mode is already polling continuously
        if pc.get("appraise_once"):
            pc["appraise_once"]()

    bridge.trigger.connect(_on_trigger)

    # Always-on trigger sources (no capture until the checker is enabled).
    hk_accel = user_settings.get("hotkey", user_settings.DEFAULT_HOTKEY)
    _is_windows = sys.platform.startswith("win")
    win_hotkey: dict = {"hk": None}  # live handle so set_hotkey() can rebind it
    if _is_windows:
        # Windows lets us grab a true system-wide hotkey in-process; no socket /
        # portal / desktop-binding needed.
        from aldur_appraiser.win_hotkey import start_global_hotkey as start_win_hotkey

        win_hotkey["hk"] = start_win_hotkey(bridge.trigger.emit, accelerator=hk_accel)
        _keepalive = (win_hotkey,)  # noqa: F841
    else:
        #  1. Unix-socket poke from `appraiser trigger` (bound to a desktop shortcut),
        #  2. the GlobalShortcuts portal (best-effort; KDE / GNOME 48+).
        from aldur_appraiser.trigger import serve
        from aldur_appraiser.vision.global_hotkey import start_global_hotkey

        trigger_srv = serve(bridge.trigger.emit)  # keep a ref so it isn't GC'd
        portal_hotkey = start_global_hotkey(bridge.trigger.emit, trigger=accel_to_portal(hk_accel))
        _keepalive = (trigger_srv, portal_hotkey)  # noqa: F841

    def _check_updates(notify_uptodate: bool) -> None:
        def run():
            latest = updates.newer_release(__version__)
            if latest:
                bridge.update.emit(latest)
            elif notify_uptodate:
                bridge.uptodate.emit(__version__)

        threading.Thread(target=run, daemon=True).start()

    def open_temple() -> None:
        # In a frozen build (PyInstaller .exe) sys.executable IS our app, so call
        # it with just the subcommand; in dev it's the Python interpreter and we
        # need `-m aldur_appraiser`.
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "temple"]
        else:
            cmd = [sys.executable, "-m", "aldur_appraiser", "temple"]
        try:  # launch the planner as its own process
            subprocess.Popen(cmd)
        except Exception as exc:  # noqa: BLE001
            bridge.error.emit(f"Temple-Start fehlgeschlagen: {exc}")

    def _capture_hotkey():
        """Modal dialog that records the next key (or combo) the user presses and
        turns it into a GTK accelerator. A single key like '0' is allowed. Returns
        the accelerator string, or None if cancelled with Esc."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeySequence
        from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

        mod_only = {Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta,
                    Qt.Key_Super_L, Qt.Key_Super_R, Qt.Key_AltGr}

        class _Cap(QDialog):
            def __init__(self):
                super().__init__()
                self.accel = None
                self.setWindowTitle("Appraise-Hotkey festlegen")
                lay = QVBoxLayout(self)
                lay.addWidget(QLabel(
                    "Drücke die gewünschte Taste oder Tastenkombination.\n"
                    "Eine einzelne Taste (z. B. 0) ist erlaubt. Esc bricht ab."
                ))
                self.setMinimumWidth(360)

            def keyPressEvent(self, e):  # noqa: N802 - Qt override
                key = e.key()
                if key == Qt.Key_Escape:
                    self.reject()
                    return
                if key in mod_only:
                    return  # ignore lone modifier presses; wait for a real key
                ks = QKeySequence(key).toString()
                if not ks:
                    return
                is_fkey = len(ks) >= 2 and ks[0] in "Ff" and ks[1:].isdigit()
                base = ks.upper() if is_fkey else ks.lower()
                m = e.modifiers()
                parts = []
                if m & Qt.ControlModifier:
                    parts.append("<Control>")
                if m & Qt.AltModifier:
                    parts.append("<Alt>")
                if m & Qt.ShiftModifier:
                    parts.append("<Shift>")
                if m & Qt.MetaModifier:
                    parts.append("<Super>")
                self.accel = "".join(parts) + base
                self.accept()

        dlg = _Cap()
        return dlg.accel if dlg.exec() and dlg.accel else None

    def set_hotkey() -> None:
        accel = _capture_hotkey()
        if accel is None:  # Esc -> offer manual text entry for exotic keysyms
            cur = user_settings.get("hotkey", user_settings.DEFAULT_HOTKEY)
            text, ok = QInputDialog.getText(
                None, "Appraise-Hotkey",
                "Oder Tastenkürzel manuell (GTK-Format, z. B. <Control><Alt>p):",
                text=cur,
            )
            if not ok or not text.strip():
                return
            accel = text.strip()
        user_settings.set("hotkey", accel)
        if _is_windows:
            # Re-register the live system-wide hotkey right away.
            from aldur_appraiser.win_hotkey import start_global_hotkey as start_win_hotkey

            old = win_hotkey.get("hk")
            if old is not None:
                old.stop()
            win_hotkey["hk"] = start_win_hotkey(bridge.trigger.emit, accelerator=accel)
            if win_hotkey["hk"] is not None:
                body = f"Hotkey gesetzt: {accel}."
            else:
                body = (f"Hotkey gespeichert: {accel}, konnte aber nicht belegt werden — "
                        "evtl. ist die Kombination schon vergeben. Bitte eine andere wählen.")
        elif bind_gnome_shortcut(accel):
            body = f"Hotkey gesetzt: {accel} (GNOME-Kürzel eingetragen)."
        else:
            body = (f"Hotkey gespeichert: {accel}. Automatische Bindung nicht möglich — "
                    "bitte 'appraiser trigger' in den Tastatur-Einstellungen darauf legen.")
        if tray is not None:
            tray.showMessage("Aldur Appraiser", body, tray.MessageIcon.Information, 9000)

    # --- tray menu ----------------------------------------------------------
    _has_tray = QSystemTrayIcon.isSystemTrayAvailable()
    tray = QSystemTrayIcon(_tray_icon(app_icon)) if _has_tray else None
    price_action = QAction("Price-Checker starten")
    auto_action = QAction("Auto-Modus (laufend bewerten)")
    auto_action.setCheckable(True)
    if tray is not None:
        tray.setToolTip("Aldur Appraiser")
        menu = QMenu()
        title = QAction(f"Aldur Appraiser v{__version__}", menu)
        title.setEnabled(False)
        menu.addAction(title)
        menu.addSeparator()
        price_action.setParent(menu)
        price_action.triggered.connect(lambda: _set_price_enabled(not pc["enabled"]))
        menu.addAction(price_action)
        auto_action.setParent(menu)
        auto_action.triggered.connect(lambda: _set_auto(not pc["auto"]))
        menu.addAction(auto_action)
        appraise_action = QAction("Jetzt bewerten", menu)
        appraise_action.triggered.connect(bridge.trigger.emit)
        menu.addAction(appraise_action)
        menu.addSeparator()
        temple_action = QAction("Temple Planner öffnen", menu)
        temple_action.triggered.connect(open_temple)
        menu.addAction(temple_action)
        menu.addSeparator()
        hotkey_action = QAction("Appraise-Hotkey festlegen…", menu)
        hotkey_action.triggered.connect(set_hotkey)
        menu.addAction(hotkey_action)
        check_action = QAction("Nach Updates suchen", menu)
        check_action.triggered.connect(lambda: _check_updates(True))
        menu.addAction(check_action)
        menu.addSeparator()
        quit_action = QAction("Beenden", menu)
        quit_action.triggered.connect(app.quit)
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray._menu = menu  # keep refs alive
        tray.activated.connect(
            lambda r: menu.popup(QCursor.pos())
            if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )

        def _notify(body, level):
            tray.showMessage("Aldur Appraiser", body, level, 9000)

        info = tray.MessageIcon.Information
        bridge.error.connect(lambda m: _notify(m, tray.MessageIcon.Critical))
        bridge.uptodate.connect(lambda v: _notify(f"v{v} ist aktuell.", info))
        bridge.update.connect(lambda v: _notify(f"Update {v} verfügbar — Releases öffnen.", info))
        tray.messageClicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(updates.RELEASES_PAGE))
        )
        tray.show()
        if _is_windows and win_hotkey["hk"] is None:
            QTimer.singleShot(800, lambda: _notify(
                f"Appraise-Hotkey ({hk_accel}) konnte nicht belegt werden — evtl. ist "
                "die Kombination schon vergeben. Im Tray-Menü eine andere wählen.",
                tray.MessageIcon.Warning,
            ))
    else:
        bridge.error.connect(lambda m: print(f"error: {m}", file=sys.stderr))
        bridge.update.connect(
            lambda v: print(f"update {v}: {updates.RELEASES_PAGE}", file=sys.stderr)
        )

    print("aldur-appraiser running in the tray. Use its menu to start the "
          "Price-Checker or open the Temple Planner.")
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keep = QTimer()  # let Python handle SIGINT while Qt runs
    keep.start(200)
    keep.timeout.connect(lambda: None)
    _check_updates(notify_uptodate=False)
    return app.exec()


def run_app(
    *, backend: str | None = None, mode: str = "auto", style: str = "corner", refresh: bool = False
) -> int:
    """Entry point for `appraiser run`. mode: auto | overlay | console."""
    if mode == "console":
        return run_console(backend=backend, refresh=refresh)
    try:
        import PySide6  # noqa: F401
    except ImportError:
        if mode == "overlay":
            print("overlay requested but PySide6 is not installed "
                  '(pip install -e ".[overlay]")', file=sys.stderr)
            return 1
        print("overlay unavailable (PySide6 not installed); using console.\n")
        return run_console(backend=backend, refresh=refresh)
    return run_overlay(backend=backend, style=style, refresh=refresh)
