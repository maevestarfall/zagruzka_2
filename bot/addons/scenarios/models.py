"""Data model for two-site routes (ТЗ rev.7.1 §3, §15.2, §16.3).

``Step`` is the *addon-level* model (convenient for the planner / export).
Before execution each step is converted to the engine tuple ``(op, val)``
by :func:`bot.addons.scenarios.runner._to_engine_step` (§0.3, §7).

Vars mechanism: a ``copy`` step with ``save_as="email"`` stores the value
into ``route.vars["email"]``; a ``type`` step with ``value="{email}"``
substitutes it. That is exactly the "took mail on A -> pasted on B" flow.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

# Regexes recommended by §15.2 for OTP-style extraction.
EMAIL_REGEX = r"[\w.+-]+@[\w-]+\.[\w.-]+"
CODE_REGEX = r"\b\d{4,8}\b"
CODE6_REGEX = r"\b\d{6}\b"

# The engine's "not found" sentinel (§0.1 / Приложение A #7). Never store it.
COPY_NOT_FOUND = "Элемент не найден или пуст"

ALLOWED_OPS = (
    "click", "type", "type_login", "type_password",
    "upload", "pause", "copy", "goto",
)

_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass
class Step:
    op: str                          # click | type | type_login | type_password
                                     #       | upload | pause | copy | goto
                                     # ВАЖНО: goto исполняет сам run_route
                                     # (page.goto), а НЕ движок run_steps (§0.4)
    target: str | None = None        # URL (goto) или CSS/text-селектор
    value: str | float | None = None # type: текст ({var} поддерживается);
                                     # pause: СЕКУНДЫ (§0.2); upload: имя файла
    save_as: str | None = None       # copy: имя переменной ("email", "code")
    page: str = "A"                  # "A" | "B" — вкладка
    note: str | None = None          # человекочитаемое описание (демо/экспорт)
    extract: str | None = None       # §15.2: regex для точечного извлечения
    poll: bool = False               # §15.5: поллинг «свежего» значения (copy)
    vault_ref: str | None = None     # §11: ссылка на запись vault вместо секрета

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"op": self.op, "page": self.page}
        for k in ("target", "value", "save_as", "note", "extract", "vault_ref"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.poll:
            d["poll"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        return cls(
            op=str(d.get("op", "")),
            target=d.get("target"),
            value=d.get("value"),
            save_as=d.get("save_as"),
            # accept both "page" (§3) and "tab" (§16.3 fixture spelling)
            page=str(d.get("page") or d.get("tab") or "A").upper(),
            note=d.get("note"),
            extract=d.get("extract"),
            poll=bool(d.get("poll", False)),
            vault_ref=d.get("vault_ref"),
        )


@dataclass
class Stage:
    index: int                       # 1..N
    title: str
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Stage":
        return cls(
            index=int(d.get("index") or d.get("n") or 0),
            title=str(d.get("title", "")),
            steps=[Step.from_dict(s) for s in (d.get("steps") or [])],
        )


@dataclass
class Route:
    id: str
    title: str
    site_a: str                      # стартовая ссылка вкладки A (page.goto)
    site_b: str | None               # стартовая ссылка вкладки B (или goto-шаг)
    stages: list[Stage] = field(default_factory=list)
    vars: dict = field(default_factory=dict)   # рантайм: email, code, ...

    # ---- (de)serialisation ----

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "site_a": self.site_a,
            "site_b": self.site_b,
            "stages": [s.to_dict() for s in self.stages],
            "vars": dict(self.vars),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Route":
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:10]),
            title=str(d.get("title", "Маршрут")),
            site_a=str(d.get("site_a", "")),
            site_b=d.get("site_b"),
            stages=[Stage.from_dict(s) for s in (d.get("stages") or [])],
            vars=dict(d.get("vars") or {}),
        )

    @classmethod
    def from_json(cls, raw: str) -> "Route":
        return cls.from_dict(json.loads(raw))

    # ---- human-readable export (§10) ----

    def to_markdown(self, preamble: str | None = None) -> str:
        lines: list[str] = []
        if preamble:
            lines.append(preamble.rstrip())
            lines.append("\n---\n")
        lines.append(f"# {self.title}\n")
        lines.append(f"- Сайт А: {self.site_a}")
        if self.site_b:
            lines.append(f"- Сайт Б: {self.site_b}")
        lines.append("")
        for stage in self.stages:
            lines.append(f"## Стадия {stage.index}. {stage.title}\n")
            for i, step in enumerate(stage.steps, 1):
                note = step.note or _default_note(step)
                lines.append(f"{i}. [{step.page}] {note}")
            lines.append("")
        return "\n".join(lines)


def _default_note(step: Step) -> str:
    if step.op == "goto":
        return f"перейти по ссылке {step.target}"
    if step.op == "click":
        return f"нажать «{step.target}»"
    if step.op == "type":
        return f"ввести «{step.value}» в поле {step.target}"
    if step.op == "type_login":
        return f"ввести логин в поле {step.target}"
    if step.op == "type_password":
        return f"ввести пароль в поле {step.target}"
    if step.op == "upload":
        return f"загрузить файл {step.value}"
    if step.op == "pause":
        return f"подождать {step.value} сек."
    if step.op == "copy":
        return f"скопировать значение из {step.target}" + (
            f" → переменная {{{step.save_as}}}" if step.save_as else "")
    return f"{step.op} {step.target or ''}".strip()


# ---- Validation (§5 «валидатор перед запуском») ----

def validate_route(route: Route) -> list[str]:
    """Return a list of human-readable problems (empty list == valid).

    Checks (§5):
    - only real engine ops (§6) — no ``wait``/``screenshot``;
    - every ``{var}`` used by ``type.value`` has an earlier ``copy``
      with the same ``save_as`` name;
    - ``pause.value`` is a number (seconds, §0.2);
    - ``type`` has both ``target`` and ``value``;
    - ``copy`` with ``save_as`` has a ``target``;
    - ``goto`` has a URL ``target``;
    - page labels are "A"/"B";
    - ``extract`` regexes compile (§15.2).
    """
    problems: list[str] = []
    if not route.site_a:
        problems.append("Не задана стартовая ссылка сайта А (site_a).")
    known_vars: set[str] = set(route.vars.keys())
    for stage in route.stages:
        for k, step in enumerate(stage.steps, 1):
            where = f"Стадия {stage.index} «{stage.title}», шаг {k} ({step.op})"
            if step.op not in ALLOWED_OPS:
                problems.append(f"{where}: недопустимая операция "
                                f"(разрешены: {', '.join(ALLOWED_OPS)}).")
                continue
            if step.page not in ("A", "B"):
                problems.append(f"{where}: вкладка должна быть A или B, "
                                f"а не «{step.page}».")
            if step.op == "goto":
                if not step.target:
                    problems.append(f"{where}: у goto нет ссылки (target).")
            elif step.op == "click":
                if not step.target:
                    problems.append(f"{where}: у click нет селектора (target).")
            elif step.op == "type":
                if not step.target:
                    problems.append(f"{where}: у type нет поля (target).")
                if step.value is None or str(step.value) == "":
                    problems.append(f"{where}: у type нет текста (value).")
                else:
                    for var in _VAR_RE.findall(str(step.value)):
                        if var not in known_vars:
                            problems.append(
                                f"{where}: переменная {{{var}}} используется "
                                f"до того, как её сохранил copy save_as="
                                f"\"{var}\".")
            elif step.op in ("type_login", "type_password"):
                if not step.target:
                    problems.append(f"{where}: нет селектора поля (target).")
            elif step.op == "pause":
                try:
                    float(step.value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    problems.append(f"{where}: pause.value должно быть числом "
                                    "секунд (§0.2), не мс и не строкой.")
            elif step.op == "upload":
                if not step.value:
                    problems.append(f"{where}: у upload нет имени файла (value).")
            elif step.op == "copy":
                if not step.target:
                    problems.append(f"{where}: у copy нет селектора (target).")
                if not step.save_as:
                    problems.append(f"{where}: у copy нет имени переменной "
                                    "(save_as).")
                else:
                    known_vars.add(step.save_as)
            if step.extract:
                try:
                    re.compile(step.extract)
                except re.error as e:
                    problems.append(f"{where}: невалидный extract-regex: {e}.")
    return problems


def resolve_vars(text: str, vars_: dict) -> str:
    """Substitute ``{var}`` placeholders from ``vars_`` (§3)."""
    def _sub(m: re.Match[str]) -> str:
        return str(vars_.get(m.group(1), m.group(0)))
    return _VAR_RE.sub(_sub, text)
