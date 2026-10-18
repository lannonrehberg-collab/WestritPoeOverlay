"""Corner-HUD overlay using Qt (PySide6).

A frameless, always-on-top, translucent, click-through window in a screen
corner. Qt is a pip wheel (no system dependency, unlike tkinter on atomic
distros) and handles these window traits well on Wayland/XWayland.

Updates arrive from the capture worker thread via Qt signals, which Qt delivers
to the GUI thread with a queued connection — the thread-safe way to touch
widgets. Heavy import (PySide6) is done lazily by the caller.
"""

from __future__ import annotations

import html
import sys

from aldur_appraiser.pricing.valuation import EvalResult, format_value


def result_to_html(
    result: EvalResult, base: str, *, stale: bool = False, dr: float | None = None
) -> str:
    """Render an EvalResult as the HUD's HTML body."""

    def fmt(total: float) -> str:
        val, unit = format_value(total, dr, base_unit=base)
        return f"{val:.1f} {unit}"

    rows = ["<div style='font-weight:bold;color:#d9c89a'>Aldur Appraiser</div>"]
    if not result.items:
        rows.append("<div style='color:#aaa'>no reward options recognised</div>")
        return "".join(rows)

    for v in result.items:
        name = html.escape(v.name)
        if v.known:
            if v.is_best:
                rows.append(
                    f"<div style='color:#7CFC8A;font-weight:bold'>&#9656; {v.qty}x {name} "
                    f"&mdash; {fmt(v.total)}</div>"
                )
            else:
                rows.append(
                    f"<div style='color:#e8e8e8'>{v.qty}x {name} &mdash; {fmt(v.total)}</div>"
                )
        else:
            rows.append(f"<div style='color:#888'>{v.qty}x {name} &mdash; ?</div>")

    if result.incomplete:
        rows.append("<div style='color:#caa45a;font-size:13px'>comparison incomplete</div>")
    for v in result.bonus_items:
        val = fmt(v.total) if v.known else "?"
        bname = html.escape(v.name)
        rows.append(
            "<div style='color:#8a8a8a;font-size:13px'>"
            f"+ bonus: {v.qty}x {bname} &mdash; {val}</div>"
        )
    if stale:
        rows.append("<div style='color:#caa45a;font-size:13px'>&#9888; stale prices</div>")
    return "".join(rows)


def build_inline_overlay(base: str, *, icon_paths=None, divine_rate=None):
    """Full-monitor, transparent, click-through overlay that paints a value chip
    at the end of each reward row (positions mapped from the OCR boxes).

    Receives per-row appraisals (with OCR boxes in ROI pixels) plus the anchor
    (ROI rect + frame size) and maps each row to monitor-local coordinates.
    Values are shown as "<n> [unit-icon]"; large totals switch to Divine via
    format_value(divine_rate). icon_paths maps unit -> icon file.
    """
    from PySide6.QtCore import QRectF, Qt, QTimer, Signal, Slot
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import QApplication, QWidget

    from aldur_appraiser.pricing.valuation import format_value

    BEST = QColor("#7CFC8A")
    KNOWN = QColor("#e8e8e8")
    DIM = QColor("#9a9a9a")
    ICON_H = 30
    WIN_MARGIN = 16  # padding around painted content before the window edge

    class InlineOverlay(QWidget):
        _show = Signal(object, object)
        _busy = Signal(object)
        _hide = Signal()

        def __init__(self):
            super().__init__()
            self.base = base
            self.divine_rate = divine_rate
            # (text, x, y, color, pixmap_or_None) in monitor-local coords
            self._chips: list[tuple[str, float, float, QColor, object]] = []
            # window's monitor-local top-left; paint coords are translated by it
            self._origin = (0.0, 0.0)
            self._spin = None          # (cx, cy) of the spinner while busy, else None
            self._spin_angle = 0
            self._spin_timer = QTimer(self)
            self._spin_timer.setInterval(70)
            self._spin_timer.timeout.connect(self._tick_spinner)
            self._icons: dict[str, QPixmap] = {}
            for unit, path in (icon_paths or {}).items():
                pm = QPixmap(str(path))
                if not pm.isNull():
                    self._icons[unit] = pm.scaledToHeight(ICON_H, Qt.SmoothTransformation)
            flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            if sys.platform.startswith("linux"):
                flags |= Qt.X11BypassWindowManagerHint
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setWindowFlag(Qt.WindowTransparentForInput, True)
            self._font = QFont("DejaVu Sans")
            self._font.setPixelSize(22)
            self._font.setBold(True)
            self._show.connect(self._apply_show)
            self._busy.connect(self._apply_busy)
            self._hide.connect(self._apply_hide)

        def post_rows(self, rows, anchor) -> None:
            self._show.emit(rows, anchor)

        def post_busy(self, anchor) -> None:
            self._busy.emit(anchor)

        def post_hide(self) -> None:
            self._hide.emit()

        def _tick_spinner(self) -> None:
            self._spin_angle = (self._spin_angle + 30) % 360
            self.update()

        def _chip_rect(self, label, x, y, icon):
            """Monitor-local (x, top, w, h) of a chip, for window sizing."""
            fm = QFontMetrics(self._font)
            pad, gap = 9, 5
            h = max(fm.height(), ICON_H) + 8
            tw = fm.horizontalAdvance(label)
            iw = icon.width() if icon is not None else 0
            w = pad + tw + (gap + iw if icon is not None else 0) + pad
            return (x, y - h / 2, w, h)

        def _fit_window(self, screen, rects) -> bool:
            """Size/position the window to a tight bbox around `rects` (empty ->
            hide). A full-monitor translucent override-redirect surface leaves
            XWayland/KWin compositor artifacts (incl. on other monitors), so we
            keep the surface only as large as the painted content."""
            if not rects:
                self.hide()
                return False
            g = screen.geometry()
            left = min(r[0] for r in rects) - WIN_MARGIN
            top = min(r[1] for r in rects) - WIN_MARGIN
            right = max(r[0] + r[2] for r in rects) + WIN_MARGIN
            bottom = max(r[1] + r[3] for r in rects) + WIN_MARGIN
            self._origin = (left, top)
            self.setGeometry(
                int(g.left() + left), int(g.top() + top),
                int(right - left), int(bottom - top),
            )
            return True

        @Slot(object)
        def _apply_busy(self, anchor) -> None:
            rl, rt, rw, rh, fw, fh = anchor
            cx, cy = rl + rw + 26, rt + 22  # near the panel's reward column
            self._spin = (cx, cy)
            self._chips = []
            r, pad = 14, 6
            rect = (cx - r - pad, cy - r - pad, 2 * (r + pad), 2 * (r + pad))
            if not self._spin_timer.isActive():
                self._spin_timer.start()
            if self._fit_window(self._screen_for_frame(fw, fh), [rect]):
                self.update()
                self.show()
                self.raise_()

        def _screen_for_frame(self, fw: int, fh: int):
            for s in QApplication.screens():
                g = s.geometry()
                if g.width() == fw and g.height() == fh:
                    return s
            return QApplication.primaryScreen()

        @Slot(object, object)
        def _apply_show(self, rows, anchor) -> None:
            rl, rt, rw, rh, fw, fh = anchor
            self._spin_timer.stop()
            self._spin = None
            chips = []
            for r in rows:
                v = r.valuation
                if r.box is None:
                    continue
                x = rl + r.box[2] + 10            # just right of the row text
                y = rt + (r.box[1] + r.box[3]) / 2
                if v.known:
                    val, unit = format_value(v.total, self.divine_rate, base_unit=self.base)
                    icon = self._icons.get(unit)
                    # value + icon; fall back to the unit name only if no icon
                    num = f"{val:.1f}" if icon else f"{val:.1f} {unit}"
                    label = f"+{num}" if v.is_bonus else num
                    color = DIM if v.is_bonus else (BEST if v.is_best else KNOWN)
                else:
                    label, color, icon = "?", DIM, None
                chips.append((label, float(x), float(y), color, icon))
            self._chips = chips
            rects = [self._chip_rect(lbl, x, y, icon) for lbl, x, y, _c, icon in chips]
            if self._fit_window(self._screen_for_frame(fw, fh), rects):
                self.update()
                self.show()
                self.raise_()

        @Slot()
        def _apply_hide(self) -> None:
            self._spin_timer.stop()
            self._spin = None
            self._chips = []
            self.hide()

        def _paint_spinner(self, p) -> None:
            cx, cy = self._spin
            r = 14
            pen = QPen(QColor(150, 205, 255))
            pen.setWidth(4)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            # a 270-degree arc rotating with the tick (Qt angles are 1/16 deg)
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), -self._spin_angle * 16, 270 * 16)

        def paintEvent(self, _event) -> None:
            if self._spin is None and not self._chips:
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            # chips/spinner carry monitor-local coords; shift into window space
            p.translate(-self._origin[0], -self._origin[1])
            if self._spin is not None:
                self._paint_spinner(p)
                p.end()
                return
            p.setFont(self._font)
            fm = p.fontMetrics()
            pad, gap = 9, 5
            h = max(fm.height(), ICON_H) + 8
            for label, x, y, color, icon in self._chips:
                tw = fm.horizontalAdvance(label)
                iw = icon.width() if icon is not None else 0
                w = pad + tw + (gap + iw if icon is not None else 0) + pad
                top = y - h / 2
                p.setBrush(QColor(20, 18, 14, 225))
                p.setPen(QColor(90, 74, 42))
                p.drawRoundedRect(QRectF(x, top, w, h), 7, 7)
                p.setPen(color)
                p.drawText(QRectF(x + pad, top, tw, h), Qt.AlignVCenter | Qt.AlignLeft, label)
                if icon is not None:
                    p.drawPixmap(int(x + pad + tw + gap), int(y - icon.height() / 2), icon)
            p.end()

    return InlineOverlay()


def build_overlay(base: str, *, position: str = "top-right", opacity: float = 0.92):
    """Construct the overlay widget (imports PySide6 lazily). Returns the widget.

    The widget exposes thread-safe post_result(result)/post_hide() that emit Qt
    signals delivered on the GUI thread.
    """
    from PySide6.QtCore import Qt, Signal, Slot
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

    class CornerOverlay(QWidget):
        _show = Signal(object, bool, object, object)
        _hide = Signal()

        def __init__(self):
            super().__init__()
            self.base = base
            self.position = position
            flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            if sys.platform.startswith("linux"):
                # override-redirect: stay above a fullscreen game without a
                # focus change (alt-tab), like a classic X11 HUD overlay.
                flags |= Qt.X11BypassWindowManagerHint
            self.setWindowFlags(flags)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            # passive HUD: never intercept mouse/keyboard
            self.setWindowFlag(Qt.WindowTransparentForInput, True)

            self._label = QLabel(self)
            self._label.setTextFormat(Qt.RichText)
            self._label.setMinimumWidth(320)
            self._label.setStyleSheet(
                "QLabel{"
                f"background:rgba(20,18,14,{int(opacity * 255)});"
                "color:#e8e8e8;border:2px solid #5a4a2a;border-radius:10px;"
                "padding:12px 16px;font-family:'DejaVu Sans',sans-serif;font-size:17px;}"
            )
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self._label)

            self._show.connect(self._apply_show)
            self._hide.connect(self._apply_hide)

        # thread-safe entry points (called from the capture worker thread)
        def post_result(
            self, result: EvalResult, stale: bool = False, anchor=None, dr=None
        ) -> None:
            self._show.emit(result, stale, anchor, dr)

        def post_hide(self) -> None:
            self._hide.emit()

        @Slot(object, bool, object, object)
        def _apply_show(self, result: EvalResult, stale: bool, anchor, dr) -> None:
            self._label.setText(result_to_html(result, self.base, stale=stale, dr=dr))
            self.adjustSize()
            self._reposition(anchor)
            self.show()
            self.raise_()

        @Slot()
        def _apply_hide(self) -> None:
            self.hide()

        def _screen_for_frame(self, fw: int, fh: int):
            for s in QApplication.screens():
                g = s.geometry()
                if g.width() == fw and g.height() == fh:
                    return s
            return QApplication.primaryScreen()

        def _reposition(self, anchor=None) -> None:
            if anchor is not None:
                self._anchor_near_panel(anchor)
            else:
                self._anchor_corner(QApplication.primaryScreen().geometry())

        def _anchor_near_panel(self, anchor) -> None:
            rl, rt, rw, rh, fw, fh = anchor
            g = self._screen_for_frame(fw, fh).geometry()
            margin = 12
            # to the right of the reward column, aligned with its top
            x = g.left() + rl + rw + margin
            y = g.top() + rt
            if x + self.width() > g.right():  # no room right -> left of the panel
                x = g.left() + rl - self.width() - margin
            x = max(g.left() + margin, x)
            y = min(y, g.bottom() - self.height() - margin)
            self.move(x, y)

        def _anchor_corner(self, screen) -> None:
            margin = 16
            x = screen.left() + margin
            y = screen.top() + margin
            if "right" in self.position:
                x = screen.right() - self.width() - margin
            if "bottom" in self.position:
                y = screen.bottom() - self.height() - margin
            self.move(x, y)

    return CornerOverlay()
