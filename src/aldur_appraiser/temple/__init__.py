"""Temple planner — a second PoE2 league-mechanic helper (Temple of Atzoatl).

Separate from the pricing/overlay core: the temple is a spatial build-and-
upgrade puzzle, not a price lookup. Phase 1 is the room dataset + a rules engine;
an interactive editor and a per-run placement advisor build on top.

The mechanic (as modelled here):
  - A 9x9 diamond grid. You build it over ~10 runs (60 energy, 6 per run).
  - Each run you draw 6 random cards (rooms + paths) and place them.
  - Placing a room connected to specific other rooms raises its tier (max 3).
  - Paths connect rooms that aren't already adjacent/connected.
  - Generators must connect to a road/path and power rooms within a Manhattan
    radius that grows with their tier (3/4/5).
  - On each entry 2-3 random tiles are Destabilised (removed on exit); accessible
    Restricted Rooms always destabilise. Goal: many surviving high-tier rooms.
"""
