"""Autonomous API-debater driver for Viktor-style debates.

Each of the 6 API-debater slots is a *one-way* connector into a single door:
the bot uses the slot's own external LLM (OpenAI-compatible API) to generate
debate envelopes and posts them to that door. A slot can ONLY:

  * read the door's topic (ТЗ) and motivation phrase (read-only input),
  * read the current door state / transcript,
  * post debate envelopes to that one door (output).

A slot never reads or writes Brain 1 / Brain 2 / "Другое" configuration, nor
any other door. Isolation is structural: this module only ever touches the
slot's own config (``addons.debates.viktor_v3.api_debaters.<n>``), the bound
door's public state and the door TZ/motivation — never the brain debater seats.

LLM contract (OpenAI-compatible):
    POST {api_url}/chat/completions
    Authorization: Bearer {api_key}
    body: {"model": <model>, "messages": [...], "temperature": 0.4}
``api_url`` is the *base* url (e.g. ``https://api.openai.com/v1``); this module
appends ``/chat/completions``. If ``api_url`` already ends with
``/chat/completions`` it is used as-is.

The LLM is asked to return a single JSON object = one ``MsgEnvelope``. Before
posting, the envelope is validated *locally* with the same enforcement the
server uses, so invalid output is never sent (it is returned for retry).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

LLM_TIMEOUT = 60.0
POST_TIMEOUT = 20.0
MAX_TRANSCRIPT_CHARS = 8000


# ─── LLM endpoint + request ──────────────────────────────────────────────────


def chat_completions_url(api_url: str) -> str:
    """Resolve the chat/completions endpoint from a base url."""
    base = (api_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


async def call_llm(slot: dict[str, Any], messages: list[dict[str, str]],
                   *, temperature: float = 0.4,
                   _retries: int = 2) -> tuple[bool, str]:
    """Call the slot's OpenAI-compatible API. Returns (ok, content_or_error).

    Retries up to *_retries* times on transient network / timeout errors.
    """
    api_url = slot.get("api_url") or ""
    api_key = slot.get("api_key") or ""
    model = slot.get("model") or ""
    if not api_url or not model:
        return False, "slot missing api_url or model"
    url = chat_completions_url(api_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "messages": messages, "temperature": temperature}
    last_err = ""
    for attempt in range(1, _retries + 2):
        try:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as cli:
                resp = await cli.post(url, headers=headers, json=payload)
        except Exception as e:  # noqa: BLE001
            last_err = f"LLM request failed (attempt {attempt}): {type(e).__name__}: {e}"
            if attempt <= _retries:
                await asyncio.sleep(2 * attempt)  # 2s, 4s backoff
                continue
            return False, last_err
        if resp.status_code >= 500 and attempt <= _retries:
            last_err = f"LLM HTTP {resp.status_code} (attempt {attempt}): {resp.text[:200]}"
            await asyncio.sleep(2 * attempt)
            continue
        if resp.status_code != 200:
            return False, f"LLM HTTP {resp.status_code}: {resp.text[:300]}"
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            return False, f"LLM bad response shape: {e}"
        return True, content or ""
    return False, last_err


# ─── Turn Order ──────────────────────────────────────────────────────────────

DEBATER_ORDER = ("debater_a", "debater_b", "debater_c")


def whose_turn(state: dict[str, Any],
               active_debaters: list[str] | None = None) -> str | None:
    """Return the debater_id whose turn it is, based on message counts.

    Rule: the debater with the fewest total messages goes next.
    Ties are broken in canonical order: A → B → C.
    If *active_debaters* is given, only those IDs are considered.
    """
    debaters = list(active_debaters or DEBATER_ORDER)
    per = state.get("counters", {}).get("per_debater", {})
    counts = {d: per.get(d, {}).get("messages", 0) for d in debaters}
    if not counts:
        return debaters[0] if debaters else None
    min_count = min(counts.values())
    for d in DEBATER_ORDER:
        if d in counts and counts[d] == min_count:
            return d
    return debaters[0]


# ─── Envelope extraction ─────────────────────────────────────────────────────


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_envelope(content: str) -> dict[str, Any] | None:
    """Parse a single JSON envelope object out of an LLM reply.

    Tolerates ```json fences and surrounding prose; returns the first valid
    top-level JSON object, or None.
    """
    if not content:
        return None
    text = content.strip()
    # 1) fenced block
    m = _FENCE_RE.search(text)
    candidates = []
    if m:
        candidates.append(m.group(1))
    candidates.append(text)
    # 2) first {...} span (balanced)
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and obj.get("kind"):
                return obj
        except Exception:  # noqa: BLE001
            continue
    return None


# ─── Prompt building ─────────────────────────────────────────────────────────


_ENVELOPE_SCHEMA_HINT = (
    "Верни РОВНО один JSON-объект (envelope), без markdown-обёртки и без "
    "комментариев. Обязательное поле `kind` — одно из: note, atom-propose, "
    "atom-critique, atom-revision, atom-peer-improve, atom-peer-review, "
    "atom-adversarial, atom-approve, "
    "cluster-propose, cluster-critique, cluster-revision, cluster-approve, "
    "final-propose, final-critique, final-revision, final-approve. Поле "
    "`content` обязательно (1..64000 символов). Остальные поля зависят от "
    "kind согласно протоколу v3 STRICT (canonical_category_id, pick A/B, "
    "provocation_level, target_atom_id, verdict, evidence_quote, "
    "actionable_objection, outcome, failure_modes, improvement_summary и т.д.)."
)

_VOICE_MANIFEST_HINT = (
    "ВНИМАНИЕ: Текущая фаза — voice_manifest. Твоё ПЕРВОЕ сообщение ОБЯЗАНО "
    "быть voice manifest. kind='note', а поле `content` должно содержать "
    "РОВНО такой маркдаун:\n\n"
    "### Voice manifest\n"
    "**I will PUSH BACK ON:**\n"
    "1. [пункт длиной ≥40 символов]\n"
    "2. [пункт длиной ≥40 символов]\n"
    "3. [пункт длиной ≥40 символов]\n"
    "4. [пункт длиной ≥40 символов]\n"
    "5. [пункт длиной ≥40 символов]\n"
    "**I will REFUSE TO COMPROMISE ON:**\n"
    "1. [пункт длиной ≥40 символов]\n"
    "2. [пункт длиной ≥40 символов]\n"
    "3. [пункт длиной ≥40 символов]\n"
    "4. [пункт длиной ≥40 символов]\n"
    "5. [пункт длиной ≥40 символов]\n"
    "**Adversarial targets I will look for...**\n"
    "1. [пункт длиной ≥40 символов]\n"
    "2. [пункт длиной ≥40 символов]\n"
    "3. [пункт длиной ≥40 символов]\n"
    "4. [пункт длиной ≥40 символов]\n"
    "5. [пункт длиной ≥40 символов]\n"
    "**Self-declared bias warning:**\n"
    "[текст о твоём предвзятости]\n\n"
    "Заголовки ОБЯЗАТЕЛЬНО на английском, ровно как написано выше. "
    "Каждая секция ≥5 нумерованных пунктов, каждый ≥40 символов. "
    "Никаких JSON-обёрток внутри content — только чистый маркдаун. "
    "НЕ используй фразы: robust, scalable, elegant, best practices, "
    "leverage, utilize, ensure, comprehensive, holistic."
)


_PEER_IMPROVE_HINT = (
    "ВНИМАНИЕ: Текущая фаза — atoms_peer_improve. Дебатёры по очереди "
    "улучшают свои СОБСТВЕННЫЕ атомы. Каждый ход — ТЫ УЛУЧШАЕШЬ ОДИН "
    "свой атом (kind='atom-peer-improve'), ИЛИ рецензируешь чужое "
    "улучшение (kind='atom-peer-review').\n\n"
    "Для atom-peer-improve:\n"
    "  - target_atom_id: ID одного из ТВОИХ атомов\n"
    "  - content: полный улучшенный текст атома (≥200 символов)\n"
    "  - improvement_summary: что именно улучшил и почему (≥50 символов)\n\n"
    "Для atom-peer-review (рецензия улучшения оппонента):\n"
    "  - target_atom_id: ID атома ОППОНЕНТА (который он только что улучшил)\n"
    "  - verdict: AGREE / PARTIAL / DISAGREE\n"
    "  - content: рецензия (≥80 символов)\n\n"
    "Порядок: сначала улучши один свой атом, потом жди рецензию оппонента. "
    "Если оппонент только что улучшил свой атом — напиши рецензию.\n"
    "Смотри в стенограмму: если последний ход оппонента — atom-peer-improve, "
    "то ТЫ ДОЛЖЕН ответить atom-peer-review. Иначе — улучшай свой атом."
)


def build_messages(*, role_doc: str, topic: str, motivation: str,
                   phase: str, transcript: str,
                   debater_id: str, last_violations: list | None = None,
                   research_notes: str = "",
                   ) -> list[dict[str, str]]:
    """Build the OpenAI chat messages for one debate turn."""
    sys_parts = [
        "Ты — ИИ-дебатёр в строгом протоколе дебатов Viktor v3 STRICT.",
        f"Твой debater_id: {debater_id}.",
        "Ты участвуешь ТОЛЬКО в этой двери. Ты можешь только дебатировать по "
        "правилам ниже: брать тему и мотивацию и публиковать корректные "
        "envelope-сообщения. Ничего вне двери ты не трогаешь.",
        "",
        "=== ПРАВИЛА / РОЛЬ ===",
        role_doc.strip()[:6000] if role_doc else "(правила по умолчанию)",
        "",
        _ENVELOPE_SCHEMA_HINT,
    ]
    if phase == "voice_manifest":
        sys_parts += ["", _VOICE_MANIFEST_HINT]
    if phase == "atoms_peer_improve":
        sys_parts += ["", _PEER_IMPROVE_HINT]
    if motivation:
        sys_parts += ["", "=== МОТИВАЦИЯ ===", motivation.strip()[:1500]]
    if research_notes:
        sys_parts += [
            "",
            "=== РЕЗУЛЬТАТЫ ИССЛЕДОВАНИЯ ===",
            "📋 Правило: исследование между ходами\n\n"
            "сделай себе таймеров 15 штук по 15 минуты чтобы напоминать "
            "себе что идут дебаты и чтоб ты их продолжал . и допиши на "
            "каждый 15ый таймер(последний) что если дебаты не закончились "
            "то ставлю ещё 15 таймеров так же. так же в каждом таймере "
            "поставь что когда дебаты закончатся можно все таймеры "
            "удалять\n\n"
            "Пока ждёшь свою очередь — ищи ошибки в собственных "
            "позициях.\n\n"
            "Используй лучшие доступные методы из тех инструментов, "
            "что у тебя есть, и поищи в интернете. Проверяй факты, "
            "ищи контраргументы, находи слабые места в своих решениях.\n\n"
            "Когда придёт твоя очередь — вставляй исправленную версию "
            "с учётом того, что нашёл. Каждый следующий ход должен быть "
            "сильнее предыдущего.\n\n"
            "настрой триггер на Lindy Mail — когда на почту приходит "
            "письмо, ты просыпаешься или заканчиваешь свои поиски новых "
            "решений и выкладываешь в дебаты. сделай ссылку-кнопку, "
            "которую можно открыть и тыкнуть.\n\n"
            "gist.github.com на GitHub\n\n"
            "там есть фича \"create gist\" — можно закинуть один "
            "HTML-файл, гитхаб выдаёт прямую ссылку (raw URL). "
            "я сгенерил тёмную страницу с mailto-кнопкой и кинул "
            "сылку прямо в дверь дебатов.\n\n"
            "весь код одной страницей, никакого сервера не нужно\n\n"
            "ты можешь создать gist через свой GitHub-экшн — в линди "
            "есть `GitHub:create-gist`, ты тоже можешь его подключить,\n"
            "*но главное — github вообще не нужен*. можно запостить "
            "mailto-ссылку прямо как обычный текст в ноте дебата. "
            "достаточно написать в сообщении:\n"
            "```[🔥 Wake Up Lindy]"
            "(mailto:lindy-chloemeadowvale@lindymail.ai"
            "?subject=WAKE%20UP)```\n"
            "дебаты поддерживают Markdown (раз ты через API пишешь), "
            "mailto:// откроется в браузере/почтовике. никаких gist'ов, "
            "pastebin'ов, хостингов\n\n"
            "ставь триггер и генери HTML-страничку - Ссылка-будильник "
            "(план Б):\n"
            "При входе в дебат каждый дебатёр публикует свою "
            "ссылку-будильник в дверь. Если ты заснул либо в поисках — "
            "тебя разбудят по этой ссылке.\n\n"
            "будет тот кто до тебя в эстафете в дебатах. B Будет А, "
            "А будет С(либо Б если вас двое дебатируют)\n\n"
            "Ниже — результаты исследования:",
            research_notes.strip()[:1200],
        ]
    user_parts = [
        "=== ТЕМА (ТЗ) ===",
        (topic or "(тема не задана)").strip()[:6000],
        "",
        f"=== ТЕКУЩАЯ ФАЗА: {phase} ===",
        "",
        "=== СТЕНОГРАММА (последнее) ===",
        (transcript or "(пока пусто)")[-MAX_TRANSCRIPT_CHARS:],
    ]
    if last_violations:
        user_parts += [
            "",
            "=== ПРЕДЫДУЩАЯ ПОПЫТКА ОТКЛОНЕНА, исправь нарушения: ===",
            json.dumps(last_violations, ensure_ascii=False)[:2000],
        ]
    user_parts += ["", "Сформируй один корректный envelope для текущей фазы."]
    return [
        {"role": "system", "content": "\n".join(sys_parts)},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


# ─── Idle research (web-search while waiting for turn) ────────────────────────

_DDG_URL = "https://html.duckduckgo.com/html/"
_DDG_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DebateBot/1.0)"}


async def _ddg_search(query: str, max_results: int = 5) -> list[str]:
    """Quick DuckDuckGo search returning text snippets."""
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            resp = await cli.post(
                _DDG_URL, data={"q": query}, headers=_DDG_HEADERS)
            if resp.status_code != 200:
                return []
            html = resp.text
            # Extract snippets from result__snippet spans
            raw = re.findall(
                r'class="result__snippet"[^>]*>(.*?)</(?:a|td|span)',
                html, re.DOTALL)
            results: list[str] = []
            for s in raw[:max_results]:
                text = re.sub(r'<[^>]+>', '', s).strip()
                if text and len(text) > 20:
                    results.append(text)
            return results
    except Exception:  # noqa: BLE001
        return []


async def research_positions(
    base_url: str, door_id: str,
    debater_id: str, slot: dict[str, Any],
    state: dict[str, Any],
) -> str:
    """Fetch debater's own messages, search the web for counter-arguments
    and improvements, then ask the LLM to produce a short research brief.

    Returns the brief string (to store in slot["research_notes"]).
    """
    # 1. Get events — our own messages
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(f"{base_url}/debate/door/{door_id}/events",
                              params={"limit": "200"})
            events = r.json() if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        events = []

    my_msgs = [e for e in events
               if isinstance(e, dict) and e.get("debater_id") == debater_id]
    if not my_msgs:
        return ""

    # 2. Extract text claims from last messages
    claims: list[str] = []
    for msg in my_msgs[-5:]:
        body = msg.get("body")
        if isinstance(body, dict):
            for _key, val in body.items():
                if isinstance(val, str) and len(val) > 30:
                    claims.append(val[:250])
                elif isinstance(val, list):
                    for item in val[:5]:
                        if isinstance(item, str) and len(item) > 30:
                            claims.append(item[:250])
                        elif isinstance(item, dict):
                            for _k2, v2 in item.items():
                                if isinstance(v2, str) and len(v2) > 30:
                                    claims.append(v2[:250])
        elif isinstance(body, str) and len(body) > 30:
            claims.append(body[:250])

    if not claims:
        return ""

    # 3. Web search: pick up to 3 representative claims
    phase = state.get("current_phase", "")
    all_snippets: list[str] = []
    for claim in claims[:3]:
        # trim to first sentence for a better query
        q = claim.split(".")[0][:100]
        snippets = await _ddg_search(q)
        all_snippets.extend(snippets)
    # also search on the debate topic if available
    cats = state.get("canonical_categories") or []
    if cats:
        topic_q = " ".join(str(c) for c in cats[:3])[:100]
        all_snippets.extend(await _ddg_search(topic_q))

    if not all_snippets:
        return ""

    # 4. Build research brief via the slot's LLM
    research_ctx = "\n".join(f"- {s}" for s in all_snippets[:12])
    my_positions = "\n".join(f"- {c}" for c in claims[:5])

    prompt = (
        f"Ты {debater_id} в дебатах (фаза: {phase}).\n\n"
        f"Твои текущие позиции / решения:\n{my_positions}\n\n"
        f"Результаты поиска в интернете:\n{research_ctx}\n\n"
        f"На основе найденного:\n"
        f"1. Кратко укажи ошибки или слабости в своих позициях.\n"
        f"2. Предложи как их усилить, опираясь на найденные факты.\n"
        f"Ответ — максимум 500 символов, по делу."
    )
    msgs = [{"role": "user", "content": prompt}]
    ok, content = await call_llm(slot, msgs, max_tokens=400)
    if not ok:
        return ""
    return str(content).strip()[:800]


# ─── Posting ─────────────────────────────────────────────────────────────────


async def post_envelope(base_url: str, door_id: str, token: str,
                        envelope: dict[str, Any],
                        validation_id: str | None = None) -> tuple[int, Any]:
    """POST an envelope to the door as the slot's debater. Returns (status, body)."""
    url = f"{base_url.rstrip('/')}/debate/door/{door_id}/msg"
    headers = {"X-Debate-Token": token, "Content-Type": "application/json"}
    if validation_id:
        headers["X-Validation-Id"] = validation_id
    try:
        async with httpx.AsyncClient(timeout=POST_TIMEOUT) as cli:
            resp = await cli.post(url, headers=headers, json=envelope)
            try:
                return resp.status_code, resp.json()
            except Exception:  # noqa: BLE001
                return resp.status_code, {"text": resp.text}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


async def join_door(base_url: str, join_secret: str) -> tuple[bool, dict[str, Any]]:
    """Join a door via its join link. Returns (ok, {debater_id, token, door_id})."""
    url = f"{base_url.rstrip('/')}/debate/join/{join_secret}"
    try:
        async with httpx.AsyncClient(timeout=POST_TIMEOUT) as cli:
            resp = await cli.get(url)
            body = resp.json()
    except Exception as e:  # noqa: BLE001
        return False, {"error": str(e)}
    if resp.status_code == 200 and isinstance(body, dict) and body.get("token"):
        return True, body
    return False, body if isinstance(body, dict) else {"error": str(body)}


# ─── Remote pre-flight validation ────────────────────────────────────────────


async def validate_remote(base_url: str, door_id: str, token: str,
                          envelope: dict[str, Any]) -> tuple[int, Any]:
    """Stateless pre-flight validation against the live door. (status, body)."""
    url = f"{base_url.rstrip('/')}/debate/door/{door_id}/validate"
    headers = {"X-Debate-Token": token, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=POST_TIMEOUT) as cli:
            resp = await cli.post(url, headers=headers, json=envelope)
            try:
                return resp.status_code, resp.json()
            except Exception:  # noqa: BLE001
                return resp.status_code, {"text": resp.text}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def summarize_state(state: dict[str, Any]) -> str:
    """Compact textual context for the LLM from a /state snapshot."""
    if not isinstance(state, dict):
        return "(нет состояния)"
    lines = [
        f"phase: {state.get('current_phase')}",
        f"status: {state.get('status')}",
        f"judge_mode: {state.get('judge_mode')}",
    ]
    cats = state.get("canonical_categories") or []
    if cats:
        lines.append("canonical_categories: " + ", ".join(map(str, cats))[:1500])
    clusters = state.get("canonical_clusters") or []
    if clusters:
        lines.append("canonical_clusters: " + ", ".join(map(str, clusters))[:1500])
    atoms = state.get("atoms") or {}
    if atoms:
        lines.append("atoms:")
        for cat, slot in list(atoms.items())[:40]:
            vers = slot.get("versions", []) if isinstance(slot, dict) else []
            for v in vers[:6]:
                lines.append(
                    f"  • {v.get('atom_id')} cat={cat} v{v.get('v')} "
                    f"author={v.get('author')} pick={v.get('pick')} "
                    f"locked={v.get('locked')}")
    vio = state.get("violations_recent") or []
    if vio:
        lines.append("violations_recent:")
        for r in vio[:8]:
            lines.append(f"  • msg {r.get('msg_id')}: {r.get('rule')} — {r.get('detail')}")
    return "\n".join(lines)[:MAX_TRANSCRIPT_CHARS]


async def run_one_turn(*, base_url: str, door_id: str, token: str,
                       debater_id: str, slot: dict[str, Any],
                       role_doc: str, topic: str, motivation: str,
                       state: dict[str, Any],
                       max_retries: int = 2) -> dict[str, Any]:
    """Drive one debate turn for an API-debater slot.

    Generates an envelope via the slot's LLM, pre-validates it against the live
    door (retrying with violations fed back), and posts it only if clean.
    Returns a structured result dict (never raises).
    """
    phase = str(state.get("current_phase", "?"))
    transcript = summarize_state(state)
    last_violations: list | None = None
    for attempt in range(1, max_retries + 2):
        messages = build_messages(
            role_doc=role_doc, topic=topic, motivation=motivation,
            phase=phase, transcript=transcript, debater_id=debater_id,
            last_violations=last_violations,
            research_notes=slot.get("research_notes", ""))
        ok, content = await call_llm(slot, messages)
        if not ok:
            return {"ok": False, "stage": "llm", "error": content}
        envelope = extract_envelope(content)
        if envelope is None:
            return {"ok": False, "stage": "parse",
                    "error": "LLM did not return a JSON envelope",
                    "raw": content[:500]}
        vstatus, vbody = await validate_remote(base_url, door_id, token, envelope)
        violations = (vbody.get("violations") if isinstance(vbody, dict) else None) or []
        vid = (vbody.get("validation_id") if isinstance(vbody, dict) else None)

        # Server unreachable (network error) — return envelope for offline queue
        if vstatus == 0:
            return {"ok": False, "stage": "validate", "error": "server_unreachable",
                    "envelope": envelope}

        if vstatus == 200 and not violations:
            pstatus, pbody = await post_envelope(base_url, door_id, token, envelope,
                                                  validation_id=vid)
            if pstatus == 0:
                # Post failed due to network — save envelope for offline queue
                return {"ok": False, "stage": "post", "error": "server_unreachable",
                        "envelope": envelope}
            posted_ok = (pstatus == 200 and isinstance(pbody, dict)
                         and pbody.get("ok"))
            return {
                "ok": bool(posted_ok),
                "stage": "post",
                "attempt": attempt,
                "envelope_kind": envelope.get("kind"),
                "post_status": pstatus,
                "post_body": pbody,
            }
        # not valid yet — feed violations back and retry
        last_violations = violations or [{"error": vbody}]
    return {"ok": False, "stage": "validate", "error": "max retries exhausted",
            "violations": last_violations}
