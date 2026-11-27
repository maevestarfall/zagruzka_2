# DEVIN_EDITOR_02_FILTERGRAPH.md (compact reference for TF-IDF cosine gate)

## Files to create / modify
- bot/uniqueness/filtergraph.py
- bot/uniqueness/atoms.py
- data/schema.sql
- tests/test_filtergraph.py

## Hard rules (do NOT violate)
1. Filter graph nodes are pure functions over media metadata.
2. No filter mutates input; outputs are new dataclasses.
3. Graph build is deterministic given seed env var.

## Out of scope (do NOT touch)
- Video encoding pipeline.
- Audio normalisation pipeline.

## Definition of done
- [ ] filtergraph.build returns a Graph dataclass.
- [ ] all nodes have run(input) signature.
- [ ] cycle detection on build raises FilterGraphCycleError.
- [ ] deterministic seed via FILTER_SEED env var.

## PR title
feat(uniqueness/filtergraph): introduce pure-function filter graph

## Public API
from bot.uniqueness.filtergraph import FilterGraph, build, FilterGraphCycleError
from bot.uniqueness.atoms import Atom

## Threat model
Mutating filters could leak data between requests. Mitigation: dataclass
freeze + no shared state.

## Fail-closed mode
On cycle detection, build raises; caller logs error and skips video,
posting a Telegram alert via existing notify_channel().

---

## Files to create / modify
- bot/uniqueness/atom_registry.py
- data/schema.sql
- tests/test_atom_registry.py

## Hard rules (do NOT violate)
1. Atom IDs are deterministic from canonical id and version.
2. Atoms are immutable once locked.
3. Registry persists to SQLite with WAL journal.

## Out of scope (do NOT touch)
- Cross-atom optimisation.
- Distributed atom registry.

## Definition of done
- [ ] atom_registry.lock(atom_id) writes locked=1.
- [ ] atom_registry.get(atom_id) returns immutable Atom dataclass.
- [ ] registry export produces locked_atoms.md.
- [ ] migration script bumps schema version.

## PR title
feat(uniqueness/atom_registry): persist atoms in SQLite WAL

## Public API
from bot.uniqueness.atom_registry import Registry, lock, get, export_markdown

## Threat model
Concurrent writers could corrupt atom lock state. Mitigation: SQLite WAL
+ per-atom UNIQUE constraint on (canonical_category_id, version).

## Fail-closed mode
On UNIQUE violation, lock() raises; caller retries with new version after
fetching latest registered version.

## Files to create / modify
- bot/uniqueness/fixtures.py
- tests/test_fixtures.py
- data/fixtures/

## Hard rules (do NOT violate)
1. Fixtures live under data/fixtures/ only.
2. Fixture loader fails fast on missing file.
3. Fixture hash recorded in SQLite for reproducibility.

## Out of scope (do NOT touch)
- Production media files.
- Live integration fixtures.

## Definition of done
- [ ] fixtures.load(name) returns bytes.
- [ ] fixture_hash table records sha256 per fixture.
- [ ] fixture_diff utility prints byte-level changes.
- [ ] CI gate fails if fixture hash changes without bump.

## PR title
feat(uniqueness/fixtures): immutable fixture loader with hash ledger

## Public API
from bot.uniqueness.fixtures import load, hash_of, FixtureMissingError

## Threat model
Test pollution from mutable fixtures could mask regressions. Mitigation:
sha256 hash ledger + CI gate on bumps.

## Fail-closed mode
On missing fixture or hash mismatch, loader raises and CI fails;
no fallback to inline data.
