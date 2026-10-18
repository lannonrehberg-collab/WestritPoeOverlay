"""Headless smoke test for the temple editor (offscreen Qt)."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aldur_appraiser.temple.editor import abbrev, build_editor  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_layout(tmp_path, monkeypatch):
    """Point persistence at a throwaway file so a real saved layout in the user's
    config dir can't leak into (and break) these tests."""
    import aldur_appraiser.temple.editor as ed

    monkeypatch.setattr(ed, "layout_path", lambda: tmp_path / "layout.json")


def test_editor_builds_and_reflects_engine_state():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()

    # place a Commander with 2 adjacent Garrisons -> the engine should read T2
    w.temple.place((4, 4), "commander")
    w.temple.place((5, 4), "garrison")
    w.temple.place((3, 4), "garrison")
    w._refresh()

    assert w.temple.tiers()[(4, 4)] == 2
    assert "Rooms: 3" in w.status.text()
    assert "Tier 3: 0" in w.status.text()


def test_abbrev_uses_initials():
    assert abbrev("commander") == "CC"   # Commander's Chamber
    assert abbrev("path") == ""


def test_editor_advisor_highlights_best_cell():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    t = w.temple
    t.place((4, 7), "commander")   # adjacent to the entrance (4,8) -> connected
    t.place((5, 7), "garrison")
    t.place((3, 7), "garrison")   # Commander at T2; (4, 6) takes it to T3
    w._refresh()

    w.brush = "garrison"
    w._add_card()
    assert w.hand == ["garrison"]
    w._suggest()
    assert (4, 6) in w.grid._highlights          # the best placement is highlighted


def test_suggest_colours_highlights_per_room():
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.temple.place((4, 8), "garrison")          # anchor at the entrance
    w.hand = ["alchemy_lab"]
    w._suggest()
    highs = w.grid._highlights
    assert len(highs) > 1
    assert all(isinstance(c, QColor) for c in highs.values())
    # a single room -> all its cells share one colour
    assert len({c.name() for c in highs.values()}) == 1
    # the colour also appears in the (rich-text) list, matching the cells
    assert highs[next(iter(highs))].name() in w.suggestions.text().lower()


def test_remove_selected_card_from_hand():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    for rid in ("garrison", "commander", "path"):
        w.brush = rid
        w._add_card()
    assert w.hand == ["garrison", "commander", "path"]
    w.hand_list.setCurrentRow(1)                # select "Commander"
    w._remove_card()
    assert w.hand == ["garrison", "path"]
    assert w.hand_list.count() == 2


def test_editor_preset_feeds_the_advisor():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.temple.place((4, 8), "garrison")          # anchor
    w.hand = ["garrison", "alchemy_lab"]
    w.preset_select.setCurrentText("Currency & Rarity")   # weights Alchemy Lab up
    assert w.weights.get("alchemy_lab", 1.0) > 1.0
    w._suggest()
    assert "1. Alchemy Lab" in w.suggestions.text()   # weighted up -> top suggestion


def test_medallion_rooms_feed_the_advisor():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.temple.place((4, 8), "garrison")          # anchor at the entrance
    w.medallions = ["alchemy_lab"]              # a medallion-granted Armoury/Lab
    w._suggest()
    # the medallion room is considered even with an empty drawn hand
    assert "Alchemy Lab" in w.suggestions.text()


def test_medallions_persist_across_editor_sessions(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    import aldur_appraiser.temple.editor as ed

    monkeypatch.setattr(ed, "layout_path", lambda: tmp_path / "layout.json")
    _app = QApplication.instance() or QApplication([])

    w = ed.build_editor()
    w.medallions = ["armoury"]
    w._persist()

    w2 = ed.build_editor()
    assert w2.medallions == ["armoury"]


def test_editor_marks_rooms_disconnected_from_entrance():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()                              # entrance defaults to (4, 8)
    w.temple.place((4, 7), "garrison")              # adjacent to the entrance -> connected
    w.temple.place((0, 0), "armoury")               # far corner -> stranded
    w.temple.place((8, 0), "currency_vault")        # a vault may legally be an orphan
    w._refresh()
    assert w.grid._disconnected == {(0, 0)}         # the vault is NOT flagged
    assert (4, 7) not in w.grid._disconnected
    assert "Disconnected: 1" in w.status.text()
    assert "Destabilising: 1" in w.status.text()    # the vault destabilises on use


def test_transcendent_progress_checkbox_raises_max_tier():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    assert w.temple.max_tier == 3
    assert w.tier_select.findData(4) < 0          # no Tier 4 option yet
    w.transcendent_check.setChecked(True)
    assert w.temple.max_tier == 4
    assert w.tier_select.findData(4) >= 0          # Tier 4 now selectable
    w.transcendent_check.setChecked(False)
    assert w.temple.max_tier == 3
    assert w.tier_select.findData(4) < 0


def test_transcendent_progress_persists_across_sessions(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    import aldur_appraiser.temple.editor as ed

    monkeypatch.setattr(ed, "layout_path", lambda: tmp_path / "layout.json")
    _app = QApplication.instance() or QApplication([])

    w = ed.build_editor()
    w.transcendent_check.setChecked(True)
    w2 = ed.build_editor()
    assert w2.temple.max_tier == 4
    assert w2.transcendent_check.isChecked()


def test_alchemy_lab_counts_as_destabilising_only_at_tier_3():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.temple.place((4, 4), "alchemy_lab")       # T1 -> safe
    w._refresh()
    assert "Destabilising: 0" in w.status.text()
    w.temple.place((5, 4), "thaumaturge")       # two adjacent Thaumaturges -> T3
    w.temple.place((3, 4), "thaumaturge")
    w._refresh()
    assert w.temple.room_tier((4, 4)) == 3
    assert "Destabilising: 1" in w.status.text()  # the T3 Alchemy Lab now destabilises


def test_medallion_brush_boosts_a_room_to_tier_4():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from aldur_appraiser.temple.editor import MEDALLION

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.transcendent_check.setChecked(True)       # allow tier 4
    w.temple.place((4, 4), "armoury")           # build the Armoury at T3 ...
    w.temple.place((5, 4), "smithy")            # ... via Smithy AND Alchemy Lab
    w.temple.place((3, 4), "alchemy_lab")
    assert w.temple.room_tier((4, 4)) == 3
    w.brush = MEDALLION
    w._on_cell((4, 4), int(Qt.LeftButton.value))      # left-click: +1 medallion
    assert w.temple.medallion_boosts[(4, 4)] == 1
    assert w.temple.room_tier((4, 4)) == 4            # T3 + 1 -> T4
    w._on_cell((4, 4), int(Qt.LeftButton.value))      # stacks to +2
    assert w.temple.medallion_boosts[(4, 4)] == 2
    w._on_cell((4, 4), int(Qt.RightButton.value))     # right-click: remove one
    assert w.temple.medallion_boosts[(4, 4)] == 1
    w._on_cell((4, 4), int(Qt.RightButton.value))     # and the last one clears it
    assert (4, 4) not in w.temple.medallion_boosts


def test_grid_tooltip_shows_room_name_id_and_tier():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.temple.place((4, 4), "commander")
    w.temple.place((5, 4), "garrison")
    w.temple.place((3, 4), "garrison")        # Commander -> T2
    w._refresh()
    tip = w.grid._tooltip_for((4, 4))
    assert "Commander's Chamber" in tip and "[commander]" in tip and "Tier 2" in tip
    assert w.grid._tooltip_for((6, 6)) == ""          # empty cell -> no tooltip
    assert w.grid._tooltip_for((4, 4)).count("\n") >= 1  # name line + effect line


def test_placing_a_card_consumes_it_hand_before_medallion():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.brush = "garrison"
    w._add_card()
    w.brush = "generator"
    w._add_card()                                # hand: garrison, generator
    w.brush = "generator"
    w._add_medallion()                           # medallions: generator
    # placing a generator spends the HAND one, leaving the medallion untouched
    w.brush = "generator"
    w._on_cell((4, 4), int(Qt.LeftButton.value))
    assert w.temple.cells[(4, 4)] == "generator"
    assert w.hand == ["garrison"]
    assert w.hand_list.count() == 1
    assert w.medallions == ["generator"]
    # with no generator left in the hand, the next one comes from the medallion
    w.brush = "generator"
    w._on_cell((4, 5), int(Qt.LeftButton.value))
    assert w.medallions == []
    assert w.medallion_list.count() == 0


def test_placing_a_room_not_in_any_list_consumes_nothing():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.brush = "garrison"
    w._add_card()
    w.brush = "armoury"                          # not in the hand
    w._on_cell((4, 4), int(Qt.LeftButton.value))
    assert w.hand == ["garrison"]                # untouched


def test_cellname_is_one_indexed_col_row():
    from aldur_appraiser.temple.editor import cellname

    assert cellname((0, 0)) == "col 1, row 1"
    assert cellname((3, 6)) == "col 4, row 7"


def test_layout_persists_across_editor_sessions(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    import aldur_appraiser.temple.editor as ed

    monkeypatch.setattr(ed, "layout_path", lambda: tmp_path / "layout.json")
    _app = QApplication.instance() or QApplication([])

    w = ed.build_editor()                       # starts empty (tmp file absent)
    w.temple.place((4, 7), "garrison")
    w.preset_select.setCurrentText("Experience")
    w._persist()

    w2 = ed.build_editor()                       # a fresh session restores it
    assert w2.temple.cells.get((4, 7)) == "garrison"
    assert w2.preset_select.currentText() == "Experience"


def test_editor_applies_tier_override_to_manual_room():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    w = build_editor()
    w.tier_select.setCurrentIndex(2)            # Tier 3
    w.brush = "spymaster"
    w._on_cell((2, 2), int(Qt.LeftButton.value))
    assert w.temple.tier_overrides[(2, 2)] == 3
    assert w.temple.room_tier((2, 2)) == 3

