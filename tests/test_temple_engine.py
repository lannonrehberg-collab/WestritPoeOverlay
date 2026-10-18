"""Tests for the temple rules engine (grid/tiers/connectivity/risk)."""

from __future__ import annotations

from aldur_appraiser.temple.engine import Temple


def _temple(entrance=(0, 0), **kw):
    return Temple(entrance=entrance, **kw)


# --- adjacency-based tiers ---------------------------------------------------


def test_commander_tier_scales_with_adjacent_garrisons():
    t = _temple()
    t.place((4, 4), "commander")
    t.place((5, 4), "garrison")
    t.place((3, 4), "garrison")
    assert t.room_tier((4, 4)) == 2  # 2 adjacent Garrisons -> T2
    t.place((4, 3), "garrison")
    assert t.room_tier((4, 4)) == 3  # 3 -> T3


def test_garrison_t3_requires_both_commander_and_armoury():
    # require_all: 1 barrack-line upgrader -> T2, but T3 needs Commander AND Armoury
    t = _temple()
    t.place((4, 4), "garrison")
    t.place((5, 4), "commander")
    assert t.room_tier((4, 4)) == 2
    t.place((3, 4), "armoury")
    assert t.room_tier((4, 4)) == 3


def test_smithy_counts_golem_and_generator_together():
    # group sum: a Generator (power via a Path) + a Golem Works count as 2 -> T3
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "generator")        # on the entrance -> accessible
    t.place((1, 0), "path")             # the Generator feeds this Path
    t.place((1, 1), "smithy")           # beside the powered Path
    assert t.room_tier((1, 1)) == 2     # 1 source (generator via the path)
    t.place((2, 1), "golem_works")      # adjacent Golem Works (counts by adjacency)
    assert t.room_tier((1, 1)) == 3     # 2 sources (generator + golem)


def test_generator_conducts_power_along_paths():
    # a Generator powers rooms beside paths it feeds (within range), not just
    # directly-adjacent ones
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "generator")
    t.place((1, 0), "path")
    t.place((2, 0), "path")             # 2 path-steps from the generator (range 3)
    t.place((2, 1), "smithy")           # beside the powered path
    assert t.room_tier((2, 1)) == 2


def test_fixed_tier_room_never_upgrades():
    t = _temple()
    t.place((4, 4), "treasure_vault")
    t.place((5, 4), "garrison")
    assert t.room_tier((4, 4)) == 1


# --- generator power (radius + must be connected) ----------------------------


def test_generator_powers_smithy_via_a_path():
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "generator")   # at the entrance -> accessible
    t.place((1, 0), "path")        # the Generator feeds this Path (step 1, in range)
    t.place((1, 1), "smithy")      # beside the powered Path
    assert t.room_tier((1, 1)) >= 2


def test_generator_does_not_power_a_directly_adjacent_room():
    # a room merely touching the Generator (no Path between) is NOT powered
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "generator")   # accessible
    t.place((1, 0), "smithy")      # directly adjacent, but no Path link
    assert t.room_tier((1, 0)) == 1


def test_disconnected_generator_does_not_power():
    t = _temple(entrance=(8, 8))   # entrance far away
    t.place((0, 0), "generator")   # not connected to the entrance network
    t.place((1, 0), "smithy")
    assert t.room_tier((1, 0)) == 1


def test_generator_out_of_range_does_not_power():
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "generator")
    t.place((5, 0), "smithy")      # manhattan 5 > T1 radius (3)
    # bridge them so the smithy is on the network but still out of power range
    for x in range(1, 5):
        t.place((x, 0), "path")
    assert t.room_tier((5, 0)) == 1


# --- accessibility -----------------------------------------------------------


def test_accessibility_from_entrance():
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "garrison")
    t.place((0, 1), "commander")
    t.place((5, 5), "armoury")     # disconnected island
    acc = t.accessible_room_cells()
    assert (0, 0) in acc and (0, 1) in acc
    assert (5, 5) not in acc


# --- removable (loose-end) rooms / snake logic -------------------------------


def test_removable_rooms_are_the_loose_ends_of_the_chain():
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "garrison")     # first room (anchored to the entrance) -> safe
    t.place((4, 6), "armoury")      # interior of the snake -> safe (articulation)
    t.place((4, 5), "commander")    # the tail -> the only deletable room
    assert t.removable_room_cells() == {(4, 5)}
    assert (4, 6) in t.articulation_room_cells()
    # branching off the middle adds a second loose end (worse for keeping rooms)
    t.place((5, 6), "smithy")
    assert t.removable_room_cells() == {(4, 5), (5, 6)}


def test_temple_to_dict_from_dict_roundtrip():
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "garrison")
    t.place((4, 6), "sacrificial_chamber")
    t.tier_overrides[(4, 6)] = 3
    t2 = Temple.from_dict(t.to_dict())
    assert t2.cells == t.cells
    assert t2.tier_overrides == t.tier_overrides
    assert t2.entrance == t.entrance


# --- manual tier override (sacrifice / assassinate rooms) --------------------


def test_manual_tier_override_for_sacrifice_room():
    t = _temple()
    t.place((4, 4), "sacrificial_chamber")
    assert t.room_tier((4, 4)) == 1            # default until set
    t.tier_overrides[(4, 4)] = 3
    assert t.room_tier((4, 4)) == 3
    t.remove((4, 4))                           # removal drops the override
    assert (4, 4) not in t.tier_overrides


def test_override_ignored_for_layout_upgraded_room():
    t = _temple()
    t.place((4, 4), "commander")               # not a manual_tier room
    t.tier_overrides[(4, 4)] = 3
    assert t.room_tier((4, 4)) == 1            # override ignored; no adjacent garrisons


def test_max_tier_caps_rooms_and_transcendent_raises_it():
    # a manual_tier room may climb to 4 only once the cap (Transcendent Progress)
    # allows it; by default it's clamped to 3.
    t = _temple()
    t.place((4, 4), "sacrificial_chamber")     # manual_tier room
    t.tier_overrides[(4, 4)] = 4
    assert t.room_tier((4, 4)) == 3            # capped at the default max of 3
    t.max_tier = 4                              # Transcendent Progress on
    assert t.room_tier((4, 4)) == 4


def test_medallion_adds_tiers_stackably_capped_at_max():
    t = _temple()
    t.place((4, 4), "garrison")            # a layout room at T1
    assert t.room_tier((4, 4)) == 1
    t.medallion_boosts[(4, 4)] = 1
    assert t.room_tier((4, 4)) == 2        # +1 from one medallion
    t.medallion_boosts[(4, 4)] = 2
    assert t.room_tier((4, 4)) == 3        # +2 from two medallions (stackable)
    t.medallion_boosts[(4, 4)] = 5
    assert t.room_tier((4, 4)) == 3        # clamped to max_tier (3)
    t.max_tier = 4
    assert t.room_tier((4, 4)) == 4        # Transcendent Progress lifts the cap
    t.remove((4, 4))
    assert (4, 4) not in t.medallion_boosts


def test_medallion_boosts_survive_dict_roundtrip():
    t = _temple()
    t.place((1, 1), "armoury")
    t.medallion_boosts[(1, 1)] = 2
    assert Temple.from_dict(t.to_dict()).medallion_boosts == {(1, 1): 2}
    # legacy list format (one boost per cell) still loads
    legacy = Temple.from_dict({"medallion_boosts": [[1, 1], [2, 2]]})
    assert legacy.medallion_boosts == {(1, 1): 1, (2, 2): 1}


def test_connection_blocked_caps_alchemy_per_armoury():
    t = _temple()
    t.place((4, 4), "armoury")
    assert t.connection_blocked("alchemy_lab", (4, 4)) is False   # no alchemy yet
    t.place((5, 4), "alchemy_lab")                                # armoury's one alchemy
    assert t.connection_blocked("alchemy_lab", (4, 4)) is True    # cap reached
    # the rule is specific: a Smithy may still connect to that Armoury
    assert t.connection_blocked("smithy", (4, 4)) is False


def test_garrison_feeding_a_commander_cannot_take_a_synthflesh():
    t = _temple()
    t.place((4, 4), "garrison")
    assert t.connection_blocked("synthflesh_lab", (4, 4)) is False  # plain garrison: ok
    t.place((5, 4), "commander")                                    # garrison now feeds it
    assert t.connection_blocked("synthflesh_lab", (4, 4)) is True   # converter blocked


def test_max_tier_survives_dict_roundtrip():
    t = _temple()
    t.max_tier = 4
    assert Temple.from_dict(t.to_dict()).max_tier == 4
    assert Temple.from_dict(Temple().to_dict()).max_tier == 3   # default


# --- conversions -------------------------------------------------------------


def test_spymaster_converts_adjacent_garrison_to_legion():
    t = _temple()
    t.place((4, 4), "garrison")
    t.place((5, 4), "spymaster")
    assert t.effective_room_id((4, 4)) == "legion_barracks"


def test_synthflesh_converts_adjacent_garrison_to_transcendent():
    t = _temple()
    t.place((4, 4), "garrison")
    t.place((5, 4), "synthflesh_lab")
    assert t.effective_room_id((4, 4)) == "transcendent_barracks"


# --- cannot-connect rule -----------------------------------------------------


def test_cannot_connect_violation_detected():
    t = _temple()
    t.place((4, 4), "commander")
    t.place((5, 4), "spymaster")    # Commander cannot connect to Spymaster
    assert t.connection_violations() == [((4, 4), (5, 4))]


def test_tiers_returns_all_rooms_only():
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "garrison")
    t.place((0, 1), "path")
    t.place((0, 2), "commander")
    tiers = t.tiers()
    assert set(tiers) == {(0, 0), (0, 2)}  # path excluded
