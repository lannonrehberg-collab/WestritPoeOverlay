"""Temple rules engine — pure, no UI.

Models a 9x9 grid of placed rooms/paths and derives everything the editor and the
advisor need:

  - connectivity & accessibility from the entrance (BFS over occupied cells) —
    used for the Generator's "must be connected to a road" rule and to let the
    editor keep placements connected (the game never allows orphaned rooms),
  - room tier from its group upgrade rules (count / require_all / source_min_tier
    over a list of source types; a "generator" source is satisfied by a Generator
    *powering* the cell via the road network, not plain adjacency),
  - Garrison conversions (Spymaster -> Legion, Synthflesh -> Transcendent).

Tiers are resolved to a fixed point because they can depend on neighbours' tiers
(Generator range, source_min_tier rules). Upgrade rules are ported from the
Tetriszocker editor; see docs/temple-plan.md.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from aldur_appraiser.temple.rooms import MAX_NEIGHBOR_RULES, ROOMS

GRID_SIZE = 9
ENTRANCE = (4, 8)  # bottom-centre; the build grows up from here (VERIFY orientation)
GEN_RADIUS = {1: 3, 2: 4, 3: 5, 4: 6}  # Generator Manhattan power range by tier

Cell = tuple[int, int]


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


@dataclass
class Temple:
    size: int = GRID_SIZE
    entrance: Cell = ENTRANCE
    # pre-destabilised / unusable cells (2-3 random per entry)
    blocked: set[Cell] = field(default_factory=set)
    # (x, y) -> room id (an id in ROOMS, including "path")
    cells: dict[Cell, str] = field(default_factory=dict)
    # (x, y) -> tier, for rooms whose tier comes from a player action (sacrifice /
    # assassinate) and so can't be derived from the layout (manual_tier rooms).
    tier_overrides: dict[Cell, int] = field(default_factory=dict)
    # Highest tier a room can reach. Default 3; the Atlas "Transcendent Progress"
    # node raises it to 4 (rooms may then climb one rank higher).
    max_tier: int = 3
    # cell -> number of Medallions applied (+1 tier each, capped at max_tier).
    medallion_boosts: dict[Cell, int] = field(default_factory=dict)

    # --- grid basics ---------------------------------------------------------

    def copy(self) -> Temple:
        t = Temple(self.size, self.entrance, set(self.blocked), dict(self.cells))
        t.tier_overrides = dict(self.tier_overrides)
        t.max_tier = self.max_tier
        t.medallion_boosts = dict(self.medallion_boosts)
        return t

    def to_dict(self) -> dict:
        """Plain-JSON snapshot (for persisting a layout)."""
        return {
            "size": self.size,
            "entrance": list(self.entrance),
            "blocked": [list(c) for c in sorted(self.blocked)],
            "cells": {f"{x},{y}": rid for (x, y), rid in self.cells.items()},
            "tier_overrides": {f"{x},{y}": t for (x, y), t in self.tier_overrides.items()},
            "max_tier": self.max_tier,
            "medallion_boosts": {f"{x},{y}": n for (x, y), n in self.medallion_boosts.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> Temple:
        def key(s: str) -> Cell:
            x, y = s.split(",")
            return (int(x), int(y))

        t = cls(
            size=int(d.get("size", GRID_SIZE)),
            entrance=tuple(d.get("entrance", ENTRANCE)),  # type: ignore[arg-type]
            blocked={tuple(c) for c in d.get("blocked", [])},  # type: ignore[misc]
            cells={key(k): v for k, v in d.get("cells", {}).items()},
        )
        t.tier_overrides = {key(k): int(v) for k, v in d.get("tier_overrides", {}).items()}
        t.max_tier = int(d.get("max_tier", 3))
        mb = d.get("medallion_boosts", {})
        if isinstance(mb, dict):
            t.medallion_boosts = {key(k): int(v) for k, v in mb.items()}
        else:  # legacy format: a list of [x, y] cells, one boost each
            t.medallion_boosts = {tuple(c): 1 for c in mb}  # type: ignore[misc]
        return t

    def in_bounds(self, c: Cell) -> bool:
        return 0 <= c[0] < self.size and 0 <= c[1] < self.size

    def neighbors4(self, c: Cell) -> list[Cell]:
        x, y = c
        return [n for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)) if self.in_bounds(n)]

    def place(self, c: Cell, room_id: str) -> None:
        if room_id not in ROOMS:
            raise ValueError(f"unknown room id {room_id!r}")
        if not self.in_bounds(c):
            raise ValueError(f"cell {c} out of bounds")
        if c in self.blocked:
            raise ValueError(f"cell {c} is destabilised/blocked")
        if c in self.cells:
            raise ValueError(f"cell {c} already occupied by {self.cells[c]!r}")
        self.cells[c] = room_id

    def remove(self, c: Cell) -> None:
        self.cells.pop(c, None)
        self.tier_overrides.pop(c, None)
        self.medallion_boosts.pop(c, None)

    def connection_blocked(self, placing_id: str, neighbor_cell: Cell) -> bool:
        """True if a newly-placed `placing_id` may NOT connect to the room at
        `neighbor_cell` because a maxNeighborCount cap is already full (e.g. that
        Armoury already has its single allowed Alchemy Lab). Ported from the
        Tetriszocker editor's ILLEGAL_PLACEMENT_RULES."""
        target_id = self.cells.get(neighbor_cell)
        if target_id is None:
            return False
        for placing, target, limited, mx in MAX_NEIGHBOR_RULES:
            if placing == placing_id and target == target_id:
                count = sum(
                    1 for nn in self.neighbors4(neighbor_cell) if self.cells.get(nn) == limited
                )
                if count >= mx:
                    return True
        return False

    def is_path(self, c: Cell) -> bool:
        return self.cells.get(c) == "path"

    def is_room(self, c: Cell) -> bool:
        rid = self.cells.get(c)
        return rid is not None and rid != "path"

    def room_cells(self) -> list[Cell]:
        return [c for c, rid in self.cells.items() if rid != "path"]

    # --- connectivity / accessibility ---------------------------------------

    def accessible_cells(self, *, ignore: Cell | None = None) -> set[Cell]:
        """Occupied cells reachable from the entrance over 4-adjacent occupied
        cells. `ignore` removes a cell first (used to test what a removal would
        orphan). Drives the Generator's road-connection rule and the
        articulation / removable-room checks."""
        occupied = {c for c in self.cells if c != ignore}
        if not occupied:
            return set()
        # Seed: occupied cells adjacent to (or at) the entrance.
        seeds = [c for c in occupied if c == self.entrance or self.entrance in self.neighbors4(c)]
        seen: set[Cell] = set()
        q = deque(seeds)
        seen.update(seeds)
        while q:
            cur = q.popleft()
            for n in self.neighbors4(cur):
                if n in occupied and n not in seen:
                    seen.add(n)
                    q.append(n)
        return seen

    def accessible_room_cells(self) -> set[Cell]:
        acc = self.accessible_cells()
        return {c for c in acc if self.is_room(c)}

    def articulation_room_cells(self) -> set[Cell]:
        """Accessible rooms whose removal would orphan another room (articulation
        points). The game never strands rooms, so these are SAFE from
        destabilisation — only `removable_room_cells` (the loose ends) can go."""
        base = self.accessible_room_cells()
        if not base:
            return set()
        arts: set[Cell] = set()
        for c in self.room_cells():
            after = {rc for rc in self.accessible_cells(ignore=c) if self.is_room(rc)}
            if (base - {c}) - after:  # some other room lost access -> c is an articulation
                arts.add(c)
        return arts

    def removable_room_cells(self) -> set[Cell]:
        """Rooms destabilisation can delete: accessible rooms whose removal orphans
        nothing (the "loose ends"). A snake from the entrance has exactly one — its
        tail; every branch or loop adds another. Keep valuable rooms OFF this set
        (bury them in the chain) and leave a cheap room as the end."""
        return self.accessible_room_cells() - self.articulation_room_cells()

    # --- conversions ---------------------------------------------------------

    def effective_room_id(self, c: Cell) -> str:
        """A Garrison adjacent to a Spymaster becomes a Legion Barracks; adjacent
        to a Synthflesh Lab it becomes Transcendent Barracks (Spymaster wins ties;
        VERIFY)."""
        rid = self.cells.get(c, "")
        if rid != "garrison":
            return rid
        neigh = {self.cells.get(n) for n in self.neighbors4(c)}
        if "spymaster" in neigh:
            return "legion_barracks"
        if "synthflesh_lab" in neigh:
            return "transcendent_barracks"
        return rid

    # --- tiers ---------------------------------------------------------------

    def _generator_powering(self, cur: dict[Cell, int]) -> dict[Cell, set[Cell]]:
        """room cell -> set of Generator cells powering it. Power flows only along
        the road: a Generator feeds the Path cells it sits next to and conducts
        through connected Paths up to its tier's range, powering rooms beside those
        powered paths. A room merely touching the Generator (with no Path between)
        is NOT powered — the link must run through a Path."""
        accessible = self.accessible_cells()
        powering: dict[Cell, set[Cell]] = {}
        gens = [c for c in self.cells if self.cells[c] == "generator"]
        for g in gens:
            if g not in accessible:
                continue  # an unconnected Generator provides no power
            rng = GEN_RADIUS.get(cur.get(g, 1), 0)
            reached: set[Cell] = set()  # rooms powered only via Paths, not adjacency
            seen = {g}
            q: deque[tuple[Cell, int]] = deque()
            for p in self.neighbors4(g):
                if self.is_path(p):
                    seen.add(p)
                    q.append((p, 1))
            while q:
                cell, d = q.popleft()
                for n in self.neighbors4(cell):
                    if n in seen:
                        continue
                    if self.is_room(n):
                        reached.add(n)
                    elif self.is_path(n) and d < rng:
                        seen.add(n)
                        q.append((n, d + 1))
            for r in reached:
                powering.setdefault(r, set()).add(g)
        return powering

    def _rule_satisfied(self, c: Cell, rule, cur, powering) -> bool:
        def qualifying(source: str) -> int:
            if source == "generator":
                return sum(
                    1 for g in powering.get(c, ()) if cur.get(g, 1) >= rule.source_min_tier
                )
            return sum(
                1
                for n in self.neighbors4(c)
                if self.is_room(n)
                and self.effective_room_id(n) == source
                and cur.get(n, 1) >= rule.source_min_tier
            )

        if rule.require_all:
            return all(qualifying(s) >= 1 for s in rule.sources)
        return sum(qualifying(s) for s in rule.sources) >= rule.count

    def _compute_tier(self, c: Cell, room, cur, powering) -> int:
        tier = 1
        for rule in room.upgraded_by:
            if self._rule_satisfied(c, rule, cur, powering):
                tier = max(tier, rule.tier)
        return min(tier, self.max_tier)

    def _boosted(self, c: Cell, tier: int) -> int:
        """Apply Medallions (+1 tier each, stackable) and clamp to the max tier."""
        return min(tier + self.medallion_boosts.get(c, 0), self.max_tier)

    def tiers(self) -> dict[Cell, int]:
        """Tier of every placed room (paths excluded).

        Iterated to a fixed point because tiers can depend on neighbours' tiers
        (Generator power range; `source_min_tier` rules like Flesh Surgeon needing
        a T2+ Synthflesh). A Medallion adds +1 to any room (capped at `max_tier`)
        and propagates like a normal tier. Tiers only rise, so it converges fast."""
        rooms = self.room_cells()
        cur: dict[Cell, int] = {}
        for c in rooms:
            room = ROOMS[self.effective_room_id(c)]
            if room.fixed_tier is not None:
                cur[c] = self._boosted(c, room.fixed_tier)
            elif room.manual_tier:
                cur[c] = self._boosted(c, min(self.tier_overrides.get(c, 1), self.max_tier))
            else:
                cur[c] = self._boosted(c, 1)
        for _ in range(6):
            powering = self._generator_powering(cur)
            new = dict(cur)
            for c in rooms:
                room = ROOMS[self.effective_room_id(c)]
                if room.fixed_tier is None and not room.manual_tier:
                    new[c] = self._boosted(c, self._compute_tier(c, room, cur, powering))
            if new == cur:
                break
            cur = new
        return cur

    def room_tier(self, c: Cell) -> int:
        return self.tiers().get(c, 1)

    # --- rule checks ---------------------------------------------------------

    def connection_violations(self) -> list[tuple[Cell, Cell]]:
        """Adjacent room pairs that break a `cannot_connect` rule."""
        bad: list[tuple[Cell, Cell]] = []
        for c in self.room_cells():
            rid = self.effective_room_id(c)
            forbidden = ROOMS[rid].cannot_connect
            for n in self.neighbors4(c):
                if n > c and self.is_room(n) and self.effective_room_id(n) in forbidden:
                    bad.append((c, n))
        return bad
