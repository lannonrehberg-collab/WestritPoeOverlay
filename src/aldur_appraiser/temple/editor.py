"""Interactive Qt editor for the Temple (Phase 2).

A 9x9 grid you paint rooms/paths onto; the rules engine drives the live display
(room tiers, the hovered Generator's power radius, cannot-connect violations).
Logic lives in engine.py — this file is just the Qt shell, kept thin so it stays
testable headlessly. Launched via `appraiser temple`.
"""

from __future__ import annotations

from aldur_appraiser.temple.engine import Temple
from aldur_appraiser.temple.rooms import ROOMS, can_orphan, destabilises, is_volatile

CATEGORY_COLOR = {
    "barrack": "#3b6ea5",
    "production": "#a5703b",
    "ritual": "#7a3ba5",
    "generator": "#a53b3b",
    "utility": "#3ba59a",
    "special": "#c8a13b",
    "path": "#4a4a4a",
}
ERASE = "— erase —"  # sentinel palette entry
MEDALLION = "✦ Medallion +1 Tier"  # brush: click a room to toggle a +1-tier boost

# Distinct, readable highlight colours assigned per suggested room so the grid
# cell and its line in the "Best placements" list share a colour.
HIGHLIGHT_PALETTE = ["#ffd24a", "#5ab0ff", "#9be15a", "#ff7ad0", "#ffa54a", "#b58aff"]

# Goal presets -> per-room advisor weights. The user picks one instead of tuning
# individual rooms; missing rooms default to weight 1.0. NB: Vaults are NOT
# weighted up — they're one-use (destabilise when opened) and the efficient
# community layouts avoid them, so the volatile discount keeps them low-ranked.
PRESETS: dict[str, dict[str, float]] = {
    "Balanced (all equal)": {},
    "Currency & Rarity": {
        "alchemy_lab": 3.0, "smithy": 2.0, "spymaster": 1.5,  # spymaster boosts Alchemy
    },
    "Experience": {"synthflesh_lab": 3.0, "flesh_surgeon": 1.5},
    "Crafting & Corruption": {
        "corruption_chamber": 3.0, "sacrificial_chamber": 2.0, "thaumaturge": 2.0,
    },
    "Monster Quantity & Effectiveness": {
        "garrison": 2.0, "commander": 2.0, "armoury": 2.0, "golem_works": 2.0,
    },
}


def abbrev(room_id: str) -> str:
    """Short cell label for a room id."""
    if room_id == "path":
        return ""
    name = ROOMS[room_id].name
    return "".join(w[0] for w in name.split()[:2]).upper() or name[:2].upper()


def cellname(c: tuple[int, int]) -> str:
    """Human grid reference matching the 1-9 edge labels (column, row)."""
    return f"col {c[0] + 1}, row {c[1] + 1}"


def disconnected_rooms(temple) -> set[tuple[int, int]]:
    """Placed rooms stranded from the entrance — excluding Architect-console rooms
    (Vaults, Royal Access, …) which may legally exist as orphans."""
    accessible = temple.accessible_room_cells()
    return {
        c
        for c in temple.room_cells()
        if c not in accessible and not can_orphan(ROOMS[temple.cells[c]])
    }


def layout_path():
    """File the editor auto-saves the working layout to (platformdirs config)."""
    from aldur_appraiser.config import config_dir

    return config_dir() / "temple_layout.json"


def save_layout(temple, preset: str = "", medallions: list[str] | None = None) -> None:
    """Persist the current layout + chosen preset + medallion rooms (best-effort;
    ignores IO errors)."""
    import json

    try:
        p = layout_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "temple": temple.to_dict(),
                    "preset": preset,
                    "medallions": list(medallions or []),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def load_layout() -> tuple[Temple | None, str, list[str]]:
    """Return (temple, preset_name, medallion_rooms) from the saved layout, or
    (None, '', []) if absent."""
    import json

    try:
        p = layout_path()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return Temple.from_dict(d["temple"]), d.get("preset", ""), list(d.get("medallions", []))
    except (OSError, ValueError, KeyError):
        pass
    return None, "", []


_ROOM_PIXMAPS = None


def _room_pixmaps():
    """room id -> QPixmap of the official icon (cached once per process; empty if
    icons can't be fetched, so the editor falls back to coloured tiles)."""
    global _ROOM_PIXMAPS
    if _ROOM_PIXMAPS is None:
        from PySide6.QtGui import QPixmap

        from aldur_appraiser.temple.icons import room_icon_paths

        _ROOM_PIXMAPS = {}
        for rid, path in room_icon_paths().items():
            pm = QPixmap(str(path))
            if not pm.isNull():
                _ROOM_PIXMAPS[rid] = pm
    return _ROOM_PIXMAPS


def build_editor():
    """Construct the editor widget (imports PySide6 lazily). Returns the widget."""
    from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPolygonF
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QHBoxLayout,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QPushButton,
        QToolTip,
        QVBoxLayout,
        QWidget,
    )

    room_pixmaps = _room_pixmaps()  # room id -> QPixmap (official icons; may be {})

    class GridWidget(QWidget):
        cellClicked = Signal(tuple, int)  # (cell, Qt.MouseButton value)

        def __init__(self, temple: Temple):
            super().__init__()
            self.temple = temple
            self.icons = room_pixmaps  # room id -> QPixmap (official icons)
            self.cell = 56
            self.pad = 22  # margin for the 1-9 row/column labels
            self._hover: tuple[int, int] | None = None
            self._tiers: dict[tuple[int, int], int] = {}
            self._violation_cells: set[tuple[int, int]] = set()
            self._removable: set[tuple[int, int]] = set()
            self._disconnected: set[tuple[int, int]] = set()
            self._boosted: dict[tuple[int, int], int] = {}
            # suggested cell -> QColor (one colour per suggested room, matching its
            # line in the "Best placements" list).
            self._highlights: dict[tuple[int, int], object] = {}
            self.setMouseTracking(True)
            n = self.pad + self.cell * temple.size
            self.setFixedSize(n, n)
            self._font = QFont("DejaVu Sans")
            self._font.setPixelSize(15)
            self._font.setBold(True)
            self._label_font = QFont("DejaVu Sans")
            self._label_font.setPixelSize(12)

        def refresh(self) -> None:
            self.temple_changed()

        def set_highlights(self, colors) -> None:
            """`colors` is {cell: QColor}, or a plain iterable of cells (gold)."""
            if isinstance(colors, dict):
                self._highlights = dict(colors)
            else:
                gold = QColor("#ffd24a")
                self._highlights = {c: gold for c in colors}
            self.update()

        def temple_changed(self) -> None:
            self._tiers = self.temple.tiers()
            self._violation_cells = {
                c for pair in self.temple.connection_violations() for c in pair
            }
            self._removable = self.temple.removable_room_cells()
            # rooms not reachable from the entrance: normal ones need a Path/road
            # drawn (Vaults/Royal Access/Architect may legally stay orphans).
            self._disconnected = disconnected_rooms(self.temple)
            self._boosted = dict(self.temple.medallion_boosts)
            self.update()

        def _cell_at(self, x: int, y: int) -> tuple[int, int] | None:
            if x < self.pad or y < self.pad:
                return None
            cx, cy = (x - self.pad) // self.cell, (y - self.pad) // self.cell
            return (cx, cy) if self.temple.in_bounds((cx, cy)) else None

        def mousePressEvent(self, e) -> None:  # noqa: N802 (Qt naming)
            p = e.position().toPoint()
            c = self._cell_at(p.x(), p.y())
            if c is not None:
                self.cellClicked.emit(c, int(e.button().value))

        def mouseMoveEvent(self, e) -> None:  # noqa: N802
            p = e.position().toPoint()
            c = self._cell_at(p.x(), p.y())
            if c != self._hover:
                self._hover = c
                self.update()

        def leaveEvent(self, _e) -> None:  # noqa: N802
            self._hover = None
            self.update()

        def _tooltip_for(self, c) -> str:
            """Hover text for a cell: room name, id, current tier and effect."""
            if c is None:
                return ""
            rid = self.temple.cells.get(c)
            if rid is None:
                return "" if c not in self.temple.blocked else "Blocked / destabilised"
            if rid == "path":
                return "Path — road connector"
            room = ROOMS[rid]
            lines = [f"{room.name}  [{rid}]  ·  Tier {self._tiers.get(c, 1)}"]
            if room.bonus:
                lines.append(room.bonus)
            return "\n".join(lines)

        def event(self, e):  # per-cell tooltips
            if e.type() == QEvent.ToolTip:
                text = self._tooltip_for(self._cell_at(e.pos().x(), e.pos().y()))
                if text:
                    QToolTip.showText(e.globalPos(), text, self)
                else:
                    QToolTip.hideText()
                return True
            return super().event(e)

        def _power_cells(self) -> set[tuple[int, int]]:
            """Rooms actually powered by the hovered Generator (engine model)."""
            h = self._hover
            if h is None or self.temple.cells.get(h) != "generator":
                return set()
            powering = self.temple._generator_powering(self.temple.tiers())
            return {c for c, gens in powering.items() if h in gens}

        def paintEvent(self, _e) -> None:  # noqa: N802
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            s, pad = self.cell, self.pad
            # 1-9 column (top) and row (left) labels, to read off recommendations
            p.setFont(self._label_font)
            p.setPen(QColor("#b8ae93"))
            for i in range(self.temple.size):
                p.drawText(QRectF(pad + i * s, 0, s, pad), Qt.AlignCenter, str(i + 1))
                p.drawText(QRectF(0, pad + i * s, pad, s), Qt.AlignCenter, str(i + 1))
            power = self._power_cells()
            for x in range(self.temple.size):
                for y in range(self.temple.size):
                    c = (x, y)
                    r = QRectF(pad + x * s, pad + y * s, s, s)
                    rid = self.temple.cells.get(c)
                    if c in self.temple.blocked:
                        p.fillRect(r, QColor(25, 22, 20))
                    elif rid is not None:
                        p.fillRect(r, QColor(CATEGORY_COLOR.get(ROOMS[rid].category, "#666")))
                    else:
                        p.fillRect(r, QColor(38, 35, 32))
                    if c in power:
                        p.fillRect(r, QColor(150, 205, 255, 60))
                    if c == self.temple.entrance:
                        p.fillRect(r, QColor(124, 252, 138, 50))
                    if c in self._disconnected:  # not reachable from the entrance
                        p.fillRect(r, QColor(0, 0, 0, 110))  # dim it out
                    # border (red if a cannot-connect violation)
                    bad = c in self._violation_cells
                    p.setPen(QColor("#d04a4a") if bad else QColor(20, 18, 14))
                    p.drawRect(r)
                    if rid and rid != "path":
                        tier = self._tiers.get(c, 1)
                        pm = self.icons.get(rid)
                        p.setPen(QColor("#f0ead6"))
                        p.setFont(self._font)
                        if pm is not None:
                            p.drawPixmap(r.adjusted(7, 4, -7, -14).toRect(), pm)
                            p.drawText(r.adjusted(0, 0, 0, -2),
                                       Qt.AlignBottom | Qt.AlignHCenter, "I" * tier)
                        else:
                            p.drawText(r, Qt.AlignCenter, f"{abbrev(rid)}\n{'I' * tier}")
                    if c in self._boosted:  # Medallion +N tier (stackable)
                        n = self._boosted[c]
                        p.setPen(QColor("#ffd24a"))
                        p.setFont(self._label_font)
                        label = "✦" if n <= 1 else f"✦{n}"
                        p.drawText(r.adjusted(4, 2, -2, -2), Qt.AlignTop | Qt.AlignLeft, label)
                    if rid and rid != "path" and is_volatile(ROOMS[rid]):
                        p.setPen(QColor("#d04ad0"))  # one-use: consumed on completion
                        p.drawRect(r.adjusted(3, 3, -3, -3))
                    # destabilisation warning: a ⚠ triangle in the top-right corner.
                    # Vaults always destabilise; craft rooms only at their device tier.
                    if rid and rid != "path" and destabilises(ROOMS[rid], self._tiers.get(c, 1)):
                        ax, top = r.right() - 13, r.top() + 4
                        tri = QPolygonF([
                            QPointF(ax, top),
                            QPointF(ax - 9, top + 15),
                            QPointF(ax + 9, top + 15),
                        ])
                        p.setPen(Qt.NoPen)
                        p.setBrush(QColor("#e8b020"))
                        p.drawPolygon(tri)
                        p.setBrush(Qt.NoBrush)
                        p.setPen(QPen(QColor(35, 22, 0), 1))
                        p.drawLine(QPointF(ax, top + 5), QPointF(ax, top + 10))
                        p.drawPoint(QPointF(ax, top + 12))
                    if c in self._disconnected:  # dashed cyan = needs a road to connect
                        p.setPen(QPen(QColor("#5fd0e0"), 1, Qt.DashLine))
                        p.drawRect(r.adjusted(2, 2, -2, -2))
                    if c in self._removable:  # loose end -> destabilisation can delete it
                        p.setPen(QColor("#e08a2a"))
                        p.drawRect(r.adjusted(6, 6, -6, -6))
                    if c in self._highlights:
                        p.setPen(self._highlights[c])
                        p.drawRect(r.adjusted(2, 2, -2, -2))
                        p.drawRect(r.adjusted(4, 4, -4, -4))
            p.end()

    class TempleEditor(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Aldur — Temple Planner")
            self.temple, self._saved_preset, saved_medallions = load_layout()  # restore
            if self.temple is None:
                self.temple = Temple()
            self.brush = "garrison"

            self.grid = GridWidget(self.temple)
            self.grid.cellClicked.connect(self._on_cell)

            self.palette = QListWidget()
            self.palette.addItem(ERASE)
            self.palette.addItem(MEDALLION)
            for rid in ROOMS:
                item = QListWidgetItem(f"{ROOMS[rid].name}  [{rid}]")
                if rid in room_pixmaps:
                    item.setIcon(QIcon(room_pixmaps[rid]))
                self.palette.addItem(item)
            self.palette.setCurrentRow(2 + list(ROOMS).index("garrison"))
            self.palette.currentRowChanged.connect(self._on_brush)

            # goal preset -> advisor weights (what the player wants to build for)
            self.weights: dict[str, float] = {}
            self.preset_select = QComboBox()
            for name in PRESETS:
                self.preset_select.addItem(name)
            self.preset_select.currentTextChanged.connect(self._on_preset)
            self.priorities = QLabel()
            self.priorities.setWordWrap(True)

            clear = QPushButton("Clear grid")
            clear.clicked.connect(self._clear)
            # Atlas "Transcendent Progress" node: raises the max room tier 3 -> 4.
            self.transcendent_check = QCheckBox("Transcendent Progress (max tier 4)")
            self.transcendent_check.setChecked(self.temple.max_tier >= 4)
            self.transcendent_check.toggled.connect(self._on_transcendent)
            # tier picker for rooms upgraded by a player action (sacrifice/assassinate)
            self.tier_select = QComboBox()
            self._populate_tier_select()
            self.status = QLabel()

            # --- per-run advisor: a hand of drawn cards + suggestions ----------
            self.hand: list[str] = []
            self.hand_list = QListWidget()
            add_card = QPushButton("Add selected room to hand")
            add_card.clicked.connect(self._add_card)
            remove_card = QPushButton("Remove selected card")
            remove_card.clicked.connect(self._remove_card)
            clear_hand = QPushButton("Clear hand")
            clear_hand.clicked.connect(self._clear_hand)
            detect_hand_btn = QPushButton("Detect hand from screen")
            detect_hand_btn.clicked.connect(self._detect_hand)
            suggest_btn = QPushButton("Suggest placement")
            suggest_btn.clicked.connect(self._suggest)
            self.suggestions = QLabel("Add your drawn cards, then Suggest.")
            self.suggestions.setWordWrap(True)

            # --- medallion rooms: persistent extra cards from Medallions --------
            # held across runs, so they persist (unlike the per-run random hand)
            # and feed the advisor alongside the drawn hand.
            self.medallions: list[str] = list(saved_medallions)
            self.medallion_list = QListWidget()
            for rid in self.medallions:
                if rid in ROOMS:
                    self.medallion_list.addItem(ROOMS[rid].name)
            add_med = QPushButton("Add selected room to medallions")
            add_med.clicked.connect(self._add_medallion)
            remove_med = QPushButton("Remove selected medallion")
            remove_med.clicked.connect(self._remove_medallion)
            clear_med = QPushButton("Clear medallions")
            clear_med.clicked.connect(self._clear_medallions)
            detect_med_btn = QPushButton("Detect medallions from screen")
            detect_med_btn.clicked.connect(self._detect_medallions)

            left = QVBoxLayout()
            left.addWidget(QLabel("Room (left-click place · right-click erase)"))
            left.addWidget(self.palette, 1)
            left.addWidget(QLabel("Goal preset (what you want to build for)"))
            left.addWidget(self.preset_select)
            left.addWidget(self.priorities)
            left.addWidget(self.transcendent_check)
            left.addWidget(QLabel("Tier for sacrifice/assassinate rooms"))
            left.addWidget(self.tier_select)
            left.addWidget(clear)
            left.addWidget(QLabel("Hand (drawn cards this run)"))
            left.addWidget(self.hand_list, 1)
            left.addWidget(add_card)
            left.addWidget(remove_card)
            left.addWidget(clear_hand)
            left.addWidget(detect_hand_btn)
            left.addWidget(QLabel("Medallion rooms (held across runs)"))
            left.addWidget(self.medallion_list, 1)
            left.addWidget(add_med)
            left.addWidget(remove_med)
            left.addWidget(clear_med)
            left.addWidget(detect_med_btn)
            left.addWidget(suggest_btn)
            legend = QLabel(
                "Markers:  ⚠ amber = destabilises on use (avoid / mind its effect)   ·   "
                "orange = deletable loose end   ·   cyan dashed = not connected to entrance   ·   "
                "coloured border = suggested placement (matches its line below)   ·   "
                "✦ = Medallion +1 tier (pick the Medallion brush, click a room)"
            )
            legend.setWordWrap(True)
            legend.setStyleSheet("color: #9b927d;")

            row = QHBoxLayout(self)
            row.addLayout(left)
            grid_col = QVBoxLayout()
            grid_col.addWidget(self.grid)
            grid_col.addWidget(self.status)
            grid_col.addWidget(legend)
            grid_col.addWidget(self.suggestions)
            row.addLayout(grid_col)

            if self._saved_preset in PRESETS:
                self.preset_select.setCurrentText(self._saved_preset)  # fires _on_preset
            else:
                self._on_preset(self.preset_select.currentText())
            self._refresh()

        def _add_card(self) -> None:
            if self.brush in ROOMS:
                self.hand.append(self.brush)
                self.hand_list.addItem(ROOMS[self.brush].name)

        def _remove_card(self) -> None:
            row = self.hand_list.currentRow()
            if 0 <= row < len(self.hand):
                del self.hand[row]
                self.hand_list.takeItem(row)

        def _clear_hand(self) -> None:
            self.hand.clear()
            self.hand_list.clear()
            self.grid.set_highlights(())
            self.suggestions.setText("Add your drawn cards, then Suggest.")

        def _detect_hand(self) -> None:
            """Read the drawn cards off the live game screen (capture + OCR).

            Grabs a single frame and closes the capture immediately. On Wayland an
            active portal/PipeWire screencast keeps flickering other monitors with
            stray fragments, so we keep it open as briefly as possible (one grab)
            rather than holding a session open. The portal's restore-token means
            this doesn't re-prompt after the first grant."""
            try:
                from aldur_appraiser.temple.vision import detect_hand

                detected = detect_hand(self._grab_screen())
            except Exception as e:  # noqa: BLE001 - a UI button must never crash
                self.suggestions.setText(f"Detection failed: {e}")
                return
            if not detected:
                self.suggestions.setText("No 'Room Cards' panel found on screen.")
                return
            self.hand = list(detected)
            self.hand_list.clear()
            for rid in self.hand:
                self.hand_list.addItem(ROOMS[rid].name)
            names = ", ".join(ROOMS[r].name for r in self.hand)
            self.suggestions.setText(f"Detected {len(self.hand)} cards: {names}")

        def _grab_screen(self):
            """One BGR frame off the screen. One-shot (open+grab+close) so the
            Wayland screencast is active only momentarily; the portal restore-token
            means it doesn't re-prompt after the first grant."""
            from aldur_appraiser.vision.capture import open_capture

            with open_capture() as cap:
                return cap.grab()

        def _detect_medallions(self) -> None:
            """Read the held medallions off the screen (icon template-matching)."""
            try:
                from aldur_appraiser.temple.vision import detect_medallions

                detected = detect_medallions(self._grab_screen())
            except Exception as e:  # noqa: BLE001 - a UI button must never crash
                self.suggestions.setText(f"Detection failed: {e}")
                return
            if not detected:
                self.suggestions.setText("No medallions detected on screen.")
                return
            self.medallions = list(detected)
            self.medallion_list.clear()
            for rid in self.medallions:
                self.medallion_list.addItem(ROOMS[rid].name)
            self._persist()
            names = ", ".join(ROOMS[r].name for r in self.medallions)
            self.suggestions.setText(f"Detected {len(self.medallions)} medallion(s): {names}")

        def _add_medallion(self) -> None:
            if self.brush in ROOMS:
                self.medallions.append(self.brush)
                self.medallion_list.addItem(ROOMS[self.brush].name)
                self._persist()

        def _remove_medallion(self) -> None:
            row = self.medallion_list.currentRow()
            if 0 <= row < len(self.medallions):
                del self.medallions[row]
                self.medallion_list.takeItem(row)
                self._persist()

        def _clear_medallions(self) -> None:
            self.medallions.clear()
            self.medallion_list.clear()
            self._persist()

        def _suggest(self) -> None:
            from aldur_appraiser.temple.advisor import suggest

            cards = self.hand + self.medallions  # drawn cards plus medallion rooms
            if not cards:
                self.suggestions.setText("Hand is empty — add the cards you drew.")
                return
            ranked = suggest(self.temple, cards, values=self.weights, top=20)
            if not ranked:
                self.suggestions.setText("No legal placement found.")
                self.grid.set_highlights(())
                return
            # Pick at most one suggestion per cell so every list line maps to its
            # own coloured grid cell (different rooms often share the best cells).
            # First take each distinct room's best free cell — so each room in the
            # hand shows up — then fill the rest with the next-best free cells.
            used: set[tuple[int, int]] = set()
            chosen = []
            seen_rooms: set[str] = set()
            for s in ranked:
                if s.card not in seen_rooms and s.cell not in used:
                    seen_rooms.add(s.card)
                    used.add(s.cell)
                    chosen.append(s)
            for s in ranked:
                if len(chosen) >= 5:
                    break
                if s.cell not in used:
                    used.add(s.cell)
                    chosen.append(s)
            chosen.sort(key=lambda s: -s.gain)
            ranked = chosen[:5]
            # one colour per suggested room, shared by its grid cell(s) and its
            # line in the list, so it's obvious which room goes where.
            room_color: dict[str, str] = {}
            for s in ranked:
                if s.card not in room_color:
                    room_color[s.card] = HIGHLIGHT_PALETTE[len(room_color) % len(HIGHLIGHT_PALETTE)]
            # grid: best (highest-gain) suggestion per cell, coloured by its room
            best_by_cell: dict[tuple[int, int], object] = {}
            best_gain: dict[tuple[int, int], float] = {}
            for s in ranked:
                if s.cell not in best_gain or s.gain > best_gain[s.cell]:
                    best_gain[s.cell] = s.gain
                    best_by_cell[s.cell] = QColor(room_color[s.card])
            self.grid.set_highlights(best_by_cell)
            lines = [
                f'<span style="color:{room_color[s.card]}">{i + 1}. {s.note}  @ '
                f"{cellname(s.cell)}  (+{s.gain:.1f})</span>"
                for i, s in enumerate(ranked)
            ]
            self.suggestions.setText("Best placements:<br>" + "<br>".join(lines))

        def _populate_tier_select(self) -> None:
            """Fill the manual-tier dropdown with 1..max_tier, keeping the choice."""
            keep = self.tier_select.currentData()
            self.tier_select.blockSignals(True)
            self.tier_select.clear()
            for t in range(1, self.temple.max_tier + 1):
                self.tier_select.addItem(f"Tier {t}", t)
            if keep:
                idx = self.tier_select.findData(min(keep, self.temple.max_tier))
                if idx >= 0:
                    self.tier_select.setCurrentIndex(idx)
            self.tier_select.blockSignals(False)

        def _on_transcendent(self, checked: bool) -> None:
            self.temple.max_tier = 4 if checked else 3
            self._populate_tier_select()
            self._refresh()
            self._persist()

        def _on_brush(self, idx: int) -> None:
            if idx <= 0:
                self.brush = ERASE
            elif idx == 1:
                self.brush = MEDALLION
            else:
                self.brush = list(ROOMS)[idx - 2]

        def _on_preset(self, name: str) -> None:
            self.weights = dict(PRESETS.get(name, {}))
            shown = ", ".join(f"{ROOMS[r].name} {w:g}" for r, w in sorted(self.weights.items()))
            self.priorities.setText(f"Priorities: {shown or 'all rooms equal'}")
            if getattr(self, "preset_select", None) is not None:
                self._persist()

        def _on_cell(self, cell, button) -> None:
            from PySide6.QtCore import Qt

            # Medallion brush: left-click a placed room to add a +1-tier boost
            # (stackable), right-click to remove one.
            if self.brush == MEDALLION:
                if self.temple.is_room(cell):
                    boosts = self.temple.medallion_boosts
                    if button == int(Qt.RightButton.value):
                        if boosts.get(cell, 0) > 1:
                            boosts[cell] -= 1
                        else:
                            boosts.pop(cell, None)
                    else:
                        boosts[cell] = boosts.get(cell, 0) + 1
                    self._refresh()
                    self._persist()
                return

            erase = self.brush == ERASE or button == int(Qt.RightButton.value)
            self.temple.remove(cell)
            if not erase and cell not in self.temple.blocked:
                try:
                    self.temple.place(cell, self.brush)
                    if ROOMS[self.brush].manual_tier:
                        self.temple.tier_overrides[cell] = self.tier_select.currentData()
                    self._consume_card(self.brush)  # used a drawn/medallion card
                except ValueError:
                    pass
            self._refresh()
            self._persist()

        def _consume_card(self, rid: str) -> None:
            """Placing a room spends one matching card. Prefer the drawn hand; fall
            back to a Medallion room only if the hand has none (ambiguous when the
            same room sits in both — the hand wins)."""
            if rid in self.hand:
                i = self.hand.index(rid)
                del self.hand[i]
                self.hand_list.takeItem(i)
            elif rid in self.medallions:
                i = self.medallions.index(rid)
                del self.medallions[i]
                self.medallion_list.takeItem(i)

        def _clear(self) -> None:
            self.temple.cells.clear()
            self.temple.tier_overrides.clear()
            self._refresh()
            self._persist()

        def _persist(self) -> None:
            save_layout(self.temple, self.preset_select.currentText(), self.medallions)

        def closeEvent(self, e) -> None:  # noqa: N802
            self._persist()
            super().closeEvent(e)

        def _refresh(self) -> None:
            self.grid.temple_changed()
            tiers = self.temple.tiers()
            t3 = sum(1 for v in tiers.values() if v == 3)
            viol = len(self.temple.connection_violations())
            removable = len(self.temple.removable_room_cells())
            volatile = sum(
                1
                for c in self.temple.room_cells()
                if destabilises(ROOMS[self.temple.cells[c]], tiers.get(c, 1))
            )
            disconnected = len(disconnected_rooms(self.temple))
            self.status.setText(
                f"Rooms: {len(tiers)}   Tier 3: {t3}   "
                f"Loose ends (deletable): {removable}   "
                f"Destabilising: {volatile}   Disconnected: {disconnected}   Violations: {viol}"
            )

    return TempleEditor()


def run_editor() -> int:
    """Entry point for `appraiser temple` — opens the editor window."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    win = build_editor()
    win.show()
    return app.exec()
