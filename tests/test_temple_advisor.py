"""Tests for the per-run placement advisor."""

from __future__ import annotations

import pytest

from aldur_appraiser.temple.advisor import legal_cells, plan_hand, score, suggest
from aldur_appraiser.temple.engine import Temple


def _temple(entrance=(0, 0)):
    return Temple(entrance=entrance)


def test_generator_is_weighted_and_exempt_from_loose_end_discount():
    # a lone Generator keeps its full weighted value (no loose-end discount),
    # unlike a normal room — so a higher-tier Generator clearly outranks a lower.
    g = _temple()
    g.place((0, 0), "generator")
    assert score(g) == pytest.approx(2.0)   # value 2.0 x T1, NOT discounted
    n = _temple()
    n.place((0, 0), "garrison")
    assert score(n) == pytest.approx(0.4)   # value 1.0 x T1 x (1 - 0.6 loose-end)


def test_score_rewards_higher_tiers():
    t = _temple(entrance=(4, 4))           # commander sits on the entrance -> connected
    t.place((4, 4), "commander")
    t.place((5, 4), "garrison")
    low = score(t)
    t.place((3, 4), "garrison")  # commander now T2
    assert score(t) > low


def test_legal_cells_require_network_connection():
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "garrison")           # adjacent to the entrance
    cells = set(legal_cells(t, "commander"))  # commander connects to a garrison
    assert (4, 6) in cells                # touches the placed room (legal connection)
    assert (4, 8) in cells                # the entrance itself
    assert (0, 0) not in cells            # far corner, disconnected


def test_legal_cells_respect_the_connection_whitelist():
    # A Spymaster only connects to a Path or a Garrison — never an Alchemy Lab.
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "alchemy_lab")        # connected to the entrance
    cells = set(legal_cells(t, "spymaster"))
    assert (4, 6) not in cells            # would touch only the Alchemy Lab -> illegal
    assert (3, 7) not in cells            # likewise
    # an Armoury *does* connect to an Alchemy Lab, so it's legal beside it
    assert (4, 6) in set(legal_cells(t, "armoury"))


def test_spymaster_is_never_suggested_next_to_alchemy_lab():
    # regression: the advisor used to recommend a Spymaster beside an Alchemy Lab,
    # an impossible connection in-game.
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "alchemy_lab")
    sugg = suggest(t, ["spymaster"], top=5)
    for s in sugg:
        # every suggested spymaster cell must legally connect (path/entrance/garrison)
        assert s.cell != (4, 6) and s.cell != (3, 7)


def test_placement_must_reach_the_entrance():
    # A cluster floating away from the entrance is unreachable, so nothing may be
    # placed against it — the game never strands rooms. Only entrance-rooted cells
    # are legal until the road actually connects the cluster up.
    t = _temple(entrance=(0, 0))
    t.place((5, 5), "garrison")            # floating cluster, far from the entrance
    t.place((5, 6), "commander")
    cells = set(legal_cells(t, "commander"))
    assert (5, 4) not in cells             # touches the cluster but unreachable
    assert (6, 5) not in cells
    assert (1, 0) in cells and (0, 1) in cells  # only the entrance-rooted cells


def test_orphan_rooms_may_be_placed_anywhere():
    # Architect-console rooms (Vaults, Royal Access, …) may sit disconnected, so
    # the advisor can put them on a far cell that touches nothing.
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "garrison")            # the only connected room
    legal = set(legal_cells(t, "currency_vault"))
    assert (5, 5) in legal                 # nowhere near the network -> still legal
    # a normal room cannot go there
    assert (5, 5) not in set(legal_cells(t, "armoury"))


def test_alchemy_lab_blocked_when_armoury_already_has_one():
    # An Armoury may have at most one Alchemy Lab neighbour (maxNeighborCount). A
    # second Alchemy Lab whose only connection would be that Armoury is illegal.
    t = _temple(entrance=(0, 0))
    t.place((1, 0), "path")
    t.place((2, 0), "path")
    t.place((2, 1), "armoury")              # connected to the entrance via the road
    # control: with no Alchemy Lab yet, a cell touching only the Armoury is legal
    assert (2, 2) in set(legal_cells(t, "alchemy_lab"))
    t.place((3, 1), "alchemy_lab")          # the Armoury's one allowed Alchemy Lab
    assert (2, 2) not in set(legal_cells(t, "alchemy_lab"))  # the 2nd is blocked


def test_generator_joins_the_road_not_adjacent_rooms():
    # In-game the Generator only sits next to a Path (the road); being adjacent to
    # a Thaumaturge/Smithy is not enough (those are upgraded by adjacency/power).
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "path")
    t.place((1, 0), "path")
    t.place((1, 1), "thaumaturge")            # accessible, beside the road
    legal = set(legal_cells(t, "generator"))
    assert (2, 0) in legal                    # beside a path -> legal
    assert (2, 1) not in legal                # beside only the Thaumaturge -> not legal


def test_path_must_attach_to_the_road_not_a_room():
    # The road grows from the entrance: a Path may only sit beside the entrance or
    # another Path, never floating next to a room.
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "garrison")              # a room beside the entrance
    cells = set(legal_cells(t, "path"))
    assert (4, 8) in cells                   # the entrance itself
    assert (3, 8) in cells and (5, 8) in cells   # beside the entrance
    assert (4, 6) not in cells               # beside the garrison only -> illegal
    assert (3, 7) not in cells               # beside the garrison only -> illegal
    # a path next to an existing path is fine
    t.place((3, 8), "path")
    assert (3, 7) in set(legal_cells(t, "path"))  # now beside a path


def test_suggest_places_third_garrison_next_to_commander():
    # Commander already at T2 (2 adjacent Garrisons); a 3rd adjacent one takes it
    # to T3, which must beat dropping the Garrison anywhere else.
    t = _temple(entrance=(4, 8))
    t.place((4, 5), "commander")
    t.place((5, 5), "garrison")
    t.place((3, 5), "garrison")
    # connect to the entrance so placements are legal around the cluster
    t.place((4, 6), "path")
    t.place((4, 7), "path")
    best = suggest(t, ["garrison"], top=1)[0]
    assert best.card == "garrison"
    assert best.cell == (4, 4)            # the remaining cell adjacent to Commander
    assert best.upgrades >= 1             # it pushed the Commander up a tier


def test_path_that_connects_a_generator_scores_positive():
    # A Generator one tile off the network can't power; a Path bridging it in
    # lets it power the adjacent Smithy -> the Path placement gains score.
    t = _temple(entrance=(0, 0))
    t.place((0, 0), "garrison")           # at entrance
    t.place((2, 0), "generator")
    t.place((2, 1), "smithy")             # within radius once the generator is on the net
    # gap at (1,0) disconnects the generator cluster from the entrance
    s = suggest(t, ["path"], top=1)[0]
    assert s.cell == (1, 0)
    assert s.gain > 0


def test_volatile_room_is_discounted_in_score():
    # a persistent room outscores a one-use (volatile) room of the same tier
    persistent = _temple()
    persistent.place((0, 0), "garrison")          # on the entrance -> connected
    volatile = _temple()
    volatile.place((0, 0), "treasure_vault")      # tier 1, self-destabilises
    assert score(persistent) > score(volatile)


def test_suggest_flags_one_use_rooms():
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "garrison")                   # something to connect to
    best = [s for s in suggest(t, ["treasure_vault"], top=3) if s.card == "treasure_vault"][0]
    assert "one-use" in best.note


def test_weights_steer_the_recommendation():
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "garrison")            # anchor so placements are legal
    hand = ["garrison", "alchemy_lab"]
    # the player says "I want Alchemy Labs" -> it should top the ranking
    best = suggest(t, hand, values={"alchemy_lab": 5.0}, top=1)[0]
    assert best.card == "alchemy_lab"


def test_plan_hand_consumes_cards_and_improves_score():
    t = _temple(entrance=(4, 8))
    t.place((4, 7), "commander")
    before = score(t)
    steps = plan_hand(t, ["garrison", "garrison", "path"])
    assert len(steps) == 3                # all cards placed
    # applying the plan for real reproduces a better board
    for s in steps:
        t.cells[s.cell] = s.card
    assert score(t) > before
