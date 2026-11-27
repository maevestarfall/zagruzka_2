# Context-relay Phases 1-5: the TEMPORAL RELAY multi-agent pipeline.
# Co-authored with CoCo
"""Runs the owner's TEMPORAL RELAY over the bot's LLM.

Every agent is a fresh, independent one-shot call (``agent.oneshot_chat``)
— that IS the rotation: no agent accumulates context, so none degrades.

Phase 1  Decomposer   → split ТЗ into parts (shape from calibration).
Phase 2  5 Coders      → N fresh versions per part (diverse "peaks").
Phase 3  2 Comparators → pick the golden version per part (best of N).
Phase 4  Refiner       → patch gold with better bits from the non-gold ones.
Phase 5  Assembler     → assemble gold per contracts + set next-run priorities.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from . import store

logger = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


async def _noop(_msg: str) -> None:
    return None


# Round-robin cursor per role, so multiple orchestra slots on one role share load.
_RR: dict[str, int] = {}


async def _agent(system: str, user: str, purpose: str, temperature: float = 0.3,
                 role: str | None = None) -> str:
    """Run one agent. If «🎼 Оркестр» has a slot assigned to ``role``, use that
    slot's model (round-robin across slots for the role); otherwise fall back
    to the default brain via ``oneshot_chat``."""
    from ...agent import oneshot_chat
    if role:
        try:
            from . import orchestra
            slots = orchestra.slots_for_role(role)
        except Exception:  # noqa: BLE001
            slots = []
        if slots:
            n = _RR.get(role, 0) % len(slots)
            _RR[role] = n + 1
            s = slots[n]
            try:
                from ...agent import oneshot_chat_with
                out = await oneshot_chat_with(
                    s.get("api_key", ""), s.get("base_url", ""), s.get("model", ""),
                    system, user, purpose=purpose, temperature=temperature,
                )
                if out.strip():
                    return out
            except Exception as e:  # noqa: BLE001
                logger.warning("orchestra slot for role %s failed: %s; fallback", role, e)
    return await oneshot_chat(system, user, purpose=purpose, temperature=temperature)


# ─── Phase 1 — Decomposer ──────────────────────────────────────────────

async def _decompose(tz: str, layout: dict) -> list[dict]:
    n = layout["parts"]
    sys = (
        "Ты — Декомпозитор. Раздели ТЗ РОВНО на N частей, каждая размером под "
        "один пик модели. Для каждой части задай контракт. Верни СТРОГО JSON-массив "
        'объектов: [{"name":"A","spec":"что сделать","inputs":"вход","outputs":"выход",'
        '"accept":"критерии приёмки"}]. Никакого текста вне JSON.'
    )
    user = f"N = {n}.\n\nТЗ:\n{tz}"
    raw = await _agent(sys, user, "relay-decompose", 0.2, role="decomposer")
    parts = store.extract_json(raw)
    if not isinstance(parts, list) or not parts:
        # Fallback: single part = whole ТЗ.
        return [{"name": "A", "spec": tz, "inputs": "", "outputs": "", "accept": ""}]
    out = []
    for i, p in enumerate(parts[:n]):
        if isinstance(p, dict) and p.get("spec"):
            p.setdefault("name", chr(65 + i))
            out.append(p)
    return out or [{"name": "A", "spec": tz, "inputs": "", "outputs": "", "accept": ""}]


# ─── Phase 2 — Coders (round-robin; efficiency by rotation POSITION) ───
# Owner's model (msg 7, 10 + picture): each coder walks its parts in a
# rotated order; the part at position p gets efficiency level eff_levels[p]
# (0→100%, 1→80%, 2→60%, …). The levels come FROM THE TEST (calibration).
# So for N=coders=5 every part gets exactly one 100% (gold) segment + four
# non-gold (80/60/40/20) segments → 5 gold + 20 non-gold = 25 segments.

async def _coder_one(part: dict, tz: str, coder_id: int, eff: int) -> dict:
    """One coder's single version of one part, tagged with its position
    efficiency (from calibration)."""
    sys = (
        "Ты — Кодер (свежий экземпляр, без истории). Напиши ТОЛЬКО код для своей "
        "части по контракту. Кратко, рабоче, без пояснений вне кода."
    )
    user = (
        f"Общее ТЗ (контекст): {tz[:1500]}\n\n"
        f"ТВОЯ ЧАСТЬ «{part.get('name')}»:\n{part.get('spec')}\n"
        f"Вход: {part.get('inputs','')}\nВыход: {part.get('outputs','')}\n"
        f"Критерии: {part.get('accept','')}\n\nКодер #{coder_id}."
    )
    try:
        code = await _agent(sys, user, "relay-coder", 0.4 + 0.1 * coder_id, role="coder")
    except Exception as e:  # noqa: BLE001
        logger.warning("coder %s failed: %s", coder_id, e)
        code = ""
    return {"coder": coder_id, "eff": eff, "code": code}


async def _generate_all(parts: list[dict], tz: str, coders: int, eff_levels: list[int]) -> dict:
    """Round-robin generation with position-based efficiency labels.

    Coder0: A(100),B(80),C(60)…; Coder1: B(100),C(80)…,A(20); … — msg 7.
    Returns ``{name: [{"eff","code","coder"}, …]}`` (segments per part).
    """
    n = len(parts)
    tasks, meta = [], []
    for c in range(coders):
        order = [parts[(c + i) % n] for i in range(n)]  # rotated start per coder
        for pos, part in enumerate(order):
            eff = eff_levels[pos] if pos < len(eff_levels) else eff_levels[-1]
            tasks.append(_coder_one(part, tz, c, eff))
            meta.append(part.get("name"))
    segs = await asyncio.gather(*tasks)
    pooled: dict[str, list] = {p.get("name"): [] for p in parts}
    for name, seg in zip(meta, segs):
        if seg.get("code", "").strip():
            pooled[name].append(seg)
    return pooled


# ─── Phase 3 — Comparator (pick gold by efficiency, from the test) ─────

async def _pick_gold(part: dict, segments: list[dict]) -> tuple[str, list[str]]:
    """Gold = the highest-efficiency segment (the peak-position one, per the
    calibration). A fresh Comparator confirms it's coherent; only if the top is
    clearly broken it drops to the next-highest efficiency. Returns
    ``(gold_code, non_gold_codes)``."""
    segs = sorted(
        [s for s in segments if s.get("code", "").strip()],
        key=lambda s: -int(s.get("eff", 0)),
    )
    if not segs:
        return "", []
    if len(segs) == 1:
        return segs[0]["code"], []
    listing = "\n\n".join(
        f"=== ВЕРСИЯ idx={i} eff={s['eff']}% ===\n{s['code'][:2000]}"
        for i, s in enumerate(segs)
    )
    sys = (
        "Ты — Компаратор (свежий экземпляр). Версии части УЖЕ ранжированы по "
        "эффективности из калибровки (eff больше = ближе к пику = лучше). Обычно "
        "золото = версия с максимальным eff; смени выбор ТОЛЬКО если у топовой "
        'версии явная поломка. Верни СТРОГО JSON: {\"gold\": <idx>}.'
    )
    user = f"ЧАСТЬ «{part.get('name')}»:\n{part.get('spec')}\n\n{listing}"
    try:
        v = store.extract_json(await _agent(sys, user, "relay-compare", 0.0, role="comparator")) or {}
        idx = int(v.get("gold", 0))
    except Exception:  # noqa: BLE001
        idx = 0
    idx = idx if 0 <= idx < len(segs) else 0
    gold = segs[idx]["code"]
    others = [s["code"] for i, s in enumerate(segs) if i != idx]
    return gold, others


async def _compare_lane(subset: list[dict], pooled: dict) -> dict:
    """One comparator processes its lane of parts, picking gold by efficiency
    and returning the non-gold segments as the patch pool."""
    out: dict[str, tuple[str, list[str]]] = {}
    for p in subset:
        nm = p.get("name")
        gold, others = await _pick_gold(p, pooled.get(nm, []))
        out[nm] = (gold, others)
    return out


async def _compare_all(ordered: list[dict], pooled: dict) -> dict:
    """2 Comparators run CONCURRENTLY, each starting from one of the 2 most
    important parts (``ordered`` is priority-sorted → lane A starts at the
    #1 part, lane B at the #2), then sweep the rest — msg 10.
    """
    laneA = ordered[0::2]
    laneB = ordered[1::2]
    ra, rb = await asyncio.gather(_compare_lane(laneA, pooled), _compare_lane(laneB, pooled))
    return {**ra, **rb}


# ─── Phase 4 — Refiner + Supervisor (patch gold, never rewrite) ────────
# Per the owner (msg 9): the Supervisor is needed ONLY here — to guard the
# refinement of the golden code. No supervisor at coders/comparators.

async def _refine(part: dict, gold: str, others: list[str]) -> str:
    if not others:
        return gold
    alt = "\n\n".join(f"--- альт {i} ---\n{o[:1500]}" for i, o in enumerate(others))
    sys = (
        "Ты — Рефайнер. ЗОЛОТО — основа, НЕ переписывай его. Если в альтернативах "
        "есть что-то объективно лучше — внеси точечные улучшения в золото. "
        "Верни ТОЛЬКО итоговый код части."
    )
    user = f"ЧАСТЬ «{part.get('name')}».\n\nЗОЛОТО:\n{gold[:3000]}\n\nАЛЬТЕРНАТИВЫ:\n{alt}"
    try:
        patched = await _agent(sys, user, "relay-refine", 0.2, role="refiner")
    except Exception:  # noqa: BLE001
        return gold
    if not patched.strip():
        return gold
    # Supervisor: does the patched version keep the gold's strengths and only
    # improve? If it regressed or drifted, keep the original gold.
    sup_sys = (
        "Ты — Супервайзер рефайна. Тебе дают ЗОЛОТО и ПАТЧ. Патч допустим, только "
        "если он не сломал золото и реально его улучшил (иначе — откат к золоту). "
        'Верни СТРОГО JSON: {\"accept\": true|false, \"why\": \"кратко\"}.'
    )
    try:
        verdict = await _agent(
            sup_sys,
            f"ЗОЛОТО:\n{gold[:3000]}\n\nПАТЧ:\n{patched[:3000]}",
            "relay-refine-sup", 0.0, role="supervisor",
        )
        v = store.extract_json(verdict) or {}
        return patched if bool(v.get("accept", True)) else gold
    except Exception:  # noqa: BLE001
        return patched


# ─── Phase 5 — Assembler (+ priorities) ────────────────────────────────

async def _assemble(tz: str, parts: list[dict], golds: dict[str, str]) -> dict:
    joined = "\n\n".join(
        f"### Часть {p.get('name')} (вход:{p.get('inputs','')} выход:{p.get('outputs','')})\n"
        f"{golds.get(p.get('name',''), '')[:2500]}"
        for p in parts
    )
    sys = (
        "Ты — Ассемблер. Собери финальный код из золотых частей по контрактам, "
        "проверь стыки интерфейсов. Затем выдели слабые места. Верни СТРОГО JSON: "
        '{\"code\": \"<весь финальный код>\", \"priorities\": [\"имя части\", ...], '
        '\"weak\": \"кратко о слабых местах\"}.'
    )
    raw = await _agent(sys, f"ТЗ:\n{tz[:1500]}\n\nЗОЛОТЫЕ ЧАСТИ:\n{joined}", "relay-assemble", 0.2, role="assembler")
    v = store.extract_json(raw)
    if not (isinstance(v, dict) and v.get("code")):
        # Fallback: concatenate golds.
        return {"code": joined, "priorities": [], "weak": ""}
    code = str(v.get("code", ""))
    prio = v.get("priorities")
    weak = str(v.get("weak", ""))[:500]

    # Supervisor: validate the seams between assembled parts. If it finds a
    # concrete break, ask the Assembler for ONE targeted fix pass.
    sup_sys = (
        "Ты — Супервайзер сборки. Проверь стыки интерфейсов между частями по "
        "контрактам. Верни СТРОГО JSON: "
        '{\"ok\": true|false, \"issue\": \"конкретная проблема стыка или пусто\"}.'
    )
    try:
        verdict = await _agent(sup_sys, f"КОНТРАКТЫ+КОД:\n{code[:6000]}", "relay-assemble-sup", 0.0, role="supervisor")
        sv = store.extract_json(verdict) or {}
        if not bool(sv.get("ok", True)) and sv.get("issue"):
            fixed = await _agent(
                "Ты — Ассемблер. Исправь ТОЛЬКО указанную проблему стыка, не ломая "
                "остальное. Верни ТОЛЬКО итоговый код.",
                f"ПРОБЛЕМА: {sv.get('issue')}\n\nКОД:\n{code[:6000]}",
                "relay-assemble-fix", 0.2, role="assembler",
            )
            if fixed.strip():
                code = fixed
    except Exception:  # noqa: BLE001
        pass

    return {
        "code": code,
        "priorities": [str(x) for x in prio] if isinstance(prio, list) else [],
        "weak": weak,
    }


# ─── Orchestrator ──────────────────────────────────────────────────────

async def run_relay(tz: str, *, progress: Progress | None = None) -> dict:
    say = progress or _noop
    cal = store.get_calibration()
    if not cal:
        await say("⚠️ Модель не откалибрована — использую значения по умолчанию (пик 20%).")
        layout = store.derive_layout(0.2)
    else:
        layout = cal.get("layout") or store.derive_layout(cal.get("peak_ratio", 0.2))

    coders = int(layout.get("coders", 5))
    await say(
        f"🧭 <b>TEMPORAL RELAY</b>\nПик≈{int(layout['peak_ratio']*100)}% → "
        f"частей: {layout['parts']}, кодеров/часть: {coders}."
    )

    # Phase 1
    await say("① Декомпозитор режет ТЗ…")
    parts = await _decompose(tz, layout)
    names = [p.get("name") for p in parts]
    await say(f"→ частей: {len(parts)} ({', '.join(map(str, names))})")

    # Priority order (Assembler's list from last run first, then the rest).
    prio = store.get_priorities()
    ordered = sorted(parts, key=lambda p: prio.index(p["name"]) if p.get("name") in prio else 999)

    # Phase 2 — round-robin generation; efficiency by rotation position.
    eff_levels = layout.get("eff_levels") or [100, 80, 60, 40, 20]
    await say(f"② Кодеры ({coders} шт., по кругу) пишут части (позиция→эффективность)…")
    pooled = await _generate_all(ordered, tz, coders, eff_levels)
    total_seg = sum(len(pooled[p["name"]]) for p in ordered)
    gold_ct = sum(1 for p in ordered if any(int(s.get("eff", 0)) >= max(eff_levels) for s in pooled[p["name"]]))
    await say(f"→ сегментов собрано: {total_seg} (золото≈{gold_ct}, остальные — на патчи)")

    # Phase 3 — 2 Comparators in parallel, starting from the 2 most-important
    # parts, choosing the golden version per part.
    await say("③ 2 компаратора выбирают золото (старт с 2 важнейших)…")
    picks = await _compare_all(ordered, pooled)  # {name: (gold, patch_pool)}

    # Phase 4 — Refiner + Supervisor patches gold per part.
    golds: dict[str, str] = {}
    for p in ordered:
        nm = p.get("name")
        gold, others = picks.get(nm, ("", []))
        if not gold:
            golds[nm] = ""
            continue
        await say(f"④ Рефайнер+супервайзер патчит золото «{nm}» (из {len(others)} не-золотых)…")
        golds[nm] = await _refine(p, gold, others)

    # Phase 5
    await say("⑤ Ассемблер собирает финал и считает приоритеты…")
    result = await _assemble(tz, parts, golds)
    if result.get("priorities"):
        store.save_priorities(result["priorities"])

    # Optional: loop-review the assembled code (review→fix→repeat) if the
    # «🔁 Ревью→фикс→проверка→повтор» auto-toggle is ON.
    try:
        from ..loop_review import store as _lr_store
        if _lr_store.get_enabled() and result.get("code"):
            from ..loop_review.engine import run_loop
            await say("🔁 Авто-ревью собранного кода…")
            lr = await run_loop(result["code"], progress=say)
            if lr.get("code"):
                result["code"] = lr["code"]
                await say(f"🔁 Ревью-петля: итог {lr.get('score', 0):.1f}/10 за {lr.get('passes', 0)} проход(ов)")
    except Exception:  # noqa: BLE001
        logger.exception("loop-review after relay failed")

    # C6 — single loop with graphify: push the Assembler's weak spots onto the
    # project map so they get highlighted there.
    try:
        from ..projectmap import store as _pm
        weak = [w for w in [result.get("weak", "")] if w] + list(result.get("priorities", []))
        if weak:
            _pm.set_weak_spots(weak)
    except Exception:  # noqa: BLE001
        pass

    # C7 — save all segments + chosen gold to /data and zip them for review.
    artifacts_zip = _save_artifacts(tz, pooled, golds, result)

    store.save_last_result({
        "parts": names,
        "priorities": result.get("priorities", []),
        "weak": result.get("weak", ""),
        "code_len": len(result.get("code", "")),
        "artifacts_zip": artifacts_zip,
    })
    await say("✅ Готово.")
    return {"layout": layout, "parts": names, "artifacts_zip": artifacts_zip, **result}


def _save_artifacts(tz: str, pooled: dict, golds: dict, result: dict) -> str:
    """Write every segment + chosen gold + final code to /data/relay/<ts>/ and
    zip it. Returns the zip path (or '' on failure)."""
    import io
    import time
    import zipfile
    try:
        from ...config import DATA_DIR
        base = f"{DATA_DIR}/relay"
        import os
        os.makedirs(base, exist_ok=True)
        run_id = str(int(time.time()))
        zip_path = f"{base}/relay_{run_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("TZ.txt", tz)
            zf.writestr("final_code.txt", result.get("code", ""))
            for name, segs in pooled.items():
                for s in segs:
                    zf.writestr(
                        f"segments/{name}/coder{s.get('coder')}_eff{s.get('eff')}.txt",
                        s.get("code", ""),
                    )
                zf.writestr(f"gold/{name}.txt", golds.get(name, ""))
            zf.writestr("report.txt",
                        "priorities: " + ", ".join(result.get("priorities", [])) +
                        "\nweak: " + result.get("weak", ""))
        return zip_path
    except Exception:  # noqa: BLE001
        logger.exception("relay artifacts save failed")
        return ""
