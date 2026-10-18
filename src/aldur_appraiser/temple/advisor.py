"""Per-run placement advisor.

Each run you draw 6 random cards (rooms + paths); this ranks where to place them.
Pure functions over the engine: `score` turns a temple into a single number,
`suggest` ranks every legal (card, cell) by how much it raises the score, and
`plan_hand` greedily places a whole hand.

The score is intentionally simple and tweakable: sum of `value[room] * tier` over
placed rooms, minus a penalty per cannot-connect violation. A placement's worth =
the score delta it causes, so it automatically credits not just the placed room's
own tier but every neighbour it upgrades (and a Path that connects a Generator to
the network gets credit for the rooms that Generator then powers).
"""

from __future__ import annotations

from dataclasses import dataclass

from aldur_appraiser.temple.engine import Cell, Temple
from aldur_appraiser.temple.rooms import ROOMS, can_connect, can_orphan, is_volatile

VIOLATION_PENALTY = 5.0
# A "removable" room is a loose end (its removal orphans nothing), so it's what
# destabilisation can delete. Discount it to push valuable rooms into the chain's
# interior (safe) and keep the snake's end cheap — fewer ends = less loss.
REMOVABLE_DISCOUNT = 0.6
# A volatile room (Treasure Vault / Architect reward rooms) self-destabilises once
# used, so it won't persist in a re-runnable temple — discounted the same way.
VOLATILE_DISCOUNT = 0.6


@dataclass
class Suggestion:
    card: str          # room id to place
    cell: Cell
    gain: float        # score delta from placing `card` at `cell`
    result_tier: int   # tier the placed room ends up at
    upgrades: int      # how many already-placed neighbours it pushes up a tier
    note: str = ""


def score(temple: Temple, values: dict[str, float] | None = None) -> float:
    """A single number for the whole temple: sum of value*tier, less violations."""
    values = values or {}
    removable = temple.removable_room_cells()
    tiers = temple.tiers()
    total = 0.0
    for c in temple.accessible_room_cells():  # only connected rooms give bonuses
        rid = temple.effective_room_id(c)
        room = ROOMS[rid]
        # Generators carry extra weight: besides their own tier they power nearby
        # rooms and scale a global Temple Mod (Construct Effectiveness), so a
        # higher-tier Generator should clearly win.
        default = 2.0 if room.generator else 1.0
        worth = values.get(rid, default) * tiers.get(c, 1)
        # A Generator is a functional power source you keep, not a throwaway chain
        # end, so its tier counts in full — without this a T2 Generator at a loose
        # end (0.4x) would rank below a T1 elsewhere.
        if c in removable and not room.generator:
            worth *= 1.0 - REMOVABLE_DISCOUNT
        if is_volatile(room):         # self-destabilises once used
            worth *= 1.0 - VOLATILE_DISCOUNT
        total += worth
    total -= VIOLATION_PENALTY * len(temple.connection_violations())
    return total


def legal_cells(temple: Temple, room_id: str) -> list[Cell]:
    """Empty, non-blocked cells `room_id` may legally go on.

    The road grows from the entrance, so a placement must reach back to the
    entrance — touching a *disconnected* cluster doesn't count (the game never
    leaves rooms stranded). Concretely a neighbour only qualifies if it's the
    entrance or an already-placed cell that is itself accessible from the
    entrance. The two card kinds then differ:

    * a **Path** extends the road — it may only sit next to the entrance or an
      accessible Path (a Path floating beside rooms is not a valid road), and
    * a **room** attaches to the road — it's legal next to the entrance, next to
      an accessible Path (rooms auto-connect to adjacent paths), or next to an
      accessible room the in-game whitelist lets it connect to (`rooms.can_connect`).

    On an empty grid only entrance-adjacent cells are legal. Architect-console
    rooms (`rooms.can_orphan`: Vaults, Royal Access, …) are exempt — they may sit
    anywhere, connected or not."""
    out: list[Cell] = []
    is_path = room_id == "path"
    orphan_ok = room_id in ROOMS and can_orphan(ROOMS[room_id])
    accessible = temple.accessible_cells()  # cells reachable from the entrance
    for x in range(temple.size):
        for y in range(temple.size):
            c = (x, y)
            if c in temple.cells or c in temple.blocked:
                continue
            if orphan_ok or c == temple.entrance:
                out.append(c)
                continue
            if is_path:
                connects = any(
                    n == temple.entrance or (temple.is_path(n) and n in accessible)
                    for n in temple.neighbors4(c)
                )
            else:
                connects = any(
                    n == temple.entrance
                    or (
                        n in accessible
                        and can_connect(room_id, temple.effective_room_id(n))
                        and not temple.connection_blocked(room_id, n)
                    )
                    for n in temple.neighbors4(c)
                )
            if connects:
                out.append(c)
    return out


def _evaluate(temple: Temple, card: str, cell: Cell, base: float,
              base_tiers: dict[Cell, int], values) -> Suggestion:
    temple.cells[cell] = card
    try:
        gain = score(temple, values) - base
        new_tiers = temple.tiers()
        result_tier = new_tiers.get(cell, 1)
        upgrades = sum(
            1
            for n in temple.neighbors4(cell)
            if temple.is_room(n) and new_tiers.get(n, 1) > base_tiers.get(n, 1)
        )
    finally:
        del temple.cells[cell]
    name = ROOMS[card].name
    if card == "path":
        note = "Path — connector" + (f" (+{gain:.0f} from enabling power)" if gain > 0 else "")
    else:
        note = f"{name} → T{result_tier}"
        if upgrades:
            note += f", upgrades {upgrades} neighbour(s)"
        if is_volatile(ROOMS[card]):
            note += " [one-use]"
    return Suggestion(card, cell, gain, result_tier, upgrades, note)


def suggest(temple: Temple, hand, *, values=None, top: int = 5) -> list[Suggestion]:
    """Rank the best single placements for the distinct cards in `hand`."""
    base = score(temple, values)
    base_tiers = temple.tiers()
    work = temple.copy()
    out = [
        _evaluate(work, card, cell, base, base_tiers, values)
        for card in dict.fromkeys(hand)  # distinct, order-preserving
        if card in ROOMS
        for cell in legal_cells(work, card)  # legal cells differ per room
    ]
    out.sort(key=lambda s: s.gain, reverse=True)
    return out[:top]


def plan_hand(temple: Temple, hand, *, values=None) -> list[Suggestion]:
    """Greedily place a whole hand: repeatedly take the best legal placement for a
    remaining card, apply it, recompute, until the hand is spent or nothing helps."""
    work = temple.copy()
    remaining = list(hand)
    steps: list[Suggestion] = []
    while remaining:
        best = suggest(work, set(remaining), values=values, top=1)
        if not best:
            break
        s = best[0]
        # place it for real and consume one matching card
        work.cells[s.cell] = s.card
        remaining.remove(s.card)
        steps.append(s)
    return steps
