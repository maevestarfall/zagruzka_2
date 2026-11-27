"""Structured, step-wise instruction tree («📚 Поступенчатая инструкция»).

The owner builds a *tree* of actions that drills down from the general
to the specific, e.g.:

    Общее → Контент → Фото и видео → Видео → Красивая анимация для сайта

A node (usually a leaf) holds a **plan**: an ordered list of steps, and
each step carries its *tools*, *prompt* and *timing*. When the
«сначала смотри инструкцию» toggle is ON, the bot consults this tree
before acting, finds the best matching path, and follows the steps.

Hermes works the steps in the background: it reads execution outcomes
(via :mod:`bot.addons.hermes_experience`) and refines the steps —
keeping what worked, improving what failed.

All state rides in ``state.json`` under the ``playbook`` addon namespace.
Pure data + logic here (no aiogram), so it is unit-testable.
"""

from __future__ import annotations

import time

ADDON = "playbook"


# ── low-level state ────────────────────────────────────────────────────

def _st():
    from .. import state as addon_state

    return addon_state


def _nodes() -> dict[str, dict]:
    v = _st().get(ADDON, "nodes", None)
    return dict(v) if isinstance(v, dict) else {}


def _save_nodes(nodes: dict[str, dict]) -> None:
    _st().set_(ADDON, "nodes", nodes)


def _steps_all() -> dict[str, list]:
    v = _st().get(ADDON, "steps", None)
    return dict(v) if isinstance(v, dict) else {}


def _save_steps(steps: dict[str, list]) -> None:
    _st().set_(ADDON, "steps", steps)


def _next_id(prefix: str) -> str:
    n = int(_st().get(ADDON, "seq", 0) or 0) + 1
    _st().set_(ADDON, "seq", n)
    return f"{prefix}{n}"


# ── nodes CRUD ─────────────────────────────────────────────────────────

def get_node(node_id: str) -> dict | None:
    return _nodes().get(node_id)


def list_children(parent_id: str | None) -> list[dict]:
    nodes = _nodes()
    kids = [n for n in nodes.values() if n.get("parent") == parent_id]
    kids.sort(key=lambda n: (n.get("order", 0), n.get("title", "")))
    return kids


def add_node(parent_id: str | None, title: str) -> dict:
    nodes = _nodes()
    nid = _next_id("n")
    order = len(list_children(parent_id))
    node = {"id": nid, "parent": parent_id, "title": (title or "Без названия").strip()[:80], "order": order}
    nodes[nid] = node
    _save_nodes(nodes)
    return node


def rename_node(node_id: str, title: str) -> bool:
    nodes = _nodes()
    if node_id not in nodes:
        return False
    nodes[node_id]["title"] = (title or "").strip()[:80] or nodes[node_id]["title"]
    _save_nodes(nodes)
    return True


def delete_node(node_id: str) -> int:
    """Delete a node and all descendants + their steps. Returns count removed."""
    nodes = _nodes()
    steps = _steps_all()
    to_del: list[str] = []

    def _collect(nid: str) -> None:
        to_del.append(nid)
        for ch in [n["id"] for n in nodes.values() if n.get("parent") == nid]:
            _collect(ch)

    if node_id not in nodes:
        return 0
    _collect(node_id)
    for nid in to_del:
        nodes.pop(nid, None)
        steps.pop(nid, None)
    _save_nodes(nodes)
    _save_steps(steps)
    return len(to_del)


def path_of(node_id: str) -> list[dict]:
    nodes = _nodes()
    chain: list[dict] = []
    cur = nodes.get(node_id)
    seen = set()
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur)
        cur = nodes.get(cur.get("parent")) if cur.get("parent") else None
    chain.reverse()
    return chain


def path_titles(node_id: str) -> str:
    return " → ".join(n["title"] for n in path_of(node_id))


def is_leaf(node_id: str) -> bool:
    return not list_children(node_id)


# ── steps CRUD ─────────────────────────────────────────────────────────

def get_steps(node_id: str) -> list[dict]:
    return list(_steps_all().get(node_id, []) or [])


def add_step(node_id: str, *, name: str, tools: list[str] | None = None,
             prompt: str = "", timing: str = "") -> dict:
    steps = _steps_all()
    lst = list(steps.get(node_id, []) or [])
    step = {
        "id": _next_id("s"),
        "name": (name or "Шаг").strip()[:120],
        "tools": [t.strip() for t in (tools or []) if t.strip()],
        "prompt": (prompt or "").strip()[:2000],
        "timing": (timing or "").strip()[:120],
        "notes": "",
        "ok": 0,
        "fail": 0,
    }
    lst.append(step)
    steps[node_id] = lst
    _save_steps(steps)
    return step


def delete_step(node_id: str, step_id: str) -> bool:
    steps = _steps_all()
    lst = list(steps.get(node_id, []) or [])
    new = [s for s in lst if s.get("id") != step_id]
    if len(new) == len(lst):
        return False
    steps[node_id] = new
    _save_steps(steps)
    return True


def update_step(node_id: str, step_id: str, **fields) -> bool:
    steps = _steps_all()
    lst = list(steps.get(node_id, []) or [])
    hit = False
    for s in lst:
        if s.get("id") == step_id:
            for k, v in fields.items():
                s[k] = v
            hit = True
    if hit:
        steps[node_id] = lst
        _save_steps(steps)
    return hit


def record_step_outcome(node_id: str, step_id: str, ok: bool, note: str = "") -> None:
    steps = _steps_all()
    lst = list(steps.get(node_id, []) or [])
    for s in lst:
        if s.get("id") == step_id:
            s["ok" if ok else "fail"] = int(s.get("ok" if ok else "fail", 0)) + 1
            if note:
                s["notes"] = (s.get("notes", "") + f"\n· {note}").strip()[:1000]
    steps[node_id] = lst
    _save_steps(steps)
    _log_exec(node_id, step_id, "ok" if ok else "fail", note)


# ── execution log (what Hermes learns from) ────────────────────────────

def _log_exec(node_id: str, step_id: str, outcome: str, note: str) -> None:
    log = list(_st().get(ADDON, "exec_log", []) or [])
    log.append({
        "ts": int(time.time()),
        "node_id": node_id,
        "path": path_titles(node_id),
        "step_id": step_id,
        "outcome": outcome,
        "note": (note or "")[:300],
    })
    if len(log) > 500:
        log = log[-500:]
    _st().set_(ADDON, "exec_log", log)


def exec_log() -> list[dict]:
    return list(_st().get(ADDON, "exec_log", []) or [])


# ── toggles ────────────────────────────────────────────────────────────

def instruction_first() -> bool:
    return bool(_st().get(ADDON, "instruction_first", False))


def set_instruction_first(on: bool) -> None:
    _st().set_(ADDON, "instruction_first", bool(on))


def hermes_auto() -> bool:
    return bool(_st().get(ADDON, "hermes_auto", False))


def set_hermes_auto(on: bool) -> None:
    _st().set_(ADDON, "hermes_auto", bool(on))


def hermes_interval_min() -> int:
    try:
        return max(5, int(_st().get(ADDON, "hermes_interval_min", 30) or 30))
    except (TypeError, ValueError):
        return 30


# ── matching: find the best node for a free-text request ───────────────

def _tokens(s: str) -> set[str]:
    import re
    return {t for t in re.split(r"[^\wа-яё]+", (s or "").lower()) if len(t) > 2}


def find_best_node(query: str) -> str | None:
    """Return the node whose path best matches the query (by token overlap).

    Prefers deeper (more specific) nodes on ties.
    """
    q = _tokens(query)
    if not q:
        return None
    best_id, best_score, best_depth = None, 0.0, -1
    for nid in _nodes():
        path = path_of(nid)
        ptoks = set()
        for n in path:
            ptoks |= _tokens(n["title"])
        if not ptoks:
            continue
        score = len(q & ptoks)
        depth = len(path)
        if score > best_score or (score == best_score and score > 0 and depth > best_depth):
            best_id, best_score, best_depth = nid, score, depth
    return best_id if best_score > 0 else None


def resolve_plan(query: str) -> dict | None:
    """Find the best node and return its path + steps for execution/preview."""
    nid = find_best_node(query)
    if not nid:
        return None
    return {
        "node_id": nid,
        "path": path_titles(nid),
        "steps": get_steps(nid),
    }


def render_plan(plan: dict | None, *, header: str = "") -> str:
    """Render a resolved plan (from :func:`resolve_plan`) as a concrete,
    ordered text block — path + per-step tools / prompt / timing.

    Returns an empty string when there is nothing to render.
    """
    if not plan or not plan.get("steps"):
        return ""
    lines = [header or "## НАЙДЕННЫЙ ПЛАН (выполняй по шагам)"]
    lines.append(f"Путь: {plan.get('path', '')}")
    for i, s in enumerate(plan["steps"], 1):
        tools = ", ".join(s.get("tools", []) or []) or "—"
        line = (
            f"\nШаг {i}. {s.get('name')}\n"
            f"  Инструменты: {tools}\n"
            f"  Что делать: {s.get('prompt') or '—'}\n"
            f"  Тайминг: {s.get('timing') or '—'}"
        )
        notes = (s.get("notes") or "").strip()
        if notes:
            line += f"\n  Доработки Гермеса: {notes[:200]}"
        lines.append(line)
    return "\n".join(lines)


def plan_directive(query: str) -> str:
    """Per-turn system-prompt block.

    When «сначала смотри инструкцию» is ON *and* the free-text request
    matches a node that actually has steps, return that node's concrete
    plan so the agent follows the real steps — not just the tree outline
    from :func:`directive`. Empty otherwise.
    """
    if not instruction_first():
        return ""
    plan = resolve_plan(query)
    if not plan or not plan.get("steps"):
        return ""
    return render_plan(
        plan,
        header=(
            "## ПОДХОДЯЩИЙ ПЛАН ДЛЯ ЭТОГО ЗАПРОСА (сначала смотри сюда)\n"
            "Под запрос найден готовый план. Выполни его шаги ПО ПОРЯДКУ, "
            "используя указанные инструменты, промты и тайминги. По завершении "
            "каждого шага коротко отметь, что сработало, а что нет."
        ),
    )


# ── agent directive (injected into the system prompt) ──────────────────

def _outline(parent: str | None = None, depth: int = 0, acc: list | None = None) -> list[str]:
    acc = acc if acc is not None else []
    for n in list_children(parent):
        n_steps = len(get_steps(n["id"]))
        suffix = f" — {n_steps} шаг(ов)" if n_steps else ""
        acc.append("  " * depth + f"• {n['title']}{suffix}")
        _outline(n["id"], depth + 1, acc)
    return acc


def directive() -> str:
    """System-prompt block. Empty unless «сначала смотри инструкцию» is ON."""
    if not instruction_first():
        return ""
    outline = _outline()
    if not outline:
        return ""
    body = "\n".join(outline[:80])
    return (
        "## ПОСТУПЕНЧАТАЯ ИНСТРУКЦИЯ (сначала смотри сюда)\n"
        "Прежде чем действовать: найди в дереве ниже путь от общего к "
        "частному, подходящий под запрос, спустись до нужного листа и "
        "выполняй его шаги ПО ПОРЯДКУ — используя указанные инструменты, "
        "промты и тайминги каждого шага. Если подходящего листа нет — "
        "действуй обычно и предложи добавить новый шаг.\n\n"
        "Дерево действий:\n" + body
    )


# ── default seed (the owner's example) ─────────────────────────────────

def seed_default(force: bool = False) -> bool:
    """Seed an example tree the first time (idempotent). Returns True if seeded."""
    if _nodes() and not force:
        return False
    obshchee = add_node(None, "Общее")
    kontent = add_node(obshchee["id"], "Контент")
    foto_video = add_node(kontent["id"], "Фото и видео")
    add_node(foto_video["id"], "Фото")
    video = add_node(foto_video["id"], "Видео")
    anim = add_node(video["id"], "Красивая анимация для сайта")

    # A fully worked example plan on the deepest leaf.
    add_step(
        anim["id"],
        name="1. Бриф и референс",
        tools=["chat", "firecrawl"],
        prompt="Сформулируй цель анимации (что показываем, настроение, "
               "длительность 4–8с). Собери 2–3 референса со стилем.",
        timing="сразу, ~1 мин",
    )
    add_step(
        anim["id"],
        name="2. Ключевой кадр (фото)",
        tools=["media:photo"],
        prompt="Сгенерируй опорный кадр: <субъект>, кинематографичный свет, "
               "16:9, чисто, без артефактов. seed зафиксируй.",
        timing="после брифа, пауза 5–10с между попытками",
    )
    add_step(
        anim["id"],
        name="3. Оживление (видео из кадра)",
        tools=["media:video", "img2video"],
        prompt="Оживи ключевой кадр: плавное движение камеры, лёгкий параллакс, "
               "петля для сайта. 4–6 секунд, 24fps.",
        timing="после кадра, не запускай параллельно",
    )
    add_step(
        anim["id"],
        name="4. Финиш и выгрузка",
        tools=["media:tools"],
        prompt="Подрежь, зациклить, сожми под веб (webm/mp4). Проверь вес и "
               "плавность петли.",
        timing="в конце",
    )
    return True
