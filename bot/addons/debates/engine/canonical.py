"""Canonical atom categories per part (§4 of PRAVILO_DEBAT_v3).

Loaded from part_configs/<part_id>.json at boot. Each part has:
- canonical_atom_categories: list[str] — atom category ids
- canonical_clusters: list[str] — cluster ids final spec must cover
- debater_voices: dict[str, dict] — debater_id -> {voice_label, expected_categories}
- reference_text_ids: list[str] — reference docs for TF-IDF cosine in §12.3
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PartConfig:
    part_id: str
    title: str
    canonical_atom_categories: tuple[str, ...]
    canonical_clusters: tuple[str, ...]
    debater_voices: dict
    reference_text_ids: tuple[str, ...]
    min_final_briefs: int
    max_final_briefs: int

    @classmethod
    def from_dict(cls, d: dict) -> "PartConfig":
        return cls(
            part_id=d["part_id"],
            title=d["title"],
            canonical_atom_categories=tuple(d["canonical_atom_categories"]),
            canonical_clusters=tuple(d["canonical_clusters"]),
            debater_voices=d["debater_voices"],
            reference_text_ids=tuple(d.get("reference_text_ids", [])),
            min_final_briefs=int(d.get("min_final_briefs", len(d["canonical_clusters"]))),
            max_final_briefs=int(d.get("max_final_briefs", len(d["canonical_clusters"]) + 5)),
        )


def load_part_config(part_id: str, configs_dir: Path) -> PartConfig:
    path = configs_dir / f"part_{part_id.lower()}.json"
    if not path.exists():
        raise FileNotFoundError(f"part config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return PartConfig.from_dict(json.load(f))


def list_part_ids(configs_dir: Path) -> list[str]:
    return sorted(
        p.stem.removeprefix("part_").upper()
        for p in configs_dir.glob("part_*.json")
    )
