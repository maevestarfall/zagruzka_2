# Coder brain for regen: generates real fixes using a whole-project map as context.
# Co-authored with CoCo
"""Real code-generation entrypoint for the «🔧 Регенерация» feature.

Mason's rule: *«код пусть пишет именно кодер»* — so the actual code is
written here, inside the ``coder`` package, by the coder's own LLM brain
(``bot.addons._llm.chat_once``, the shared lilush brain slot). The
``regen`` addon only *orchestrates* (scans the bot, gathers context,
researches) and then calls :func:`generate_fix` to get the real patch.

Output is a structured :class:`FixResult` that the regen addon turns into
the three deliverables Mason asked for:

* a plain «где и что болит» description,
* a JSON-with-code + instructions (to hand to another AI),
* a ready ZIP of the fixed bot.

Degrades gracefully: when the brain is unconfigured the caller catches
:class:`NoBrainError` and shows a friendly «настрой мозг» message.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .._llm import NoBrainError, chat_once

logger = logging.getLogger(__name__)

__all__ = ["FixResult", "generate_fix", "NoBrainError"]


@dataclass
class FixResult:
    """Structured output of the coder for a single problem."""

    problem: str  # restated «где и что болит»
    root_cause: str  # diagnosis
    instructions: str  # step-by-step fix instructions for a human/AI
    files: dict[str, str] = field(default_factory=dict)  # path -> full new content
    diff: str = ""  # optional unified-diff style explanation
    confidence: str = "medium"  # low / medium / high
    notes: str = ""  # caveats, what to verify

    def to_json(self) -> dict:
        return {
            "problem": self.problem,
            "root_cause": self.root_cause,
            "instructions": self.instructions,
            "files": self.files,
            "diff": self.diff,
            "confidence": self.confidence,
            "notes": self.notes,
        }


_SYSTEM = (
    "Ты — Кодер бота lilush (Telegram-бот на aiogram 3.x, Python 3.11). "
    "Тебе дают: КАРТУ ПРОЕКТА (все модули, их ответственность, функции и "
    "кнопки), описание проблемы, релевантный код, иногда диагностику "
    "сканера и ресёрч (как люди решают похожее). СНАЧАЛА изучи карту "
    "проекта целиком — ты должен знать что и где уже есть, чтобы НЕ "
    "сломать систему и НЕ выдумывать несуществующие API: переиспользуй "
    "готовые функции. Частый класс багов lilush — неполные мёрджи: "
    "код ссылается на функции/модули/импорты, которых нет (NameError / "
    "ModuleNotFoundError в рантайме). "
    "Если правишь файл — верни ПОЛНОЕ новое содержимое этого файла, а не "
    "фрагмент. Отвечай СТРОГО одним JSON-объектом без markdown-обёртки "
    "со схемой: {\"root_cause\": str, \"instructions\": str, \"files\": "
    "{path: full_new_content}, \"diff\": str, \"confidence\": "
    "\"low|medium|high\", \"notes\": str}. Если уверенного полного "
    "файла дать нельзя — оставь files пустым {} и дай в instructions "
    "точный рецепт правки (файл, строка, что заменить)."
)


def _extract_json(text: str) -> dict:
    """Best-effort: pull the first JSON object out of an LLM reply."""
    text = (text or "").strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.split("```", 2)
        text = text[1] if len(text) > 1 else ""
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    # Fallback: find outermost braces.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        with_braces = text[start : end + 1]
        try:
            return json.loads(with_braces)
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def generate_fix(
    problem: str,
    *,
    code_context: str = "",
    diagnosis: str = "",
    research: str = "",
    project_map: str = "",
    max_tokens: int = 3500,
) -> FixResult:
    """Ask the coder brain to produce a real fix for ``problem``.

    Raises :class:`NoBrainError` if the brain is unconfigured / errors.
    """
    parts = []
    if project_map.strip():
        parts.append(project_map.strip())
    parts.append(f"# ПРОБЛЕМА\n{problem.strip()}")
    if diagnosis.strip():
        parts.append(f"\n# ДИАГНОСТИКА СКАНЕРА\n{diagnosis.strip()}")
    if code_context.strip():
        parts.append(f"\n# РЕЛЕВАНТНЫЙ КОД\n{code_context.strip()}")
    if research.strip():
        parts.append(f"\n# КАК РЕШАЮТ ПОХОЖЕЕ (ресёрч)\n{research.strip()}")
    parts.append(
        "\nСначала сверяйся с картой проекта, потом выдай JSON по схеме "
        "из system. Будь конкретным и рабочим."
    )
    user = "\n".join(parts)

    raw = await chat_once(_SYSTEM, user, max_tokens=max_tokens, temperature=0.3)
    data = _extract_json(raw)
    if not data:
        # Brain replied but not as JSON — keep the prose as instructions
        # so the user still gets something useful.
        return FixResult(
            problem=problem.strip(),
            root_cause="(модель не вернула структурированный ответ)",
            instructions=raw.strip() or "(пустой ответ модели)",
            confidence="low",
            notes="Ответ кодера не распарсился как JSON — показан как текст.",
        )

    files = data.get("files") or {}
    if not isinstance(files, dict):
        files = {}
    # keep only str->str entries
    files = {
        str(k): v
        for k, v in files.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }
    return FixResult(
        problem=problem.strip(),
        root_cause=str(data.get("root_cause") or "").strip(),
        instructions=str(data.get("instructions") or "").strip(),
        files=files,
        diff=str(data.get("diff") or "").strip(),
        confidence=str(data.get("confidence") or "medium").strip().lower(),
        notes=str(data.get("notes") or "").strip(),
    )
