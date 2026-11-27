"""LLM route planner: free-text description -> :class:`Route` (ТЗ §5, §16.2).

Uses the existing :func:`bot.addons._llm.chat_once` — no own client (§5).
Selector priority (§16.2) is enforced through the system prompt:
1) aria-label  2) placeholder  3) role+text  4) type attributes;
auto-generated classes and positional selectors are forbidden.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from .._llm import chat_once
from .models import (
    ALLOWED_OPS,
    CODE_REGEX,
    EMAIL_REGEX,
    Route,
    validate_route,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Ты — планировщик браузерных маршрутов между двумя сайтами (вкладки A и B).
Пользователь описывает действия словами. Ты возвращаешь СТРОГО один JSON-объект маршрута без markdown, без комментариев, без текста вокруг.

Схема JSON:
{
  "title": "короткое название маршрута",
  "site_a": "стартовый URL вкладки A (или пустая строка, если первый шаг — goto на A)",
  "site_b": "стартовый URL вкладки B или null (если B открывается goto-шагом)",
  "stages": [
    {"index": 1, "title": "название стадии по-русски", "steps": [
      {"op": "...", "page": "A"|"B", "target": "...", "value": "...", "save_as": "...", "extract": "...", "poll": true|false, "note": "человекочитаемое описание шага по-русски"}
    ]}
  ]
}

ДОПУСТИМЫЕ op (ровно эти, никаких других): __OPS__.
Операций wait и screenshot НЕ СУЩЕСТВУЕТ — не выдумывай их.

Правила:
1. "перейти по ссылке" -> op="goto", target=URL, page="A"|"B". Навигацию исполняет рантайм, но op в JSON так и называется "goto".
2. "нажать кнопку X" -> op="click". target-приоритет селекторов (СТРОГО в этом порядке):
   (1) aria-label: input[aria-label="..."];
   (2) placeholder: input[placeholder="..."];
   (3) роль+текст: text=X или button:has-text("X") — используй ТОЧНЫЙ текст кнопки как написал пользователь (регистр и язык важны);
   (4) типовые атрибуты: button[type="submit"], input[type="email"], input[autocomplete="one-time-code"] (стандарт для полей OTP-кода).
   ЗАПРЕЩЕНО: селекторы по автогенерируемым классам и по позиции (div > div:nth-child(3)).
3. "ввести текст в поле Y" -> op="type", target=селектор поля (тот же приоритет), value=текст.
4. "скопировать почту" -> op="copy", save_as="email", extract="__EMAIL_REGEX__" (регэксп в JSON экранируй).
   "скопировать код" -> op="copy", save_as="code", extract="__CODE_REGEX__"; если пользователь назвал число цифр N — используй \\b\\d{N}\\b.
   Всегда указывай save_as И extract, и по возможности точный target-селектор — не полагайся на body.
5. "вставить почту/код" -> op="type", value="{email}" или "{code}" (фигурные скобки — подстановка переменной).
6. "подождать N секунд" -> op="pause", value=N (ЧИСЛО СЕКУНД, не миллисекунд).
7. "дождаться код / письма" -> шаг copy с "poll": true — рантайм будет опрашивать до появления свежего значения.
8. "ввести логин/пароль из профиля" -> op="type_login"/"type_password", target=селектор поля.
9. "загрузить файл" -> op="upload", value=имя файла.
10. Каждому шагу проставь page ("A" — первый сайт, "B" — второй) и note — короткое русское описание («нажать кнопку "Sign Up"»).
11. Стадии — крупные смысловые блоки в порядке исполнения, index с 1. Названия стадий по-русски.
12. Ссылки, которые пользователь не дал (например "моя почта по ссылке ..."), оставляй как плейсхолдер вида {mail_url}.""" \
    .replace("__OPS__", ", ".join(ALLOWED_OPS)) \
    .replace("__EMAIL_REGEX__", EMAIL_REGEX.replace("\\", "\\\\")) \
    .replace("__CODE_REGEX__", CODE_REGEX.replace("\\", "\\\\"))

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(raw: str) -> dict:
    """Pull the first JSON object out of an LLM reply (tolerates fences)."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw, flags=re.S).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_RE.search(raw)
        if not m:
            raise
        return json.loads(m.group(0))


def _route_from_llm_dict(d: dict) -> Route:
    d = dict(d)
    d.setdefault("id", uuid.uuid4().hex[:10])
    # If the model put the first goto for A into steps and left site_a empty,
    # promote it (run_route always opens site_a first).
    route = Route.from_dict(d)
    if not route.site_a:
        for stage in route.stages:
            for step in stage.steps:
                if step.op == "goto" and step.page == "A" and step.target:
                    route.site_a = step.target
                    break
            if route.site_a:
                break
    if not route.site_b:
        for stage in route.stages:
            for step in stage.steps:
                if step.op == "goto" and step.page == "B" and step.target:
                    # keep the goto step (run_route handles it), just note B's URL
                    route.site_b = None  # opened by the goto step itself
                    break
    return route


async def plan_route(description: str) -> tuple[Route | None, list[str], str]:
    """Turn a free-text description into a validated Route.

    Returns ``(route, problems, raw_reply)``:
    - route is None when JSON never parsed (after one retry, §5.2);
    - problems is the validator output (§5) — may be non-empty even with
      a parsed route, caller shows them for confirmation/manual fix.
    """
    raw = await chat_once(_SYSTEM_PROMPT, description,
                          max_tokens=1800, temperature=0.2)
    last_raw = raw or ""
    for attempt in (1, 2):
        try:
            data = _extract_json(last_raw)
            route = _route_from_llm_dict(data)
            problems = validate_route(route)
            return route, problems, last_raw
        except Exception as e:  # noqa: BLE001
            logger.warning("planner: JSON parse failed (attempt %d): %s", attempt, e)
            if attempt == 1:
                # §5.2 — один повтор с сообщением об ошибке.
                retry_user = (
                    f"Твой предыдущий ответ не распарсился как JSON: {e}\n"
                    f"Ответ был:\n{last_raw[:2000]}\n\n"
                    "Верни ИСПРАВЛЕННЫЙ строгий JSON по той же схеме. "
                    f"Исходное описание маршрута:\n{description}"
                )
                last_raw = await chat_once(_SYSTEM_PROMPT, retry_user,
                                           max_tokens=1800, temperature=0.1) or ""
    # оба раза невалидно — вернуть сырой план на ручную правку (§5.2)
    return None, ["Модель не вернула валидный JSON (после повтора)."], last_raw
