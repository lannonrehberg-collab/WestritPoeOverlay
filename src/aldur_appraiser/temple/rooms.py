"""Temple room dataset + schema (PoE2 Vaal Temple).

The upgrade rules are ported from the Tetriszocker editor's ROOM_DATA (group
`count` / `require_all` / `min_tier`), cross-checked against the in-game Hold-Alt
tooltips and the mobalytics guide. It's the foundation everything else builds on,
so it's plain data with a typed schema and a validator.

Confirmed by the player + Hold-Alt:
  - Tier upgrades count qualifying ADJACENT rooms as a GROUP (e.g. Commander: 2/3
    adjacent Garrison-or-Transcendent together); some T3s need one of EACH type
    (`require_all`); some need a minimum source tier (Flesh Surgeon needs a T2+
    Synthflesh). The Generator/Dynamo instead POWERS rooms (directly adjacent +
    along connected paths within range), and must be connected to a road.
  - The game's "Restricted Rooms" are the Architect reward Vaults (flagged
    volatile/architect_room) — they always destabilise. A separate engine
    heuristic, chokepoint_room_cells(), flags articulation points (sole links) as
    a layout risk; that is NOT the game's "Restricted Rooms".
"""

from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = {"barrack", "production", "ritual", "utility", "generator", "special", "path"}


@dataclass(frozen=True)
class Upgrade:
    """One tier's upgrade condition (rules sourced from the Tetriszocker editor).

    A room reaches `tier` when its qualifying adjacent rooms — ids in `sources`,
    counted as a GROUP (summed across the listed types) — satisfy this rule:
      - `count`: at least `count` adjacent rooms from `sources`, OR
      - `require_all`: at least one adjacent room of EACH id in `sources`.
    `source_min_tier` requires those neighbours to be at least that tier. A source
    of "generator" is satisfied by a Generator *powering* the cell (road + range),
    not by plain adjacency.
    """

    tier: int
    sources: tuple[str, ...]
    count: int = 1
    require_all: bool = False
    source_min_tier: int = 1


def _count(tier: int, sources, n: int = 1, *, min_tier: int = 1) -> Upgrade:
    return Upgrade(tier, tuple(sources), count=n, source_min_tier=min_tier)


def _all(tier: int, sources, *, min_tier: int = 1) -> Upgrade:
    return Upgrade(tier, tuple(sources), require_all=True, source_min_tier=min_tier)


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    category: str
    bonus: str = ""                      # effect text (scales with tier)
    generator: bool = False              # powers nearby rooms; must connect to a road
    fixed_tier: int | None = None        # rooms that can't be upgraded (e.g. Vault)
    architect_room: bool = False         # unlocked via Architect; destabilises on complete
    volatile: bool = False               # destabilises (is consumed) once opened/completed
    destabilises_at: int | None = None   # craft rooms that destabilise *only* once they
    #                                      reach this tier's device (e.g. Alchemy Lab T3)
    manual_tier: bool = False            # tier comes from a player action (sacrifice /
    #                                      assassinate), not the layout -> set by hand
    upgraded_by: tuple[Upgrade, ...] = ()
    cannot_connect: tuple[str, ...] = ()
    converts: tuple[str, ...] = ()       # "<from>-><to>" conversions this room triggers
    aka: tuple[str, ...] = ()            # in-game card/tier display names (Barracks, Depot…)
    notes: tuple[str, ...] = ()


_BARRACKS = ("garrison", "legion_barracks", "transcendent_barracks")

# Which rooms may form a CONNECTION when adjacent (the in-game whitelist, ported
# from the Tetriszocker editor's allowedNeighborMap). Rooms not listed for each
# other can still sit side by side but won't connect — and a placed room needs at
# least one connection, so e.g. a Spymaster can only go next to a Path or a
# Garrison, never an Alchemy Lab.
ALLOWED_NEIGHBORS: dict[str, set[str]] = {
    "path": {"path"},
    "garrison": {"path", "commander", "armoury", "synthflesh_lab", "spymaster"},
    "legion_barracks": {"path", "commander", "armoury", "synthflesh_lab", "spymaster"},
    "transcendent_barracks": {"path", "commander", "armoury", "synthflesh_lab", "spymaster"},
    "commander": {"path", *_BARRACKS},
    "armoury": {"path", "smithy", "alchemy_lab", *_BARRACKS},
    "smithy": {"path", "golem_works", "armoury"},
    "golem_works": {"path", "smithy"},
    # The Generator only joins the road: it must sit next to a Path. (In-game the
    # valid cells hug the road network — being adjacent to a Thaumaturge/Smithy is
    # NOT enough; those are upgraded by adjacency/power separately.)
    "generator": {"path"},
    "spymaster": {"path", *_BARRACKS},
    "synthflesh_lab": {"path", "flesh_surgeon", *_BARRACKS},
    "flesh_surgeon": {"path", "synthflesh_lab"},
    "alchemy_lab": {"path", "thaumaturge", "armoury"},
    "thaumaturge": {"path", "sacrificial_chamber", "alchemy_lab", "corruption_chamber"},
    "corruption_chamber": {"path", "sacrificial_chamber", "thaumaturge"},
    "sacrificial_chamber": {"path", "generator", "corruption_chamber", "thaumaturge"},
    "treasure_vault": {"path"},
    # special / architect rooms connect via path only
    "architect_chamber": {"path"},
    "royal_access_chamber": {"path"},
    "extraction_chamber": {"path"},
    "augments_vault": {"path"},
    "currency_vault": {"path"},
    "lineage_gems_vault": {"path"},
    "tablets_vault": {"path"},
    "uniques_vault": {"path"},
}


def can_connect(a: str, b: str) -> bool:
    """Symmetric: may rooms `a` and `b` form a connection when placed adjacent?"""
    return b in ALLOWED_NEIGHBORS.get(a, ()) or a in ALLOWED_NEIGHBORS.get(b, ())


# Per-pair placement caps, ported from the Tetriszocker editor's
# ILLEGAL_PLACEMENT_RULES (type 'maxNeighborCount'). Each tuple is
# (placing, target, limited, max): when placing `placing` next to a `target`
# room, that target may already have at most `max` neighbours of type `limited`
# — otherwise the connection to it is BLOCKED. If a placement's only possible
# connection is blocked, it's illegal. This is what stops a 2nd Alchemy Lab from
# attaching to an Armoury that already has one (keeps the snake clean).
MAX_NEIGHBOR_RULES: tuple[tuple[str, str, str, int], ...] = (
    ("thaumaturge", "corruption_chamber", "thaumaturge", 1),
    ("armoury", "garrison", "armoury", 1),
    ("armoury", "garrison", "synthflesh_lab", 1),
    ("spymaster", "garrison", "spymaster", 1),
    ("spymaster", "garrison", "commander", 1),
    ("commander", "garrison", "spymaster", 1),
    ("alchemy_lab", "armoury", "alchemy_lab", 1),
    ("synthflesh_lab", "garrison", "synthflesh_lab", 1),
    ("synthflesh_lab", "garrison", "armoury", 1),
    # A Garrison feeding a Commander can't also be converted by a Synthflesh Lab
    # (ALT/in-game: extends the {armoury⊥synthflesh}, {commander⊥spymaster}
    # mutual-exclusivity pattern; missing from the Tetriszocker port).
    ("synthflesh_lab", "garrison", "commander", 1),
    ("commander", "garrison", "synthflesh_lab", 1),
)


def is_volatile(room: Room) -> bool:
    """True if the room is consumed/destabilised once completed (Treasure Vault,
    Architect reward rooms) — placing it gives a one-time reward but it won't
    persist, so it's a poor pick for a lasting, re-runnable temple."""
    return room.volatile or room.architect_room


def destabilises(room: Room, tier: int = 1) -> bool:
    """Will this room destabilise? Vaults/Architect rooms always do (`is_volatile`);
    some craft rooms (Alchemy Lab, Thaumaturge, …) only once they reach their
    device tier (e.g. Alchemy Lab T3's Soul Core Infuser destabilises on use)."""
    if is_volatile(room):
        return True
    return room.destabilises_at is not None and tier >= room.destabilises_at


def can_orphan(room: Room) -> bool:
    """True for the Architect-console rooms (the Vaults, Royal Access, Extraction
    and the Architect's Chamber itself). Unlike normal rooms — which must join the
    road network from the entrance — these may legally sit disconnected (orphans),
    so they shouldn't be flagged as stranded or forced to connect."""
    return room.category == "special"


def _card_name_index() -> dict[str, str]:
    """lower-cased in-game label (room name + every `aka`) -> room id. The drawn
    cards use alternate names (Bronzeworks=Smithy, Dynamo=Generator, Chamber of
    Souls=Alchemy Lab, Sealed Vault=Treasure Vault, Surgeon's Ward=Flesh Surgeon)."""
    index: dict[str, str] = {}
    for rid, room in ROOMS.items():
        index[room.name.lower()] = rid
        for alias in room.aka:
            index[alias.lower()] = rid
    return index


def card_name_to_id(text: str, *, cutoff: float = 72.0) -> str | None:
    """Fuzzy-map an OCR'd card label to a room id (None if nothing matches well).
    Uses the same matcher as the pricing OCR so light OCR noise is tolerated."""
    from rapidfuzz import fuzz, process

    text = text.strip().lower()
    if not text:
        return None
    index = _card_name_index()
    match = process.extractOne(text, index.keys(), scorer=fuzz.ratio, score_cutoff=cutoff)
    return index[match[0]] if match else None


ROOMS: dict[str, Room] = {
    # --- barracks line -------------------------------------------------------
    "garrison": Room(
        id="garrison", name="Garrison", category="barrack",
        bonus="increased Number of Monster Packs (T1 8% / T2 12% / T3 16%); "
              "Normal Monster Effectiveness (T2 10% / T3 13%)",
        aka=("Barracks", "Guardhouse"),
        # Hold-Alt "Upgraded by": Commander + Armoury raise its tier; Synthflesh and
        # Spymaster instead CONVERT it (Transcendent/Legion, handled via `converts`).
        upgraded_by=(
            _count(2, ["commander", "armoury"], 1),
            _all(3, ["commander", "armoury"]),  # both Commander AND Armoury
        ),
        converts=("garrison->transcendent_barracks", "garrison->legion_barracks"),
        notes=(
            "An adjacent Synthflesh Lab converts it to Transcendent Barracks, an "
            "adjacent Spymaster converts it to Legion Barracks.",
        ),
    ),
    "commander": Room(
        id="commander", name="Commander's Chamber", category="barrack",
        bonus="Rare Monsters have increased Effectiveness (T1 10%)",
        upgraded_by=(  # adjacent barracks count together (Garrison + Transcendent)
            _count(2, ["garrison", "transcendent_barracks"], 2),
            _count(3, ["garrison", "transcendent_barracks"], 3),
        ),
        cannot_connect=("spymaster",),
        notes=(
            "Hold-Alt: upgraded by adjacent Garrison or Transcendent Barracks "
            "(NOT Legion); they count together. Upgrades adjacent Garrisons in turn.",
        ),
    ),
    "legion_barracks": Room(
        id="legion_barracks", name="Legion Barracks", category="barrack",
        bonus="more Rare Monsters; High chance for an Advanced Medallion",
        notes=("Created when a Spymaster converts an adjacent Garrison.",),
    ),
    "transcendent_barracks": Room(
        id="transcendent_barracks", name="Transcendent Barracks", category="barrack",
        bonus="more Magic Monsters (T3 35%)",
        upgraded_by=(  # VERIFY exact counts (Hold-Alt: Generator + Synthflesh)
            _count(2, ["generator", "synthflesh_lab"], 1),
            _count(3, ["generator", "synthflesh_lab"], 2),
        ),
        notes=("Created when a Synthflesh Lab converts an adjacent Garrison.",),
    ),
    # --- production line -----------------------------------------------------
    "armoury": Room(
        id="armoury", name="Armoury", category="production",
        bonus="Humanoid Monsters have increased Effectiveness; contains Equipment",
        aka=("Depot",),
        upgraded_by=(
            _count(2, ["smithy", "alchemy_lab"], 1),
            _all(3, ["smithy", "alchemy_lab"]),  # both Smithy AND Alchemy
        ),
    ),
    "smithy": Room(
        id="smithy", name="Smithy", category="production",
        bonus="Chests have increased Item Rarity (T2 30%); Vaal Infuser",
        aka=("Bronzeworks",),  # in-game card / T1 display name
        upgraded_by=(
            _count(2, ["golem_works", "generator"], 1),
            _count(3, ["golem_works", "generator"], 2),
        ),
    ),
    "golem_works": Room(
        id="golem_works", name="Golem Works", category="production",
        bonus="increased Effect of Temple Mods from Garrisons/Commanders/"
              "Armouries/Smithies/Legion Barracks (T2 15%); adds High Priest",
        aka=("Workshop",),  # in-game card name
        upgraded_by=(_count(2, ["generator"], 1), _count(3, ["generator"], 2)),
    ),
    "synthflesh_lab": Room(
        id="synthflesh_lab", name="Synthflesh Lab", category="production",
        bonus="Monsters grant increased Experience (T1 10% / T2 20%)",
        aka=("Prosthetic Research",),  # the card that places a Synthflesh Lab
        cannot_connect=("spymaster",),
        converts=("garrison->transcendent_barracks",),
        upgraded_by=(
            _count(2, ["flesh_surgeon", "generator"], 1),
            _count(3, ["flesh_surgeon", "generator"], 2),
        ),
    ),
    "flesh_surgeon": Room(
        id="flesh_surgeon", name="Flesh Surgeon's Ward", category="production",
        bonus="Unique Monsters have increased Effectiveness (T1 10%); Limb "
              "Modification; T3 Transcension Device",
        aka=("Surgeon's Ward",),  # in-game card name
        destabilises_at=3,  # T3 Transcension Device destabilises the room on use
        upgraded_by=(
            _count(2, ["synthflesh_lab"], 1),
            _count(3, ["synthflesh_lab"], 1, min_tier=2),  # needs a T2+ Synthflesh
        ),
        notes=("T3 needs an adjacent Tier-2+ Synthflesh Lab (e.g. one powered by "
               "a Generator).",),
    ),
    # --- generator -----------------------------------------------------------
    "generator": Room(
        id="generator", name="Generator", category="generator", generator=True,
        bonus="Construct Monster Effectiveness (T1 10% / T2 25% / T3 35%); adds "
              "Corrupted Abomination; powers Smithy/Golem Works/Synthflesh/Transcendent",
        aka=("Dynamo", "Shrine of Empowerment", "Solar Nexus"),  # T1/T2/T3 names
        upgraded_by=(
            _count(2, ["thaumaturge", "sacrificial_chamber"], 1),
            _all(3, ["thaumaturge", "sacrificial_chamber"]),  # both
        ),
        notes=(
            "Must be connected to a Road/Path to function.",
            "Powers directly adjacent rooms, and conducts along connected paths to "
            "rooms beside them; range grows with tier (3/4/5).",
        ),
    ),
    # --- ritual / corruption line -------------------------------------------
    "thaumaturge": Room(
        id="thaumaturge", name="Thaumaturge's Laboratory", category="ritual",
        bonus="increased Effect of Temple Mods from Corruption Chambers/Treasure "
              "Vaults/Sacrificial Chambers (T1 8% / T2 15% / T3 22%); "
              "adds Quadrilla Sergeant",
        destabilises_at=3,  # T3 Gem Corrupter destabilises the room on use
        upgraded_by=(
            _count(2, ["sacrificial_chamber"], 1),
            _count(3, ["sacrificial_chamber"], 2),
        ),
        notes=(
            "T3 holds a Gem Corrupter device; using it destabilises the room (one-use).",
        ),
    ),
    "alchemy_lab": Room(
        id="alchemy_lab", name="Alchemy Lab", category="ritual",
        bonus="increased Rarity of Items and Gold found (T1 10% / T2 25%); T1-2 "
              "Soul Core Cache, T3 Soul Core Infuser (-> Core Destabiliser)",
        aka=("Chamber of Souls",),  # in-game card name
        destabilises_at=3,  # ALT-verified: T3 Soul Core Infuser destabilises on use
        upgraded_by=(_count(2, ["thaumaturge"], 1), _count(3, ["thaumaturge"], 2)),
    ),
    "corruption_chamber": Room(
        id="corruption_chamber", name="Corruption Chamber", category="ritual",
        bonus="Rare Monsters have a chance for an additional Modifier; T1-2 "
              "Corruption Altar, T3 Corruption Instiller (-> Architect's Orb)",
        aka=("Crimson Hall",),  # in-game card name
        destabilises_at=3,  # T3 Corruption Instiller destabilises the room on use
        upgraded_by=(
            _count(2, ["sacrificial_chamber", "thaumaturge"], 1),
            _count(3, ["sacrificial_chamber", "thaumaturge"], 2),
        ),
    ),
    "sacrificial_chamber": Room(
        id="sacrificial_chamber", name="Sacrificial Chamber", category="ritual",
        bonus="increased number of Rare Chests; T3 Morphology Mechanism "
              "(-> Vaal Cultivation Orb)",
        aka=("Altar of Sacrifice",),  # in-game card name
        manual_tier=True,
        destabilises_at=3,  # T3 Morphology Mechanism destabilises the room on use
        notes=(
            "Upgraded by SACRIFICING other placed rooms (irreversible), not by "
            "adjacency. Upgrades adjacent Generator/Thaumaturge/Corruption Chamber.",
            "Holds a Morphology Mechanism device; using it destabilises the room.",
        ),
    ),
    "treasure_vault": Room(
        id="treasure_vault", name="Treasure Vault", category="ritual",
        bonus="25% increased Rarity of Items Dropped by Monsters",
        fixed_tier=1,
        volatile=True,
        aka=("Sealed Vault",),
        notes=(
            "Contains valuable Chests based on surrounding rooms.",
            "Room destabilises once the central Vault is opened — one-use.",
        ),
    ),
    # --- utility -------------------------------------------------------------
    "spymaster": Room(
        id="spymaster", name="Spymaster's Study", category="utility",
        bonus="8% increased Effect of Temple Mods (per tier) from Generators, "
              "Synthflesh Labs, Flesh Surgeons, Transcendent Barracks, and "
              "Alchemy Labs.",
        manual_tier=True,
        cannot_connect=("commander", "synthflesh_lab"),
        converts=("garrison->legion_barracks",),
        notes=(
            "Room Tier can be increased by defeating other Spymasters (ALT-verified), "
            "not by adjacency.",
        ),
    ),
    # --- path connector ------------------------------------------------------
    "path": Room(
        id="path", name="Path", category="path",
        bonus="No bonus; expands the path / connects rooms.",
    ),
    # --- Architect's Chamber (spawns on the grid; fight the Architect here) ---
    "architect_chamber": Room(
        id="architect_chamber", name="Architect's Chamber", category="special",
        bonus="The Vaal Architect spawns here; defeating it unlocks Xipocado's "
              "Console (place Restricted Rooms / Vaults) and Medallions.",
        notes=("Generates on the grid; defeating the Architect causes greater "
               "destabilisation (and unlocks Atziri via the Royal Access Chamber).",),
    ),
    # --- Architect-unlocked special rooms (destabilise after completion) -----
    "royal_access_chamber": Room(
        id="royal_access_chamber", name="Royal Access Chamber", category="special",
        architect_room=True, bonus="Unlocks Atziri's Chamber",
    ),
    "extraction_chamber": Room(
        id="extraction_chamber", name="Extraction Chamber", category="special",
        architect_room=True,
        bonus="Extraction Workbench (destroys an item, returns its Augments; "
              "-> Orb of Extraction)",
    ),
    "augments_vault": Room(
        id="augments_vault", name="Augments Vault", category="special",
        architect_room=True, bonus="Rune Cache (a high-level endgame Rune)",
    ),
    "currency_vault": Room(
        id="currency_vault", name="Currency Vault", category="special",
        architect_room=True, aka=("Kishara's Vault",),
        bonus="Rare Treasury Chests of Currency",
    ),
    "lineage_gems_vault": Room(
        id="lineage_gems_vault", name="Lineage Gems Vault", category="special",
        architect_room=True, aka=("Vault of Reverence",),
        bonus="A Historic Chest with a random Lineage Support Gem",
    ),
    "tablets_vault": Room(
        id="tablets_vault", name="Tablets Vault", category="special",
        architect_room=True, aka=("Tablet Research Vault",),
        bonus="Corrupted Precursor Machine (corrupts a Precursor Tablet; "
              "-> Ancient Infuser)",
    ),
    "uniques_vault": Room(
        id="uniques_vault", name="Uniques Vault", category="special",
        architect_room=True, aka=("Ancient Reliquary Vault",),
        bonus="A Display granting a random Unique Item (e.g. Atziri's Disdain)",
    ),
}


def validate(rooms: dict[str, Room] = ROOMS) -> list[str]:
    """Return a list of dataset problems (empty == OK).

    Checks referential integrity so a typo'd room id surfaces immediately. Both
    sides of a `cannot_connect` must agree (the constraint is symmetric).
    """
    problems: list[str] = []
    for rid, room in rooms.items():
        if rid != room.id:
            problems.append(f"{rid}: key != room.id ({room.id!r})")
        if room.category not in CATEGORIES:
            problems.append(f"{rid}: unknown category {room.category!r}")
        for rule in room.upgraded_by:
            if rule.tier not in (2, 3):
                problems.append(f"{rid}: upgrade rule has odd tier {rule.tier}")
            if not rule.sources:
                problems.append(f"{rid}: upgrade rule for T{rule.tier} has no sources")
            for s in rule.sources:
                if s not in rooms:
                    problems.append(f"{rid}: upgraded_by unknown room {s!r}")
        for other in room.cannot_connect:
            if other not in rooms:
                problems.append(f"{rid}: cannot_connect unknown room {other!r}")
            elif rid not in rooms[other].cannot_connect:
                problems.append(f"{rid}: cannot_connect {other!r} is not symmetric")
        for conv in room.converts:
            src, _, dst = conv.partition("->")
            if src not in rooms or dst not in rooms:
                problems.append(f"{rid}: converts references unknown room(s) {conv!r}")
    return problems
