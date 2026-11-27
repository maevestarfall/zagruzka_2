"""Prompt-library finder engine (pure-ish logic, network injected).

Idea: the user sends an image + a short wish ("хочу летать в небе"). We
extract keywords, search the configured prompt-source sites **in priority
order** (site #1 first, then #2, ...), collect matching ready-made prompts,
and additionally synthesise ONE hybrid prompt that blends the best matches
with the user's wish.

Network and LLM are injected as callables so this module stays testable:
* ``fetch(url) -> str`` returns page HTML/text.
* ``llm(prompt) -> str`` returns the hybrid (e.g. the bot's brain).

youmind.com is parsed specially (its listing exposes ``/prompts/<slug>-<id>``
links and the detail page carries the full prompt text). Any other site
falls back to a generic "lines/snippets containing the keywords" scan.
"""

from __future__ import annotations

import html as _html
import re

# Minimal RU+EN stopwords so keyword extraction keeps only meaningful tokens.
_STOP = {
    "i", "want", "a", "an", "the", "to", "in", "on", "of", "and", "with", "for",
    "me", "my", "is", "it", "this", "that", "be", "as", "at", "by",
    "я", "хочу", "мне", "это", "как", "в", "во", "на", "и", "с", "со", "по",
    "у", "о", "об", "за", "то", "же", "бы", "ли", "не", "да", "там", "типо",
    "типа", "чтобы", "что", "так", "вот", "из", "для", "под", "над",
}


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    """Lowercase tokens (RU+EN+digits) minus stopwords, de-duplicated."""
    toks = re.findall(r"[a-zA-Zа-яёА-ЯЁ0-9]+", (text or "").lower())
    out: list[str] = []
    for t in toks:
        if len(t) < 3 or t in _STOP:
            continue
        if t not in out:
            out.append(t)
    return out[:limit]


def title_from_slug(slug: str) -> str:
    """``cinematic-sunset-wooden-boat-portrait-26317`` -> readable title."""
    s = re.sub(r"-\d+$", "", slug)            # drop trailing numeric id
    return s.replace("-", " ").strip().title()


_PROMPT_LINK_RE = re.compile(r'href="(/prompts/([a-z0-9\-]+?-\d+))"')


def parse_youmind_listing(html: str, base: str = "https://youmind.com") -> list[dict]:
    """Extract prompt cards from a youmind listing page."""
    seen: set[str] = set()
    cards: list[dict] = []
    for m in _PROMPT_LINK_RE.finditer(html):
        rel, slug = m.group(1), m.group(2)
        if slug in seen:
            continue
        seen.add(slug)
        cards.append({
            "slug": slug,
            "url": base + rel,
            "title": title_from_slug(slug),
        })
    return cards


_SVGISH = re.compile(r"^[Mm][\d.\-]")           # SVG path data starts like "M12.1-"
_WORDY = re.compile(r"[A-Za-zА-Яа-яЁё]")


def extract_prompt_text(detail_html: str, hint_words: list[str] | None = None) -> str:
    """Pick the prompt body from a youmind detail page.

    The page also carries boilerplate (a Seedream marketing blurb identical on
    every page) and CSS/utility blobs. We keep natural-language candidates and
    choose the one with the best overlap with ``hint_words`` (title/keywords),
    breaking ties by length — this beats "just the longest string", which
    tends to grab the boilerplate.
    """
    hint = [w.lower() for w in (hint_words or [])]
    cands = re.findall(r'[\\"]([^"\\]{80,2000})[\\"]', detail_html)
    css_markers = ("flex", "grid", "px-", "py-", "text-", "bg-", "rounded",
                   "hover:", "md:", "border", "inline-", "gap-", "aria-")
    bad = ("cdn-cgi", "_next", "http", "://", "/media/", "width=", "quality=",
           "format=", "metadata=", "base64", "\\u", "function(", "svg")
    best, best_key = "", (-1, -1)
    for c in cands:
        c = c.strip()
        if _SVGISH.match(c) or c.startswith("/"):
            continue
        low = c.lower()
        if any(b in low for b in bad):
            continue
        if sum(1 for m in css_markers if m in low) >= 2:
            continue
        letters = len(_WORDY.findall(c))
        spaces = c.count(" ")
        if letters < 60 or spaces < 12 or (letters / max(1, len(c))) < 0.55:
            continue
        if not re.search(r"[.,]", c):
            continue
        overlap = sum(1 for w in hint if w in low)
        key = (overlap, len(c))
        if key > best_key:
            best_key, best = key, c
    best = _html.unescape(best)
    # the candidate may straddle an HTML tag boundary — keep only the prose
    best = re.split(r"<", best, 1)[0]
    best = best.lstrip(">{}, ").strip()
    return best


def score_card(title: str, keywords: list[str]) -> int:
    t = title.lower()
    return sum(1 for k in keywords if k in t)


def rank_site(html: str, keywords: list[str], *, is_youmind: bool) -> list[dict]:
    """Return cards from one site, scored by keyword overlap (desc)."""
    if is_youmind:
        cards = parse_youmind_listing(html)
        for c in cards:
            c["score"] = score_card(c["title"], keywords)
        # keep keyword matches first; if none match, keep trending order
        matched = [c for c in cards if c["score"] > 0]
        return sorted(matched, key=lambda c: -c["score"]) or cards[:5]
    # generic fallback: lines containing any keyword
    text = re.sub(r"<[^>]+>", " ", html)
    out: list[dict] = []
    for line in re.split(r"[\n.]+", text):
        line = line.strip()
        if 20 <= len(line) <= 400 and any(k in line.lower() for k in keywords):
            out.append({"title": line[:60], "url": "", "text": line,
                        "score": score_card(line, keywords)})
    return sorted(out, key=lambda c: -c["score"])[:10]


def find_ready_prompts(
    request: str,
    sites: list[str],
    fetch,
    *,
    per_site: int = 3,
    keywords: list[str] | None = None,
) -> dict:
    """Search ``sites`` in priority order, return ready prompts + keywords.

    ``fetch(url)`` must return page text/HTML (sync callable). Network errors
    on one site are recorded and skipped so a single dead site doesn't break
    the whole lookup. Pass ``keywords`` to override extraction (e.g. the
    handler translates a Russian wish into English search terms first, since
    the libraries are English).
    """
    keywords = keywords if keywords else extract_keywords(request)
    ready: list[dict] = []
    errors: list[str] = []
    for prio, site in enumerate([s for s in sites if s], start=1):
        is_ym = "youmind.com" in site
        try:
            html = fetch(site)
        except Exception as e:  # noqa: BLE001
            errors.append(f"сайт #{prio} ({site}): {e}")
            continue
        cards = rank_site(html, keywords, is_youmind=is_ym)[:per_site]
        for c in cards:
            text = c.get("text", "")
            if is_ym and c.get("url") and not text:
                try:
                    hint = extract_keywords(c.get("title", "")) + keywords
                    text = extract_prompt_text(fetch(c["url"]), hint_words=hint)
                except Exception:  # noqa: BLE001
                    text = ""
            ready.append({
                "priority": prio,
                "site": site,
                "title": c.get("title", ""),
                "url": c.get("url", ""),
                "text": text,
                "score": c.get("score", 0),
            })
    return {"keywords": keywords, "ready": ready, "errors": errors}


def build_hybrid_prompt(request: str, ready: list[dict], llm) -> str:
    """Ask the brain LLM to merge the top matches with the user's wish into a
    single hybrid image prompt. ``llm(text) -> str`` (may be sync or async —
    the caller awaits if needed). Returns a plain string."""
    refs = "\n".join(
        f"- {r['title']}: {r['text'][:300]}" for r in ready[:4] if r.get("text")
    ) or "(нет готовых примеров — сочини с нуля)"
    ask = (
        "Ты — генератор промтов для image-моделей (Nano Banana Pro). "
        "Слей пожелание пользователя и примеры ниже в ОДИН цельный английский "
        "промт для генерации картинки. Верни только сам промт, без пояснений.\n\n"
        f"Пожелание пользователя: {request}\n\n"
        f"Примеры-референсы:\n{refs}"
    )
    return llm(ask)
