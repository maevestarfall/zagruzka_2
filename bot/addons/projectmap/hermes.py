# Project-map ⇄ Hermes bridge: analyse the project and update the base.
# Co-authored with CoCo
"""Let Hermes analyse the project map and fold updates into the base.

«База» here == the persisted export document (:func:`store.export_base`).
Hermes:

1. Rescans the project source into a fresh graph (``store.set_graph``).
2. Records an experience entry so its findings show up in the Hermes doc.
3. Promotes strong scanned call-relations into curated «связки» when the
   owner hasn't added them yet — so the exportable base grows over time.

Everything is best-effort and offline; failures never propagate.
"""

from __future__ import annotations

import logging

from . import store
from . import scan
from . import installer

logger = logging.getLogger(__name__)


def rebuild_graph(root: str | None = None) -> dict:
    """Rescan the project and cache the graph. Returns the graph dict."""
    target = root or installer.project_root()
    graph = scan.scan_project(target)
    store.set_graph(graph)
    return graph


def _promote_edges(graph: dict, *, max_new: int = 40) -> int:
    """Seed curated edges from the scanned graph (only fills gaps)."""
    existing = store.get_edges()
    have = {(e.get("from"), e.get("to")) for e in existing}
    added = 0
    for e in graph.get("edges", []):
        key = (e.get("source"), e.get("target"))
        if key in have:
            continue
        store.add_edge(e["source"], e["target"], note="auto (Hermes scan)", order=e.get("order", 0))
        have.add(key)
        added += 1
        if added >= max_new:
            break
    return added


def analyze_and_update(root: str | None = None, *, promote: bool = True) -> dict:
    """Full Hermes pass: rescan → record experience → grow the base.

    Returns a small summary dict for the UI / logs.
    """
    graph = rebuild_graph(root)
    promoted = _promote_edges(graph) if promote else 0

    summary = {
        "nodes": graph.get("node_count", 0),
        "edges": graph.get("edge_count", 0),
        "promoted": promoted,
        "curated_total": len(store.get_edges()),
    }

    # Log into Hermes experience so it surfaces in the Hermes document.
    try:
        from ..hermes_experience import record as _hermes_record
        _hermes_record(
            "general",
            f"Карта проекта пересобрана: {summary['nodes']} функций, "
            f"{summary['edges']} связей.",
            outcome="ok",
            lesson=(
                "Использую карту связок, чтобы вызывать функции в правильном "
                "порядке (сначала → потом) и не терять контекст между шагами."
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug("hermes_experience record skipped", exc_info=True)

    return summary


def maybe_auto_refresh(*, max_age: int = 3600) -> bool:
    """If auto-mode is on and the cached graph is stale, refresh it.

    Called lazily (e.g. when the web map is opened). Returns True if a
    refresh ran.
    """
    if not store.get_auto():
        return False
    if store.graph_age() < max_age and store.get_graph():
        return False
    try:
        analyze_and_update()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("projectmap auto refresh failed")
        return False
