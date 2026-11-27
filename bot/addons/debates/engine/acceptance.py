# Debate phase gates A/B/C acceptance checks with provocation scoring.
# Co-authored with CoCo
"""Phase gates A/B/C (§12 of PRAVILO_DEBAT_v3).

Each gate returns (passed: bool, report: dict). Reports are persisted into
`phase_gates` table and returned to debaters via GET /state.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Iterable

from .canonical import PartConfig


# ---------- phase gate A ----------

def phase_gate_a(con, door_id: str, part_cfg: PartConfig) -> tuple[bool, dict]:
    rep: dict = {"phase": "phase_gate_a", "checks": []}

    # 1. coverage — every canonical category has ≥1 atom version per debater
    cats_per_debater: dict[str, set] = {}
    cur = con.execute(
        """SELECT canonical_category_id, author_debater_id
           FROM atoms WHERE door_id = ?""", (door_id,))
    for r in cur.fetchall():
        cats_per_debater.setdefault(r["author_debater_id"], set()).add(
            r["canonical_category_id"])
    missing_per_debater = {}
    for deb, cats in cats_per_debater.items():
        miss = sorted(set(part_cfg.canonical_atom_categories) - cats)
        if miss:
            missing_per_debater[deb] = miss
    coverage_ok = not missing_per_debater and len(cats_per_debater) >= 2
    rep["checks"].append({
        "name": "atom_coverage_per_debater",
        "passed": coverage_ok,
        "detail": missing_per_debater or "100% per debater",
    })

    # 2. disagree-at-first-pick ≥40% of canonical categories
    first_picks: dict[str, dict] = {}  # cat -> {debater_id: pick}
    cur = con.execute(
        """SELECT canonical_category_id, author_debater_id, pick, version
           FROM atoms WHERE door_id = ? ORDER BY canonical_category_id, version""",
        (door_id,))
    for r in cur.fetchall():
        fp = first_picks.setdefault(r["canonical_category_id"], {})
        if r["author_debater_id"] not in fp:
            fp[r["author_debater_id"]] = r["pick"]
    disagree_count = sum(
        1 for cat, picks in first_picks.items()
        if len(picks) >= 2 and len(set(picks.values())) > 1)
    total_cats = len(part_cfg.canonical_atom_categories)
    disagree_ratio = disagree_count / max(total_cats, 1)
    rep["checks"].append({
        "name": "first_pick_disagree_ratio",
        "passed": disagree_ratio >= 0.40,
        "detail": f"{disagree_count}/{total_cats} = {disagree_ratio:.2f} (need ≥0.40)",
    })

    # 3. each locked atom has ≥1 adversarial w/ revision_required=true from the
    # opposing debater
    adv_per_atom: dict[str, set] = {}
    cur = con.execute(
        """SELECT m.payload_json, m.debater_id FROM msgs m
           WHERE m.door_id = ? AND m.kind = 'atom-adversarial'
             AND m.rejected_reason IS NULL""", (door_id,))
    for r in cur.fetchall():
        payload = json.loads(r["payload_json"])
        if payload.get("revision_required"):
            atom_id = payload.get("target_atom_id")
            if atom_id:
                adv_per_atom.setdefault(atom_id, set()).add(r["debater_id"])
    locked_atoms = con.execute(
        "SELECT canonical_category_id, version FROM atoms WHERE door_id = ? AND locked = 1",
        (door_id,)).fetchall()
    missing_adv = []
    for r in locked_atoms:
        atom_id = f"atom_{r['canonical_category_id']}_v{r['version']}"
        if not adv_per_atom.get(atom_id):
            missing_adv.append(atom_id)
    rep["checks"].append({
        "name": "adversarial_per_locked_atom",
        "passed": len(missing_adv) == 0 and len(locked_atoms) > 0,
        "detail": (missing_adv[:5] if missing_adv else
                   f"{len(locked_atoms)} locked, all have adversarial"),
    })

    # 4. provocation distribution — avg(score) ≥1.8 across first 10 atoms each side
    scores = []
    for deb in cats_per_debater.keys():
        cur = con.execute(
            """SELECT provocation_level FROM atoms
               WHERE door_id = ? AND author_debater_id = ?
               ORDER BY version, canonical_category_id LIMIT 10""",
            (door_id, deb))
        s = sum({"LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(r["provocation_level"], 1)
                for r in cur.fetchall())
        n = max(cur.rowcount, 1) or 10
        scores.append(s / n)
    avg_prov = sum(scores) / max(len(scores), 1)
    rep["checks"].append({
        "name": "avg_provocation_level",
        "passed": avg_prov >= 1.8,
        "detail": f"{avg_prov:.2f} (need ≥1.8)",
    })

    # 5. NRN cap per debater ≤0.30
    nrn_failures = []
    cur = con.execute(
        """SELECT debater_id,
                  SUM(CASE WHEN payload_json LIKE '%NO_REVISION_NEEDED%' THEN 1 ELSE 0 END) AS nrn,
                  COUNT(*) AS tot
           FROM msgs
           WHERE door_id = ? AND kind = 'atom-revision' AND rejected_reason IS NULL
           GROUP BY debater_id""", (door_id,))
    for r in cur.fetchall():
        tot = r["tot"]
        nrn = r["nrn"]
        if tot >= 10 and nrn / tot > 0.30:
            nrn_failures.append({"debater": r["debater_id"], "nrn": nrn, "tot": tot})
    rep["checks"].append({
        "name": "nrn_rate_cap",
        "passed": not nrn_failures,
        "detail": nrn_failures or "all ≤0.30",
    })

    # 6. disagree-rate ≥0.25 per debater
    dis_failures = []
    cur = con.execute(
        """SELECT debater_id,
                  SUM(CASE WHEN payload_json LIKE '%"verdict":"DISAGREE"%' THEN 1 ELSE 0 END) AS d,
                  COUNT(*) AS tot
           FROM msgs WHERE door_id = ? AND kind = 'atom-critique'
             AND rejected_reason IS NULL
           GROUP BY debater_id""", (door_id,))
    for r in cur.fetchall():
        tot = r["tot"]
        d = r["d"]
        if tot >= 10 and d / tot < 0.25:
            dis_failures.append({"debater": r["debater_id"], "d": d, "tot": tot})
    rep["checks"].append({
        "name": "disagree_rate_floor",
        "passed": not dis_failures,
        "detail": dis_failures or "all ≥0.25",
    })

    rep["passed"] = all(c["passed"] for c in rep["checks"])
    return rep["passed"], rep


# ---------- phase gate B ----------

def phase_gate_b(con, door_id: str, part_cfg: PartConfig) -> tuple[bool, dict]:
    rep: dict = {"phase": "phase_gate_b", "checks": []}

    # 1. each canonical category covered by ≥1 cluster
    cur = con.execute(
        """SELECT payload_json FROM msgs
           WHERE door_id = ? AND kind = 'cluster-propose'
             AND rejected_reason IS NULL""", (door_id,))
    locked_atom_ids_by_cluster: dict[str, set[str]] = {}
    for r in cur.fetchall():
        p = json.loads(r["payload_json"])
        cluster_id = p.get("cluster_id")
        ids = p.get("locked_atom_ids", []) or []
        if cluster_id:
            locked_atom_ids_by_cluster.setdefault(cluster_id, set()).update(ids)
    covered_cats: set[str] = set()
    for ids in locked_atom_ids_by_cluster.values():
        for atom_id in ids:
            # atom_<cat>_vN -> cat
            m = re.match(r"atom_(.+)_v\d+$", atom_id)
            if m:
                covered_cats.add(m.group(1))
    missing = sorted(set(part_cfg.canonical_atom_categories) - covered_cats)
    rep["checks"].append({
        "name": "atom_category_coverage_by_clusters",
        "passed": not missing,
        "detail": missing[:5] or "100%",
    })

    # 2. each cluster-propose passes §8 (already enforced on POST, so just count
    # how many proposed clusters there are)
    rep["checks"].append({
        "name": "cluster_proposes_count",
        "passed": len(locked_atom_ids_by_cluster) >= max(part_cfg.min_final_briefs, 3),
        "detail": f"{len(locked_atom_ids_by_cluster)} clusters (need ≥{max(part_cfg.min_final_briefs, 3)})",
    })

    # 3. ≥30% clusters had a SUBSTANTIVE revision
    cur = con.execute(
        """SELECT COUNT(DISTINCT json_extract(payload_json, '$.cluster_id')) AS subs_clusters
           FROM msgs WHERE door_id = ? AND kind = 'cluster-revision'
             AND rejected_reason IS NULL
             AND payload_json LIKE '%SUBSTANTIVE%'""", (door_id,))
    r = cur.fetchone()
    subs = r["subs_clusters"] or 0
    total_clusters = max(len(locked_atom_ids_by_cluster), 1)
    ratio = subs / total_clusters
    rep["checks"].append({
        "name": "substantive_cluster_revision_ratio",
        "passed": ratio >= 0.30,
        "detail": f"{subs}/{total_clusters} = {ratio:.2f} (need ≥0.30)",
    })

    rep["passed"] = all(c["passed"] for c in rep["checks"])
    return rep["passed"], rep


# ---------- phase gate C ----------

def phase_gate_c(con, door_id: str, part_cfg: PartConfig,
                 reference_dir: Path) -> tuple[bool, dict]:
    rep: dict = {"phase": "phase_gate_c", "checks": []}

    # collect final-propose contents per debater
    cur = con.execute(
        """SELECT debater_id, body_text FROM msgs
           WHERE door_id = ? AND kind = 'final-propose' AND rejected_reason IS NULL""",
        (door_id,))
    finals = {r["debater_id"]: r["body_text"] for r in cur.fetchall()}
    if len(finals) < 2:
        rep["checks"].append({"name": "final_propose_per_debater_present",
                              "passed": False,
                              "detail": f"only {len(finals)} final-propose(s); need 2"})
        rep["passed"] = False
        return False, rep

    # 1+2+3: structural + banned + density already enforced on POST, but
    # re-run since they're cheap and gate-C is canonical
    from . import banned_phrases as _bp
    from .enforcement import final_propose_violations
    for deb, text in finals.items():
        vs = final_propose_violations(text, part_cfg)
        if vs:
            rep["checks"].append({"name": f"structural_{deb}", "passed": False,
                                  "detail": vs[:5]})
        else:
            rep["checks"].append({"name": f"structural_{deb}", "passed": True})
        bp = _bp.find_banned(text)
        rep["checks"].append({"name": f"banned_phrases_{deb}",
                              "passed": not bp,
                              "detail": [b["detail"] for b in bp[:5]] if bp else "0 hits"})
        ok, anchors = _bp.prose_density_ok(text, min_anchors=10)
        rep["checks"].append({"name": f"prose_density_{deb}",
                              "passed": ok,
                              "detail": f"{anchors} anchors (need ≥10)"})

    # 4. atom coverage per brief — every locked atom_id appears ≥1 time in
    # "Locked atoms in this cluster:" lines across the final spec
    locked_atoms = con.execute(
        """SELECT canonical_category_id, version FROM atoms
           WHERE door_id = ? AND locked = 1""", (door_id,)).fetchall()
    locked_ids = {f"atom_{r['canonical_category_id']}_v{r['version']}"
                  for r in locked_atoms}
    for deb, text in finals.items():
        cited = set()
        for m in re.finditer(r"Locked atoms in this cluster:\s*\**\s*([^\n]+)",
                             text, flags=re.IGNORECASE):
            for tok in re.findall(r"atom_[a-z0-9_\-]+_v\d+", m.group(1)):
                cited.add(tok)
        missing = sorted(locked_ids - cited)
        rep["checks"].append({"name": f"locked_atom_coverage_{deb}",
                              "passed": len(missing) == 0,
                              "detail": missing[:5] or f"{len(cited)}/{len(locked_ids)}"})

    # 5. TF-IDF cosine ≥0.55 vs editor 01/02 reference
    cosines = {}
    try:
        ref_text = _load_reference(reference_dir, part_cfg.reference_text_ids)
        for deb, text in finals.items():
            cosines[deb] = _tfidf_cosine(text, ref_text)
        cosine_ok = all(c >= 0.55 for c in cosines.values())
        rep["checks"].append({"name": "tfidf_cosine_vs_editor",
                              "passed": cosine_ok,
                              "detail": {k: round(v, 3) for k, v in cosines.items()}})
    except Exception as e:
        rep["checks"].append({"name": "tfidf_cosine_vs_editor",
                              "passed": False,
                              "detail": f"cosine error: {e}"})

    # 6. A3 has ≥0.4*N rows
    target_rows = math.ceil(0.40 * len(part_cfg.canonical_atom_categories))
    for deb, text in finals.items():
        a3 = _between(text, "## A3.", "## A4.")
        rows = len(re.findall(r"\n\s*\|[^\n]+\|", a3 or "")) - 2 if a3 else 0
        # subtract 2 for table header + separator
        rep["checks"].append({"name": f"a3_rows_{deb}",
                              "passed": rows >= target_rows,
                              "detail": f"{max(rows,0)} rows (need ≥{target_rows})"})

    # 7. PR titles unique + match regex
    pr_re = re.compile(r"\*\*PR title:\*\*\s*`([^`]+)`")
    title_re = re.compile(r"^[a-z][a-z0-9_\-/()\s]{5,80}$")
    for deb, text in finals.items():
        titles = pr_re.findall(text)
        dups = [t for t in set(titles) if titles.count(t) > 1]
        bad = [t for t in titles if not title_re.match(t)]
        rep["checks"].append({"name": f"pr_titles_{deb}",
                              "passed": not dups and not bad and len(titles) >= 3,
                              "detail": {"dups": dups[:3], "bad": bad[:3],
                                         "count": len(titles)}})

    rep["passed"] = all(c["passed"] for c in rep["checks"])
    rep["cosines"] = cosines
    return rep["passed"], rep


def _between(text: str, start: str, end: str):
    i = text.find(start)
    if i < 0:
        return None
    j = text.find(end, i + len(start))
    return text[i: j if j > 0 else len(text)]


def _load_reference(reference_dir: Path, ids: Iterable[str]) -> str:
    parts = []
    for rid in ids:
        path = reference_dir / f"{rid}.md"
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    if not parts:
        # fallback: tiny pseudo-reference so cosine is computable
        return ("implementer brief files create hard rules out of scope "
                "definition of done pr title public api fixtures acceptance")
    return "\n\n".join(parts)


def _tfidf_cosine(a: str, b: str) -> float:
    """Lightweight TF-IDF cosine without sklearn dependency.

    Uses bigram word features after simple tokenization. Computes IDF as
    log(2 / df) over the 2-doc corpus, then cosine of weighted vectors.
    """
    import math
    from collections import Counter

    def tokens(s: str) -> list[str]:
        return re.findall(r"[a-zA-Zа-яА-Я0-9_]{2,}", s.lower())

    def ngrams(toks: list[str], n: int) -> list[str]:
        return [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]

    ta, tb = tokens(a), tokens(b)
    feats = ngrams(ta, 1) + ngrams(ta, 2)
    featsb = ngrams(tb, 1) + ngrams(tb, 2)
    fa = Counter(feats)
    fb = Counter(featsb)
    all_feats = set(fa) | set(fb)
    # IDF: feature in both = log(2/2)=0 -> nearly zero weight (uninformative)
    # We bump with +1 smoothing to keep weights nonzero
    idf = {f: math.log(2 / (1 + (1 if f in fa else 0) + (1 if f in fb else 0)) + 1.0)
           for f in all_feats}
    va = {f: fa.get(f, 0) * idf[f] for f in all_feats}
    vb = {f: fb.get(f, 0) * idf[f] for f in all_feats}
    dot = sum(va[f] * vb[f] for f in all_feats)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
