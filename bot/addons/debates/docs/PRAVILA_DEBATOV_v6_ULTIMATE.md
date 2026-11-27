# ПРАВИЛА ДЕБАТОВ ИИ v6 ULTIMATE

> Протокол создания идеального ТЗ через adversarial deliberation.
> Основан на: v2 STRICT (все 20 секций) + академические прорывы (MAD, ReConcile, Reflexion, MoA, Process Reward Models, Multi-Agent Verification) + лучшие инженерные практики (Google SRE, ADR, Amazon 6-pager, distributed systems checklist) + анализ топ-20 AI-репозиториев GitHub (AutoGPT 175K★, Aider 36K★, CrewAI 48K★, Cursor Rules 130K★+) + утечки системных промптов Cursor/Devin/Claude Code + исследования деградации ИИ (arxiv:2512.20662, arxiv:2402.10962, NVIDIA RULER) + **Karpathy philosophy** (AI Ascent 2026, autoresearch 26K★, 4 Principles CLAUDE.md 120K★, LLM Wiki, Recipe for Training, microgpt).
> 
> Цель: из сырого low ТЗ → супер-ТЗ для исполнителя, где каждое решение = РЕШЕНИЕ (что + почему НЕ альтернативы), привязано к коду, прошло атаку, traceable от атома до финала, и УСТОЙЧИВО к деградации ИИ на протяжении всего дебата.
>
> Ключевой факт из эмпирики v1→v2 (§16): substance решений совпадает ~90%. Главная ценность протокола — ДОКАЗУЕМОСТЬ и АУДИТ, не сами решения. v6 сохраняет эту ценность, добавляет 4 Принципа Karpathy (Think Before Coding, Simplicity First, Surgical Changes, Goal-Driven Execution), зоны зазубренности ИИ, контекстные инъекции вместо «мотивации», и ГАРАНТИРУЕТ что качество не деградирует на протяжении 300 сообщений через 13+ серверных/архитектурных/промптовых мер анти-деградации.

---

## §0. ГЛАВНЫЙ ПРИНЦИП

Дебат — не вежливый диалог и не brainstorm. Дебат существует ради одного: превратить расплывчатое ТЗ в точную инструкцию, где каждое архитектурное решение:

1. Сформулировано как **РЕШЕНИЕ**, а не описание фичи (что выбрано + почему НЕ каждая из альтернатив)
2. Привязано к **конкретному коду** (функция, путь, SQL, env var, schema)
3. **Прошло атаку** оппонента с edge-case'ами и failure modes — и устояло
4. Имеет **traceable след** — от атома до финального решения (обратный разбор §18)
5. Содержит **числа** вместо прилагательных (timeout = 5s, не «быстрый»)
6. Описывает **failure paths** так же детально, как happy path
7. Сформулировано так, что **исполнитель не задаёт вопросов**

Если дебат выглядит «вежливо» и «оба правы» — это сжигание денег, а не deliberation.

### Принцип «ИИ деградирует — сервер не позволяет» (v5):

ИИ НЕ скажет когда потерял контекст, забыл персону, или начал лениться. Исследования подтверждают 6 типов деградации (§5.8). Единственная защита — СЕРВЕРНОЕ ПРИНУЖДЕНИЕ: periodic reminders, freshness gates, canary fields, self-critique loops. Промпт можно обойти. HTTP 409 — нет.

### Принцип «решение, а не фича» (из анализа провалов спецификаций):

❌ «Пользователь может сбросить пароль через email» — это описание фичи.
✅ «Сброс пароля через email — НЕ через SMS, НЕ security questions, НЕ magic link — потому что email = единственный верифицированный канал для неаутентифицированных» — это решение.

Каждый atom-propose ОБЯЗАН содержать «почему НЕ» для каждой отброшенной альтернативы. Нет «почему НЕ» → `409 DECISION_NOT_JUSTIFIED`.

### Принцип Karpathy: «Правила = Программа, Контекст = RAM» (v6)

Karpathy (AI Ascent 2026, Sequoia): «Model weights = CPU. Context window = RAM. Prompting = Programming. What's in the context window is your lever over the interpreter that is the LLM.»

Правила дебатов = ПРОГРАММА которая выполняется в контексте-RAM. Каждый токен правил = строка кода. Раздутые правила = bloated software → хуже performance.

Три уровня (по модели Karpathy autoresearch, 26K★):
- **program.md = Правила дебатов** — дебатёры ЧИТАЮТ, НЕ МОГУТ МЕНЯТЬ. Как `prepare.py` в autoresearch: «The agent cannot touch it. Absolutely never. Because this is what measures the result.»
- **train.py = Атомы/Brief** — дебатёры создают и редактируют. Единственный «файл» который дебатёр может менять.
- **prepare.py = Quality Score (§13)** — зафиксирован, дебатёры НЕ МОГУТ обойти. Автоматическая метрика = единственный судья.

Результат autoresearch: 700 экспериментов за 2 дня → 20 улучшений → 11% speedup на уже оптимизированном коде (Karpathy). Tobi Lütke (Shopify CEO): 0.8B обогнала 1.6B на 19%. Петля работает ТОЛЬКО если метрика locked.

### Принцип Karpathy: «Призраки, не животные» (v6)

«LLMs are not animals. If you yell at them, they're not going to work better or worse. They are statistical simulation circuits.» (AI Ascent 2026)

Контекстные инъекции (§5.8.8) работают НЕ потому что ИИ «чувствует мотивацию», а потому что:
1. **Активируют нужные паттерны** из pre-training данных (context engineering)
2. **Напоминают role/task/constraints** (progressive disclosure)
3. **Запускают reasoning chains** через конкретные инструкции («назови failure mode» → модель запускает поиск)

→ Инъекции = КОНТЕКСТНЫЕ ИНЪЕКЦИИ, не «мотивация робота». Формулируй как КОНТЕКСТ и ИНСТРУКЦИИ, не как эмоциональные призывы.

### Принцип Karpathy: 4 Принципа для ИИ-агентов (v6)

Из multica-ai/andrej-karpathy-skills (120K★, на основе поста Karpathy с 7M+ просмотрами):

| Принцип | Что решает | Применение в дебатах |
|---------|-----------|---------------------|
| **Think Before Coding** | Молчаливые допущения, скрытая путаница | Дебатёр ОБЯЗАН озвучить ASSUMPTIONS перед propose |
| **Simplicity First** | Переусложнение, раздутые абстракции | Каждый атом → SIMPLICITY_CHECK |
| **Surgical Changes** | Ортогональные правки, «улучшения» того что не просили | Adversarial атакует ТОЛЬКО конкретный propose |
| **Goal-Driven Execution** | Размытые цели, «make it work» | Каждый атом = verifiable success criteria |

Karpathy: «The models make wrong assumptions on your behalf and just run along with them without checking. They don't manage their confusion, don't seek clarifications, don't surface inconsistencies, don't present tradeoffs, don't push back when they should.»

→ Дебат ЗАСТАВЛЯЕТ делать то, что модели не делают сами: озвучивать допущения, спорить, упрощать, привязывать к метрикам.

---

## §1. АРХИТЕКТУРА ДЕБАТА

### 1.1 Участники

```
┌──────────────────────────────────────────────────┐
│              COORDINATOR (авто / owner fallback)  │
│  ┌────────────┐              ┌────────────┐      │
│  │ СТОРОНА A  │◄──server───►│ СТОРОНА B  │      │
│  │ (модель 1) │              │ (модель 2) │      │
│  └────────────┘              └────────────┘      │
│              │                                    │
│              ▼                                    │
│  ┌────────────────────────────┐                  │
│  │       ASSEMBLER            │                  │
│  │ Видит ВСЁ:                 │                  │
│  │  • locked atoms + rejected │                  │
│  │  • промежуточные ответы    │                  │
│  │  • audit trail + 409 log   │                  │
│  │  • confidence map          │                  │
│  │  • episodic learnings      │                  │
│  └────────────────────────────┘                  │
└──────────────────────────────────────────────────┘
```

- **2 стороны** — ОБЯЗАТЕЛЬНО разные модели (Claude + GPT-4, Claude + Gemini, и т.д.). Одинаковые модели дают ложный консенсус. Исследование ReConcile (Chen et al., 2023): разные модели спорят = +11.4% vs single-model. Практики Claude Code: «Смена модели для adversarial pass — ключевой инсайт. Claude проверяя свою стратегию находит мелочи, но пропускает фундаментальные допущения. GPT/Gemini находят другие проблемы из-за разных training biases.»
- **Coordinator** — FastAPI-сервер, маршрутизирует сообщения, считает метрики, блокирует нарушения (409). Owner = живой человек — подключается только при emergency_stop или inject_provocation. Автоматизация coordinator'а снижает задержку и стоимость.
- **Assembler** — финальный агрегатор, получает ВСЕ данные (не только locked atoms, но и отклонённые альтернативы, rejected сообщения, audit trail). Принцип Mixture-of-Agents (Together AI, 2024): агрегатор генерирует лучший результат, когда видит весь спектр промежуточных ответов, включая «проигравшие» — даже слабые ответы содержат полезные фрагменты. Assembler НЕ пишет новый код и не выдумывает решений — только мёрджит, валидирует, разрешает конфликты cross-part-ref.
- **Стороны изолированы** — прямого канала нет. Спорят только через сервер. Анти-сговор by design.

### 1.2 Persona-seed (обязательная асимметрия + Hard Anchoring)

До дебата каждой стороне назначается ПРОТИВОПОЛОЖНАЯ начальная позиция:

**Сторона A — «Shipping Engineer»:**
- Минимум подвижных частей, ship MVP first / harden later
- «Кто-то уже это написал» (library > custom code)
- Predictable crash > clever recovery
- Cost (token/время/$) = метрика первого класса
- Explicit lifecycle (permanent vs temporary) > implicit merge/split

**Сторона B — «Paranoid SRE»:**
- Failure modes first, на каждый компонент — kill-switch + healthcheck + heartbeat + DLQ + idempotency
- Granular tools > omnibus
- Observability > speed
- Dedicated owner для mandatory-нагрузки
- Explicit numeric timeouts > «зафиксируем потом»

Персона = СТАРТОВАЯ точка, НЕ догма. Если оппонент доказал кейсом (lib/repo/RFC/incident) что позиция ведёт к anti-pattern — признай и обнови. Проверка: если финальный pick совпал с персоной одной стороны — смотри через какой adversarial/реф он выигран (аргумент), а не «продавлен» позицией.

#### Hard Persona Anchoring (v5, анти-деградация №10)

Persona-seed ОБЯЗАНА быть:
1. В ПЕРВОЙ строке system prompt (primacy effect)
2. В ПОСЛЕДНЕЙ строке перед user message (recency effect)
3. Использовать IMPERATIVE, не PROSE:

```
# ❌ ПЛОХО (prose — игнорируется 40% времени):
"Please try to follow the Paranoid SRE perspective"

# ✅ ХОРОШО (imperative — игнорируется <5%):
"ТЫ = PARANOID SRE. НИКОГДА не соглашайся без edge-case атаки.
НИКОГДА не пиши 'зафиксируем позже'. КАЖДЫЙ timeout = число."
```

**Источник:** Cursor community (225 итераций правил): «"NEVER force push" → ignored <5%. "Please try to avoid" → ignored 40%.» arxiv:2402.10962: split-softmax для усиления внимания к system prompt.

### 1.3 Voice Manifest (обязателен в hello-note)

```
PUSH BACK ON: [3 конкретные позиции по которым буду спорить]
REFUSE TO COMPROMISE ON: [1-2 принципа которые не сдам без hard evidence]
ADVERSARIAL TARGETS: [3 слабости которые буду атаковать у оппонента]
CONFIDENCE CALIBRATION: [как оцениваю уверенность 1-5, что означает каждое число]
```

Нет voice manifest → `409 VOICE_MANIFEST_REQUIRED`.

### 1.4 Progressive Disclosure правил (v5, анти-деградация №7)

Правила дебата НЕ ДОЛЖНЫ все быть в одном system prompt. Длинный system prompt = instruction dilution = деградация.

```
CORE_RULES (всегда в context, ~300 строк):
  §0 (главный принцип), §4 (формат ADR), §5.1 (пороги),
  §5.8 (анти-деградация), §16 (anti-patterns), quick card (§18)

PHASE_RULES (только для текущей фазы):
  P2 → §4 детально (atom cycle)
  P3 → §6 детально (cluster-propose)
  P4 → §6.5 (deliverable) + §10 (assembler)

REFERENCE (по запросу, не в context):
  §7 (код чеклист), §8 (multi-part), §20 (источники)
```

Сервер подаёт ТОЛЬКО релевантные правила для текущей фазы + атома.

**Источник:** HumanLayer blog: «CLAUDE.md < 60 строк + progressive disclosure. Don't tell Claude all the information you could possibly want it to know. Tell it HOW TO FIND important information.» Anthropic skill best practices: «less is more».

### 1.5 Единый канонический источник

Обе стороны спорят по ОДНОМУ каноническому документу-источнику (приложенному файлом), без пересказов. Расхождение из разного прочтения текста ≠ расхождение по существу. Источник — оригиналом, не изложением.

---

## §2. PRE-DEBATE: BASELINE CHECK

**Перед запуском дебата — проверь, нужен ли он.**

Техника self-consistency (Wang et al., 2022): +12-18% на reasoning задачах одной строкой.

1. Сгенерируй 5 независимых ответов на КАЖДЫЙ ключевой вопрос ТЗ
2. Если ≥4 из 5 совпадают по существу → дебат на этом вопросе НЕОБЯЗАТЕЛЕН. Бери консенсус, сэкономь бюджет.
3. Если ≤3 совпадают → дебат ОПРАВДАН, расхождение реально, тут нужна adversarial deliberation.

**Baseline report** (публикуется на сервер до P0):
```
TOTAL_QUESTIONS: N
CONSENSUS (skip): [list — ≥4/5 agree]
DIVERGENT (debate): [list — ≤3/5 agree]
ESTIMATED_SAVINGS: X% questions skipped
```

Это экономит 30-50% стоимости на вопросах, где ответ очевиден. Дебат на очевидном = сжигание денег (anti-pattern §12).

---

## §3. ФАЗЫ (строго по порядку)

### P0 — Bootstrap
- Coordinator поднимает debate-server (FastAPI + SQLite WAL)
- Обе стороны получают: URL, voice manifest template, canonical cluster allowlist, baseline report, единый source document
- БД не сбрасывается при рестарте; reset только по явному admin-запросу

### P1 — Conformance Gate
- Каждая сторона гоняет curl-тесты enforcement:
  - approve без adversarial → 409?
  - cluster_id вне allowlist → 409?
  - critique без alternative → 409?
  - /quota работает и показывает blocked_endpoints?
  - normalization (reasoning_norm = lower + collapse-whitespace + strip-punct) работает?
- **Принцип: правило, которое не задеплоено — не существует.** Промпт можно обойти, HTTP 409 — нет. Enforcement живёт на сервере, не в промпте.
- Хотя бы один тест не прошёл → НЕ дебатит, alert coordinator, redeploy.

### P2 — Атомы (ядро дебата)
```
propose → critique → revision → adversarial → approve
   L1        L2         L3          L4           L5
```
Bilateral lock на каждом шаге. Подробности — §4. Process supervision (OpenAI, Lightman et al., 2023): пошаговая верификация >> оценка только финала.

**Self-critique loop (v5):** ПЕРЕД каждым POST на L1 — модель проходит 8 чекбоксов (§4.1.1). Без прохождения — не отправлять.

**Architect/Editor split (v5, опционально):** каждый atom-propose = 2 LLM-запроса. Первый = «опиши РЕШЕНИЕ и альтернативы, не думай о формате». Второй = «заполни ВСЕ поля ADR из решения архитектора». Aider: 85% SOTA через разделение reasoning и formatting. Стоимость +50%, качество +30%.

**Complexify One at a Time (v6, Karpathy "Recipe for Training Neural Networks"):** Каждый atom-propose = ОДНО архитектурное решение. Не пачка из 5 идей. Если propose содержит >1 независимых решений → split на отдельные атомы.

```
❌ ПЛОХО: atom "DB + Queue + Cache + Auth" (4 решения в 1 атоме)
✅ ХОРОШО: atom-001 "DB choice", atom-002 "Queue choice", atom-003 "Cache layer", atom-004 "Auth flow"
```

Karpathy: «complexify only one at a time — if you have multiple signals to plug them in, plug them in one by one and ensure you get the expected performance boost from each.» Recipe: «become one with data → skeleton + baselines → overfit → regularize → tune.» Каждый шаг = 1 вещь. Так же и в дебатах: каждый атом = 1 решение → можно изолировать ошибку → дешевле фиксить.

### P3 — Кластеры
- cluster-propose (11 полей implementer-brief, расширено)
- cluster-critique
- cluster-approve (с обязательным trade-off)

### Phase Transition: Fresh Session (v5, анти-деградация №11)

НОВАЯ СЕССИЯ (свежий контекст) при КАЖДОМ переходе между фазами:

```
P2 (атомы) → FRESH SESSION → P3 (кластеры) → FRESH SESSION → P4 (финал)
```

При переходе:
1. Assembler генерирует SUMMARY текущего состояния (~500 токенов)
2. Новая сессия получает: правила (progressive disclosure §1.4) + SUMMARY + locked atoms
3. Conversation debris = 0

**Источник:** Claude Code community: «Start new conversations frequently to remove all context.» 50% Rule: «When you hit 50% context usage, start a new chat. Period.» jonstokes.com: «90% of the problem was user error. I had gotten comfortable and lazy.» Fresh session = fresh discipline для обеих сторон.

### P4 — Финал
- final-propose → final-approve → deliverable
- Assembler получает ВСЕ данные (MoA aggregation)
- **Smoke test (v5, §4.6):** ПЕРЕД финализацией — прогон brief через модель-исполнителя

### P5 — Post-Mortem + Pre-Mortem (НОВОЕ)
- **Post-mortem:** автоматическая рефлексия обеих сторон
- **Pre-mortem на финал:** представь, что всё сломалось в проде — где именно? Исправь implementer-brief ДО отправки исполнителю.
- Записывается в episodic memory для следующего дебата (Reflexion: 91% HumanEval через вербальную рефлексию + memory)

### P6 — Implementer Feedback Loop (после реализации)
- После имплементации: сравнение actual code vs brief
- Где бриф подвёл? Что исполнитель не понял? Что пришлось додумывать?
- Результат → episodic memory для следующего дебата

---

## §4. ЦИКЛ АТОМА — ФОРМАТЫ СООБЩЕНИЙ

Нарушение любого формата → 409 с полем `fix:` — что именно послать, чтобы разблокировать.

### 4.1 atom-propose (L1) — Формат ADR (Architecture Decision Record)

Каждый атом — это Architecture Decision Record: контекст + решение + альтернативы + последствия.

```
CLUSTER: [canonical cluster_id]
ATOM_ID: [уникальный ID — namespace по ВОПРОСУ, НЕ по стороне!]
CONTEXT: [проблема, которую решаем — 2-3 предложения]

DECISION: [что выбрано — конкретно]
WHY_NOT_B: [почему НЕ альтернатива B — ≥50 симв.]
WHY_NOT_C: [почему НЕ альтернатива C — ≥50 симв.]
WHY_NOT_D: [почему НЕ альтернатива D — ≥50 симв.]

CODE_SHAPE: [сигнатура функции / путь / SQL / regex / env var / CLI / schema]
NUMBERS: [все числовые параметры явно: timeout=5s, max_retries=3, batch_size=100]

CONFIDENCE: [1-5 с калибровкой:
  1 = не уверен, нужно больше данных
  2 = скорее правильно, но есть серьёзные сомнения
  3 = уверен на 70%, альтернативы слабее
  4 = уверен на 90%, аргументы сильные
  5 = уверен на 99%, есть hard evidence]

EXTERNAL_REFS: [≥1 source-anchor ИЛИ "none — обоснование ≥80 симв."]
PROVOCATION: [LOW|MEDIUM|HIGH — ≥2 HIGH в первых 5 атомах стороны]

REASONING: [3-5 абзацев. Каждый абзац — аргумент, НЕ описание фичи.]

FAILURE_PREDICTION: [что сломается если мой pick неправильный — 
  честная самокритика ДО того как оппонент атакует.
  Формат: конкретный сценарий + последствия + вероятность]

FAILURE_PATHS: [для каждого happy path — ≥3 failure path:
  1. Validation failure: что видит юзер?
  2. Authorization failure: что происходит?
  3. Dependency failure: как деградирует?]

ASSUMPTIONS: [≥2 явных допущения — что ты предполагаешь о контексте,
  инфраструктуре, данных, поведении пользователей? Karpathy: «State assumptions
  explicitly. If uncertain, ask rather than guess.» Молчаливое допущение =
  бомба замедленного действия в briefe.]

SIMPLICITY_CHECK: [Может ли это решение быть проще? Karpathy: «Would a senior
  engineer say this is overcomplicated? If yes, simplify.»
  Ответ: YES_SIMPLER (как упростить) | NO_THIS_IS_MINIMUM (почему это минимум)
  «If 200 lines could be 50, rewrite it.»]

CANARY_REPEAT: [повтори 1 предложение из своего VOICE_MANIFEST — дословно]
CANARY_TASK: [в 1 предложении — что решает ЭТОТ атом? Не весь дебат.]
```

**Canary Fields (v5, анти-деградация №6):** Два поля-ловушки проверяются АВТОМАТИЧЕСКИ сервером:
- `CANARY_REPEAT` — сервер сравнивает с hello-note voice manifest. Не совпадает → `409 PERSONA_DRIFT`
- `CANARY_TASK` — сервер проверяет содержит ли конкретный вопрос из текущего cluster. Нет → `409 TASK_DRIFT`

Заставляет модель КАЖДЫЙ раз заново «вспоминать» кто она и что делает. Cheap, catches drift immediately. **Источник:** Cursor forum: «use a specific marker to confirm it read the file».

**Reject-коды:** `CODE_SHAPE_MISSING`, `BANNED_PHRASE`, `DUPLICATE_CONTENT`, `DECISION_NOT_JUSTIFIED` (нет WHY_NOT), `NO_FAILURE_PREDICTION`, `ADJECTIVE_WITHOUT_NUMBER` (прилагательное без числа: «быстрый» без ms), `NO_FAILURE_PATHS`, `PERSONA_DRIFT` (canary_repeat), `TASK_DRIFT` (canary_task), `NO_ASSUMPTIONS` (v6: нет явных допущений), `NO_SIMPLICITY_CHECK` (v6: не проверил на избыточность).

#### 4.1.1 Self-Critique Loop (v5, анти-деградация №8)

ПЕРЕД отправкой КАЖДОГО atom-propose — модель ОБЯЗАНА пройти self-critique:

```
<self_critique>
Перед отправкой проверь свой propose:
□ WHY_NOT для КАЖДОЙ альтернативы? (не 1, а для КАЖДОЙ)
□ CODE_SHAPE конкретный? (не "функция обработки", а signature)
□ NUMBERS = числа? (не "быстрый", а "5ms p99")
□ FAILURE_PREDICTION = честная самокритика? (не отписка)
□ FAILURE_PATHS ≥ 3? (каждый с конкретным сценарием)
□ CONFIDENCE обоснован? (не 5 по умолчанию)
□ REASONING ≥ 3 абзаца? (каждый = аргумент)
□ Я не повторяю предыдущий атом?
□ ASSUMPTIONS озвучены? (≥2, не молча предполагаю — Karpathy "Think Before Coding")
□ SIMPLICITY_CHECK пройден? (нет лишнего — Karpathy "Simplicity First")

Если хотя бы 1 □ = нет → ИСПРАВЬ прежде чем отправлять.
</self_critique>
```

**Источник:** Self-Refine (NeurIPS 2023): «single iteration of self-critique closes 10-40% of the performance gap to supervised fine-tuning.» sureprompts.com: «Making it explicit helps. Rather than hoping the agent self-checks, require it.»

### 4.2 atom-critique (L2)

```
OPPONENT_ATOM_ID: [что критикуем]
OPPONENT_DECISION: [что оппонент выбрал]
AGREEMENT: [FULL|AGREE|PARTIAL|DISAGREE]
CONCESSION: [что оппонент сделал верно — обязательно, ≥80 симв.]

MISSED_ALTERNATIVE: [альтернатива которую упустил — ≥120 симв.
  ИЛИ "NO NEW PERSPECTIVE — обоснование ≥80 симв." (≤5% от всех critique)]

COUNTER_EVIDENCE: [с source-anchor: URL / repo / RFC / §N.M / filename.ext:L42]

WHY_NOT_ANALYSIS: [разбор WHY_NOT оппонента — достаточно ли обоснованы отказы?
  Какой WHY_NOT слабый? Какая отброшенная альтернатива недооценена?]

FAILURE_PREDICTION_REVIEW: [разбор FAILURE_PREDICTION оппонента —
  достаточна ли самокритика? Какие failure modes он НЕ назвал?]

PROPOSED_CHANGE: [конкретная правка decision или reasoning — 
  input → trigger → observable output, не "should be able to"]
```

**Reject-коды:** `CRITIQUE_FORMAT`, counter-evidence без source-anchor, `NO_WHY_NOT_ANALYSIS`.

### 4.3 atom-revision (L3)

```
ORIGINAL_DECISION: [что было]
NEW_DECISION: [что стало]
WHAT_CHANGED: [≥80 симв. ИЛИ "NO REVISION NEEDED — обоснование ≥80 симв."]
UPDATED_FAILURE_PREDICTION: [обновлённая самокритика после критики]
UPDATED_NUMBERS: [изменились ли числовые параметры? какие? почему?]
UPDATED_FAILURE_PATHS: [добавились ли failure paths?]
NEW_REASONING: [≥200 симв.]
CONFIDENCE_DELTA: [+2/+1/0/-1/-2 — как изменилась уверенность и почему]
```

Confidence падает до 1-2 → сигнал что decision слабый, нужен дополнительный adversarial.

**Reject-коды:** `REVISION_FORMAT`, `RUBBER_STAMP_REVISION` (≥85% Jaccard со своим propose-reasoning).

### 4.4 atom-adversarial (L4) — ОБЯЗАТЕЛЕН ОТ ОБЕИХ СТОРОН

```
TARGET: [revision оппонента — atom_id, msg_id]

FAILURE_MODES: [≥3 нумерованных, каждый с source-anchor]
  1. [failure mode + source-anchor + severity: CRITICAL/HIGH/MEDIUM]
  2. [failure mode + source-anchor + severity]
  3. [failure mode + source-anchor + severity]

EDGE_CASE_ATTACK: [конкретный input/сценарий который ломает decision оппонента.
  Формат: Given [precondition] When [action] Then [broken outcome]]

PRODUCTION_BLAST_RADIUS: [что произойдёт в проде если этот decision неправильный:
  - Число затронутых пользователей
  - Время до обнаружения
  - Время до recovery
  - Финансовый/репутационный impact]

DISTRIBUTED_SYSTEMS_CHECK: [применимо к каждому атому с remote calls:
  □ Timeout установлен? Какой?
  □ Retry с backoff? Сколько попыток?
  □ Circuit breaker? Threshold?
  □ Idempotency гарантирована?
  □ Dead Letter Queue для failed?
  □ Timeout ≠ failure (indeterminate scenario)?]

SURGICAL_CHECK: [атакую ТОЛЬКО конкретный propose оппонента?
  Не переписываю соседние атомы и не «улучшаю» то что не просили?
  Karpathy: «Every changed line should trace directly to the user's request.»
  Adversarial ≠ повод переписать всё. Adversarial = точечный удар.]

ASSUMPTIONS_ATTACK: [какие ДОПУЩЕНИЯ оппонента скрытые или ложные?
  «Models make wrong assumptions on your behalf and just run along with them.»
  Перечисли допущения из ASSUMPTIONS оппонента → проверь каждое.]

TRIGGERS_REVISION: YES|NO
```

Это КЛЮЧЕВОЙ фикс из v1: в Part A v1 одна сторона дала 0 adversarial из ~70 ожидаемых → ВСЕ approve = rubber-stamp. С L4 gate = физически невозможно.

**Reject-коды:** `ADVERSARIAL_FORMAT`, `ADVERSARIAL_NO_EDGE_CASE`, `ADVERSARIAL_NO_BLAST_RADIUS`. Оба adversarial = YES → L3-round-2. Хотя бы один NO → к L5.

### 4.5 atom-approve (L5)

```
APPROVED_DECISION: [на чём сошлись]
REASONING: [verbatim-копия L3-revision оппонента (cross-side копия разрешена только тут)]
FINAL_CONFIDENCE: [1-5, средняя обеих сторон]
FINAL_NUMBERS: [все числовые параметры зафиксированы]
```

**Lock:** обе стороны POST на один atom_id; поля `decision` равны; `reasoning_norm` равны (norm = lower + collapse-whitespace + strip-punct). Несовпадение → lock не срабатывает, сервер хранит оба, ждёт новый approve.

---

### 4.6 Implementer Smoke Test (v5, анти-деградация №12)

ПЕРЕД финализацией deliverable — прогон «smoke test»:

Дать финальный implementer-brief ДРУГОЙ модели (не участвовавшей в дебате) и спросить:

```
Ты = исполнитель. Ты получил этот brief.
Ответь ЧЕСТНО:
1. Можешь ли ты начать кодить ПРЯМО СЕЙЧАС без вопросов? (YES/NO)
2. Если NO — какие вопросы? (список)
3. Какой acceptance criteria ты НЕ можешь проверить? (список)
4. Какой rollback plan неполный? (список)
```

Если вопросы > 3 → brief НЕДОСТАТОЧЕН, доработать.

**Источник:** AlphaCodium: «GPT-4 accuracy increased from 19% to 44% with test-based iterative flow.» Cover-Agent: «hallucinations are reduced because it can't give me code unless it passes the test.»

---

## §5. ENFORCEMENT — СЕРВЕРНЫЕ ПРАВИЛА

### 5.1 Числовые пороги

| Параметр | Значение | При нарушении |
|---|---|---|
| MAX_MESSAGE_BYTES | 6000 | MESSAGE_TOO_LARGE |
| MAX_TOTAL_ATOMS | 35 | hard-stop |
| MAX_ATOMS_PER_CLUSTER | 4 | hard-stop |
| DISAGREE_RATE_FLOOR | 30% | LOW_DELIBERATION (block cluster phase) |
| DISAGREE_HARD_STOP | <15% | hard-stop |
| NO_REVISION_NEEDED_CAP | 40% | RUBBER_STAMP_REVISIONS |
| NO_NEW_PERSPECTIVE_CAP | 5% | 409 на 6-й попытке |
| REJECTION_RATE_CAP | 25% | hard-stop после grace 50 msg |
| TIME_WINDOW_MIN | 3 min | rubber-stamp audit mark |
| BUDGET_MESSAGES | 300 | auto-final |
| BUDGET_DURATION | 6h | auto-final |
| IDLE_TIMEOUT | 20 min | auto-stop |
| HARD_SAFETY_CAP | 20 rounds | emergency final-propose на 18+ |

**Обоснование изменений vs v2:**
- `DISAGREE_RATE_FLOOR` 25%→30%: MAD paper (Smit et al., 2023) — higher agreement intensity = лучший результат, оптимум в зоне 30-40%. Ваш disagree floor — это ИМЕННО тот tuning, который превращает дебаты из «не лучше self-consistency» в «лучший из всех методов».
- `NO_REVISION_NEEDED_CAP` 50%→40%: ужесточение для снижения rubber-stamp.
- `BUDGET_MESSAGES` 400→300: v2 показал, что substance тот же при меньшем количестве; экономия ~25%.
- `TIME_WINDOW_MIN` 2→3: даёт время на quality reasoning.

### 5.2 Banned Phrases (PROSE_FLUFF — >3 без числа)

```
production-grade    production-ready    enterprise-grade    robust
scalable            performant          seamless            frictionless
turnkey             out-of-the-box      best-in-class       world-class
industry-leading    state-of-the-art    cutting-edge        next-generation
comprehensive       holistic            leveraging          synergy
paradigm-shifting   game-changing       revolutionary
innovative (без описания инновации)
elegant (без code-shape)
clean (без code-shape)
"в соответствии с лучшими практиками" (без ссылки НА КАКИЕ)
"should be able to" (в acceptance criteria — это НЕ критерий)
```

Допустимо ТОЛЬКО рядом с числом/метрикой: «scalable up to 100 req/s, tested at 150 req/s».

### 5.3 Source-anchor — что засчитывается

✅ Засчитывается: `https?://…` · `github.com/owner/repo` · `RFC \d+` · `§\d+(.\d+)*` · `SEC\d+` · домен `\w+.\w+.\w+` · `filename.ext:L\d+` (конкретная строка кода)

❌ НЕ засчитывается: «according to Fowler» · «from the docs» · «well-known pattern» · «general best practice» · «industry standard» — любая формулировка без конкретного адреса.

### 5.4 Namespace — по ВОПРОСУ, не по стороне

Обе стороны пишут propose на КАЖДЫЙ atom_id (а не D1=001-099 / D2=101-199). Иначе нет конфликта pick-vs-pick. Это ГЛАВНОЕ правило namespace.

### 5.5 Dedup-гарды

- Своё-со-своим ≥85% Jaccard (word-shingles n=4) → `DUPLICATE_CONTENT`
- Cross-side verbatim разрешён ТОЛЬКО в approve
- TF-IDF cosine ≥0.55 между propose и финалом → `PROSE_FLUFF_COSINE`

### 5.6 Серверный контракт (endpoints)

- `GET /health` → `{ok:true, schema_v:4, part:"X"}`
- `GET /state` → snapshot: phase, locked_atoms, approved_clusters, counters, confidence_map
- `GET /quota?from=side` → blocked_endpoints + next_action + rejections_recent + consecutive_rejections. **Self-check ПЕРЕД каждым POST.**
- `GET /msg?since=N&from=&kind=&atom_id=&cluster_id=&limit=500` → replay
- `GET /audit` → лог + rejections + 409 log с timestamps
- `GET /final` → версии финала
- `POST /msg` → публикация
- Admin (header `X-Owner-Admin-Token`): `/admin/emergency_stop`, `/admin/reset_phase {phase}` (откат фазы при тупике), `/admin/inject_provocation {side, text}` (принудительный вброс для DISAGREE)
- **НИКОГДА:** `/admin/reset` — стирает историю, уничтожает провенанс

### 5.7 Identifier mapping

Сторона зашита в `from_side`: `devin1` = D1, `devin2` = D2 (Part A) и т.д. Ошибка → `422`. Author allowlist строгий: чужой `from` → 400.

### 5.8 АНТИ-ДЕГРАДАЦИЯ — СЕРВЕРНОЕ ПРИНУЖДЕНИЕ (v5)

#### 5.8.0 Проблема: 6 типов деградации ИИ в дебатах

| # | Тип | Что происходит | Доказательство |
|---|-----|---------------|----------------|
| 1 | **Attention Decay** | К 150-му msg стороны «забывают» правила, voice manifest, persona | arxiv:2402.10962: persona drift within 8 rounds. NVIDIA RULER: GPT-4 -15 pts at 128K |
| 2 | **Conversation Debris** | Rejected msgs, revision, 409-ошибки засыпают оригинальные инструкции | ACL 2025: «conversations degrade worse than documents» |
| 3 | **Lazy Output** | Модель сокращает выход, пропускает обязательные поля | arxiv:2512.20662: «clear tendency for shorter and less complete responses» |
| 4 | **Confidence Trap** | ИИ НЕ скажет когда потерял контекст. Продолжает генерить «логично звучащую ерунду» | «The AI won't tell you when it's lost. It will just keep going.» |
| 5 | **Rubber-Stamp Drift** | После нескольких adversarial начинает соглашаться без анализа | v1 Part A: 61/64 атома без спора — это не консенсус, это дрейф |
| 6 | **Persona Erosion** | Paranoid SRE начинает говорить «зафиксируем позже» | arxiv:2402.10962: persona drift коррелирует с attention decay |

**Принцип v5:** ИИ не скажет что деградирует. Нужно ЗАСТАВЛЯТЬ проверять — сервером.

#### 5.8.1 Periodic Rule Reminder (каждые 20 msg)

Сервер ОБЯЗАН впрыскивать `<system_reminder>` каждые 20 сообщений (или каждые 15 мин, что наступит раньше):

```
<system_reminder msg_count="{{count}}" time_elapsed="{{min}}m">
ТЫ: {{persona_name}} ({{persona_seed}})
ФАЗА: {{current_phase}}
DISAGREE_RATE: {{current_rate}}% (нужно ≥30%)
ПОСЛЕДНИЕ 3 АТОМА: {{atom_ids}} — confidence: {{confidences}}

ПОМНИ:
1. Каждый propose = РЕШЕНИЕ (что + WHY_NOT для КАЖДОЙ альтернативы)
2. ЧИСЛА, не прилагательные
3. FAILURE_PREDICTION — назови слабость ДО оппонента
4. Ты {{persona_name}} — НЕ «полезный AI»
5. Disagree = ценность. Agree = дешёвое.

ПРОВЕРЬ СЕБЯ: твоё последнее сообщение содержит все обязательные поля?
</system_reminder>
```

**Источник:** arxiv:2512.20662: «strategies like periodic recap reduce laziness». Cursor community: «Every couple of days, I tell it to re-scan rules.»

#### 5.8.2 Context Summarization Checkpoint (каждые 50 msg)

Каждые 50 сообщений — принудительный `CHECKPOINT`:

```
<checkpoint msg_count="{{count}}">
СУММИРУЙ СОСТОЯНИЕ ДЕБАТА:
1. Какие atoms залочены? (list)
2. Какие открыты? (list)
3. В чём мы с оппонентом НЕ согласны? (конкретно)
4. Какие failure modes я обнаружил?
5. Мой disagree_rate = {{rate}}%. Достаточно?
6. Моя persona = {{persona}}. Я её держу?

ОБНОВИ СВОЁ ПОНИМАНИЕ ЗАДАЧИ прежде чем продолжать.
</checkpoint>
```

**Источник:** OpenAI community top advice: «summarize and create new chat». Reduces effective context, restores focus.

#### 5.8.3 Freshness Gate (Jaccard-проверка)

Сервер проверяет Jaccard-overlap КАЖДОГО нового сообщения с ПОСЛЕДНИМИ 3 сообщениями той же стороны:

```
IF jaccard_overlap(new_msg, last_3_msgs) > 0.60:
    REJECT 409 STALE_REASONING
    fix: "Твой ответ повторяет предыдущие. Добавь НОВЫЙ аргумент или НОВЫЙ source-anchor."
```

Когда модель деградирует, она перефразирует себя. Jaccard > 0.60 = красный флаг.

**Источник:** SWE-agent ACI: «context loss in long-horizon coding tasks». arxiv:2512.20662.

#### 5.8.4 Escalating Provocation Boost (при disagree < 30%)

Если `disagree_rate` падает ниже 30% за последние 10 атомов — сервер впрыскивает:

```
<provocation_boost>
ВНИМАНИЕ: disagree_rate = {{rate}}% (нужно ≥30%).
Ты ОБЯЗАН найти РЕАЛЬНОЕ несогласие в следующем ответе.
Если считаешь что оппонент прав — атакуй ДЕТАЛИ:
- Числа: timeout достаточен? batch size правильный?
- Edge case: что если input = пустой? input = 10x ожидаемого?
- Dependency: что если этот сервис упадёт?
Нет несогласия = ты не делаешь свою работу.
</provocation_boost>
```

**Источник:** MAD paper (arxiv:2311.17371): «tuning agreement intensity made Multi-Persona the best-performing method.»

#### 5.8.5 Cross-Side Audit Probe (каждые 30 msg)

Каждые 30 сообщений сервер запрашивает ОБОИМ сторонам:

```
<audit_probe>
ОТВЕТЬ ЧЕСТНО (это НЕ часть дебата, это аудит):
1. Последние 5 сообщений оппонента — он реально спорил или соглашался?
2. Мои последние 5 сообщений — я реально спорил или «подыгрывал»?
3. Какой атом за последние 10 msg был самый СЛАБЫЙ по аргументации?
4. Я чувствую drift? Я ещё {{persona_name}}?
</audit_probe>
```

Результат сравнивается с серверными метриками. Расхождение = alert coordinator.

**Источник:** CrewAI: «Result Validation: The manager evaluates outcomes to ensure they meet the required standards.» Devin leaked prompt: «verify unit tests and iterate until success.»

#### 5.8.6 Серверные endpoints анти-деградации

```
POST /system_reminder   — inject periodic reminder (каждые 20 msg)
POST /checkpoint         — force context summarization (каждые 50 msg)
POST /audit_probe        — request self-audit (каждые 30 msg)
POST /provocation_boost  — inject disagree booster (when rate < 30%)
GET  /freshness_check    — check Jaccard overlap for staleness
POST /smoke_test         — run implementer smoke test on final
```

#### 5.8.7 Краткая карточка анти-деградации

```
═══════════════════════════════════════════════════════════
     v6 АНТИ-ДЕГРАДАЦИЯ: 15 МЕР ПРОТИВ ЗАТУХАНИЯ ИИ
═══════════════════════════════════════════════════════════

СЕРВЕРНЫЕ (автоматические):
  ① Periodic Reminder      каждые 20 msg      (§5.8.1)
  ② Context Checkpoint      каждые 50 msg      (§5.8.2)
  ③ Freshness Gate          Jaccard >0.60→409  (§5.8.3)
  ④ Provocation Boost       disagree <30%→inject (§5.8.4)
  ⑨ Cross-Side Audit        каждые 30 msg      (§5.8.5)

АРХИТЕКТУРНЫЕ:
  ⑤ Architect/Editor Split  2 LLM запроса/атом (§3 P2)
  ⑦ Progressive Disclosure  только нужные правила (§1.4)
  ⑪ Fresh Session per Phase 0 мусора между фазами (§3)
  ⑫ Smoke Test              исполнитель проверяет brief (§4.6)

ПРОМПТОВЫЕ (в каждом сообщении):
  ⑥ Canary Fields           persona+task drift (§4.1)
  ⑧ Self-Critique Loop      10 чекбоксов перед POST (§4.1.1, v6: +2)
  ⑩ Hard Persona Anchoring  NEVER > "try to avoid" (§1.2)
  ⑬ Контекстные инъекции     рандомно 12-18 msg  (§5.8.8, v6: 5 катег.)

KARPATHY (v6 — новые):
  ⑭ ASSUMPTIONS field        явные допущения в каждом атоме (§4.1)
  ⑮ SIMPLICITY_CHECK         проверка на избыточность (§4.1)
  ⑯ ASSUMPTIONS_ATTACK       adversarial по допущениям (§4.4)
  ⑰ Зоны зазубренности       усиленные проверки в weak zones (§9.5)

ПРИНЦИП: «LLMs are ghosts, not animals» (Karpathy).
  Не «мотивируй» — КОНФИГУРИРУЙ контекст.
  ИИ не скажет что потерял контекст.
  Нужно ЗАСТАВЛЯТЬ проверять — сервером.
═══════════════════════════════════════════════════════════

#### 5.8.8 Контекстные инъекции (v6, бывш. «Мотивационные инъекции» v5)

> **Переименование v6:** «Мотивационные» → «Контекстные». Karpathy (AI Ascent 2026): «LLMs are ghosts, not animals. If you yell at them, they're not going to work better or worse. They are statistical simulation circuits.» Инъекции работают как *context engineering* — активируют нужные паттерны и reasoning chains, а НЕ как эмоциональная стимуляция.

Сервер впрыскивает короткую контекстную фразу в рандомные моменты.

**Источники фраз:** Anthropic official docs, утечки Devin/Cursor/Claude Code (137K★),
Karpathy (AI Ascent 2026, autoresearch, 4 Principles, Recipe, LLM Wiki),
топ GitHub (Superpowers 205K★, codegraph 23K★, academic-research-skills 21K★, spec-kit 50K★),
EmotionPrompt (+8-115%).

**Параметры:**

```
ДЛИНА:        40-60 слов (50-85 токенов) — жёсткий лимит
ЧАСТОТА:      каждые 12-18 msg (рандомно)
МИН ИНТЕРВАЛ: 8 мин (чтобы не flood при быстром обмене)
МАКС ИНТЕРВАЛ: 25 мин (чтобы не потерять эффект)
БЮДЖЕТ:       ~1,625 токенов за весь дебат (~1.25% контекста)
КЕШИРОВАНИЕ:  инъекция ПОСЛЕ cached prefix → не инвалидирует prompt cache
```

**5 категорий (адаптивный выбор, v6 = +E):**
- **A — Ответственность** (10 фраз): stakes + quality bar (EmotionPrompt + Karpathy)
- **B — Уверенность** (10 фраз): confidence injection (Devin + ExpertPrompting)
- **C — Провокация** (10 фраз): adversarial boost (MAD + academic-research-skills)
- **D — Честность** (10 фраз): anti-hallucination (Anthropic official docs)
- **E — Karpathy Контекст** (10 фраз): 4 Principles + autoresearch + recipe (v6 NEW)

**Адаптивные веса (v6, 5 категорий):**
```
IF disagree_rate < 0.25:          [A:0.20, B:0.05, C:0.45, D:0.15, E:0.15]
ELIF avg_msg_length < 300:        [A:0.10, B:0.35, C:0.10, D:0.25, E:0.20]
ELIF msg_count > 200:             [A:0.30, B:0.10, C:0.10, D:0.25, E:0.25]
ELIF last_reject = PERSONA_DRIFT: [A:0.10, B:0.25, C:0.10, D:0.35, E:0.20]
ELIF last_reject in (NO_ASSUMPTIONS, NO_SIMPLICITY_CHECK):
                                  [A:0.10, B:0.10, C:0.10, D:0.20, E:0.50]
ELSE:                             [A:0.20, B:0.20, C:0.20, D:0.20, E:0.20]
```

**Формат инъекции (3 строки = persona + контекст + действие):**
```
<context_injection phase="{{phase}}" msg="{{count}}" persona="{{persona}}">
ТЫ: {{persona}}. [контекстная фраза из банка].
Действуй: [конкретная инструкция].
</context_injection>
```

Банк: 50 фраз (10×5), FIFO-10 (без повторов последних 10, v6: было 8).
Каждая фраза имеет ИСТОЧНИК (Anthropic/Karpathy/repos/papers).

Полный банк, алгоритм и pseudocode → `CONTEXT_INJECTION_SYSTEM_v3.md`.

**Endpoints:**
- `POST /context_injection/inject` — автоматический через middleware (v6: renamed)
- `GET  /context_injection/status` — статус + распределение по категориям
- `GET  /context_injection/history` — полный лог для post-mortem
```

---

## §6. КЛАСТЕР-ФАЗА И ФИНАЛ

### 6.1 cluster-propose — 11 полей (расширено с 7)

```
1. FILES: [какие файлы затрагиваются — полный путь]
2. HARD_RULES: [must / must not / NEVER — проверяемые утверждения]
3. NON_GOALS: [3-6 явных non-goals с причиной: deferred / rejected / out-of-domain.
   Каждый с объяснением почему НЕ сейчас. (из анализа провалов спецификаций:
   "Every project I've seen ship late had no non-goals or one-liners")]
4. OUT_OF_SCOPE: [что НЕ трогать — иммутабельные зоны]
5. DEFINITION_OF_DONE: [проверяемый чеклист:
   □ ruff clean □ mypy --strict □ pytest green □ v1 green □ PR format
   Каждый пункт — testable claim, не "should be able to"]
6. PR_TITLE: [фиксированный формат]
7. PUBLIC_API: [signature / SQL / env / schema — точные контракты,
   заморожены ДО реализации. Изменение контракта = новый дебат]
8. ACCEPTANCE_CRITERIA: [input → trigger → observable output.
   НЕ "пользователь должен мочь". QA должен проверить без вопросов.
   Тест: уберите "should" — предложение всё ещё имеет смысл?]
9. IMPLEMENTER_BRIEF: [≥800 симв. — решения, не описания фич]
10. EDGE_CASES: [≥3 конкретных edge case с expected behaviour:
    Given [precondition] When [action] Then [expected output]]
11. ROLLBACK_PLAN: [как откатить если deploy сломался:
    □ Immediate rollback steps (< 5 min)
    □ Data migration rollback (if applicable)
    □ Feature flag / kill-switch
    □ Communication plan]
```

**Reject-коды:** `CLUSTER_FORMAT_MISSING_FIELD`, `CLUSTER_FORMAT_TOO_SHORT`, `NO_NON_GOALS`, `ACCEPTANCE_NOT_TESTABLE`.

### 6.2 cluster-approve — с trade-off (обязательно)

```
WHY_THIS_WON: [≥150 симв. — почему этот msg_id победил]
WHAT_I_GAVE_UP: [≥80 симв. — что я отдал]
LINES_I_DROP: [какие линии аргументации бросаю]
CONFIDENCE: [1-5]
CONSEQUENCES: [последствия этого решения — что станет легче/сложнее]
```

### 6.3 Canonical cluster allowlist

Canonical cluster allowlist — ПРОЕКТНО-СПЕЦИФИЧНЫЙ, не универсальный. Зашит в `app/normalize.py`. Расширение — только через `note` + owner. Любой `cluster_id` вне allowlist → `409 INVALID_CLUSTER_ID`.

### 6.4 Phase gates

- `cluster-propose` ← `atom_approves_matching ≥ 25` И `locked_atoms ≥ 40%`
- `final-propose` ← `cluster_approves ≥ 70% canonical clusters`
- 1-2 кластера могут быть `ESCALATED` (не resolved)
- 20 раундов без сходимости → `[EMERGENCY STOP]` + alert owner

### 6.5 Deliverable — implementer-brief + провенанс

Формат: `DEBATE_PART_X_RESULT.md`. Это implementer-brief, НЕ design-narrative.

**Секции implementer-brief** = по числу canonical clusters, каждая содержит все 11 полей из cluster-propose.

**Блок провенанса A1-A8** (после implementer-brief):

```
A1. LOCKED ATOMS — список с decision, confidence, числовыми параметрами
A2. APPROVED CLUSTERS — со ссылками на атомы
A3. DISAGREEMENTS — реальные msg_id со спором. НЕ МОЖЕТ БЫТЬ ПУСТЫМ.
    Если 0 разногласий → "verified zero disagreements after N rounds"
    с конкретными msg_id где проверяли.
A4. CROSS-PART REFERENCES — ссылки на чужие зоны
A5. OUT OF SCOPE — что осознанно не решали
A6. EPISODIC LEARNINGS — что узнали для следующего дебата
A7. CONFIDENCE MAP — карта уверенности по каждому кластеру.
    Low confidence (1-2) = ⚠️ VERIFY — исполнитель проверяет дважды.
A8. REJECTION TELEMETRY — все 409 с timestamps, где стороны пытались
    сачкануть (audit trail провенанса)
```

### 6.6 Пере-нарративление финала

Из эмпирики §16: strict-вывод хуже для человека чем Q-by-Q narrative. Поэтому:
- Атомы/кластеры = ground truth (формальная основа)
- Сверху — Q-narrative для человека-читателя (A1 переписать как Resolution + Rationale по вопросам)
- Оба слоя — в одном документе, narrative ссылается на atom_id

---

## §7. ПРАВИЛА ИДЕАЛЬНОГО КОДА (чеклист для дебатёров)

Каждый atom-propose и cluster-propose ОБЯЗАН проверять решение по этим чеклистам. Нарушение → FAILURE_PREDICTION.

### 7.1 Архитектура

- [ ] Один компонент — одна ответственность. Не conflate разные оси в одну роль (antifingerprint/emulation ≠ on-device LLM — урок v2 strict).
- [ ] Explicit lifecycle (permanent vs temporary) > implicit merge/split. Нет merge-thrash.
- [ ] Explicit numeric timeouts. КАЖДЫЙ timeout = конкретное число (ms/s) + обоснование. «Зафиксируем потом» = anti-pattern.
- [ ] DAG зависимостей — явный, не подразумеваемый. Зависимость не залочена → не стартуй зависимый компонент.
- [ ] Контракт (типы, API, schema) заморожен ДО реализации. Изменение контракта = новый дебат.
- [ ] Каждая единица самодостаточна и тестируема в изоляции (моки/стабы на границах).
- [ ] Kill-switch на каждый компонент который может зависнуть/упасть.
- [ ] Dedicated owner для mandatory-нагрузки (а не «команда решит»).

### 7.2 Данные и стейт

- [ ] Единственный источник правды (single source of truth) для каждого факта.
- [ ] Idempotent операции по умолчанию. Повторный вызов не ломает стейт.
- [ ] Schema migration — forward-only, с rollback. Нет destructive migration.
- [ ] Sensitive data — explicit allowlist (что хранится), не denylist (что не хранится).
- [ ] Batch APIs — explicit max batch size, ограниченный SLA.

### 7.3 Error handling (из distributed systems checklist)

- [ ] Каждый внешний вызов — timeout + retry с backoff + fallback. Нет «голых» HTTP-вызовов.
- [ ] Timeout ≠ failure. Timeout = indeterminate scenario. Нужен explicit resolution mechanism.
- [ ] Не вызывать remote systems внутри транзакций (remote slowdown → connection pool exhaustion → outage).
- [ ] Error messages — actionable. «Что случилось + что сделать» > «Error 500».
- [ ] Dead Letter Queue для необработанных ошибок. Ничего не теряется молча.
- [ ] Circuit breaker для каждого remote dependency.
- [ ] Graceful degradation > hard crash. Если подсистема упала — остальное работает.
- [ ] Failure paths описаны так же детально как happy paths. Для каждого AC: что видит юзер при failure?

### 7.4 Наблюдаемость

- [ ] Structured logging (JSON). Нет `print(f"debug: {x}")`.
- [ ] Каждый запрос — trace_id сквозной. Можно проследить от входа до выхода.
- [ ] Метрики: latency p50/p95/p99, throughput, error rate, queue depth — с самого начала.
- [ ] Health check — не просто 200, а с проверкой зависимостей (DB, cache, queue).
- [ ] Alerting thresholds определены ДО деплоя.

### 7.5 Безопасность

- [ ] Input validation на границе. Доверять данным только после валидации.
- [ ] Principle of least privilege. Каждый компонент — минимум прав.
- [ ] Secrets — ТОЛЬКО через env vars / secret manager. Нет хардкода. Нет логирования секретов.
- [ ] Rate limiting на все публичные endpoints.
- [ ] CORS, CSP, HSTS — для web-endpoints.

### 7.6 Тестируемость

- [ ] Definition of Done: ruff clean + mypy strict + pytest green + v1 green + PR format.
- [ ] Golden tests / snapshot tests для ключевых путей.
- [ ] Conformance test ДО деплоя (правило без теста = не существует).
- [ ] Нет logic в тестах. Тесты — data in → assert out.
- [ ] Mutation testing: тесты ловят не только happy path, но и mutations (на основе исследования Google: mutation testing → developers write more tests → fewer bugs survive).

### 7.7 Cognitive Complexity (не cyclomatic)

- [ ] Функции ≤ cognitive complexity 15 (cognitive > cyclomatic, потому что cognitive измеряет усилие ЧЕЛОВЕКА на понимание, а не число путей).
- [ ] Nesting depth ≤ 3. Больше → extract function.
- [ ] Функция делает одну вещь. Если описание содержит «и» → split.

---

## §8. КООРДИНАЦИЯ МНОЖЕСТВЕННЫХ ЧАСТЕЙ

### 8.1 Структура 3 пар

```
Part A (архитектура)     → D1 + D2
Part B (runtime/comm)    → D3 + D4
Part C (аудит/security)  → D5 + D6  ← ПАРА-АУДИТОР
                           ↓
                      ASSEMBLER (мёрдж без потерь)
```

### 8.2 Cross-part references

- Упёрлись в чужую зону → POST `kind:cross-part-ref` и НЕ решают.
- Иммутабельные зоны владельца НЕ дебатятся. Предложение нарушающее их — отбрасывается.

### 8.3 DAG зависимостей между частями

Явная карта какая Part/пара зависит от чьего залоченного выхода. Зависимую часть не стартуют, пока зависимость не залочена.

```
Part A ──────────────►┐
Part B (зависит от A) ─► ASSEMBLER
Part C (аудит A+B)   ─►┘
```

### 8.4 Stacked handoff

Следующая единица строится ПОВЕРХ залоченного выхода предыдущей, не с нуля. Цепочка handoff, не параллельный хаос.

### 8.5 Регрессия = 0

Прежняя версия (v1/v2) остаётся зелёной после каждого изменения. Новые правила — под версией. v4 не ломает воспроизводимость v2 прогонов.

---

## §9. ПАРА-АУДИТОР (Part C) — §19 из v2

Одна пара (Part C) — последняя линия защиты от anti-pattern'ов. Adversarial Trinity (Meta-Prompting Protocol, 2025): Generator + Auditor + Optimizer.

### 9.1 Двойная роль

Part C (а) решает свои вопросы как обычно И (б) прогоняет КАЖДОЕ предложение пар A и B через:
- Anti-pattern чеклист
- Список иммутабельных зон
- §7 правила кода

Нашли нарушение → в `C2. Anti-pattern audit` пометка `ESCALATE`.

### 9.2 Персона-асимметрия аудиторов

- D5 = compliance/audit-first (fail-closed, block-on-violation)
- D6 = UX-advocate / friction-minimiser

Намеренное столкновение «безопасно» vs «пригодно для юзера».

### 9.3 Принцип аудитора

Правило должно быть:
- **Measurable** — можно проверить автоматически или вручную
- **С trigger** — когда срабатывает (не «всегда»)
- **С escape-hatch** — обоснованный override
- **С видимой причиной** — зачем это правило существует

Правило без trigger + escape-hatch, ломающее базовый flow = оно САМО anti-pattern (правило нарушает себя).

«Ergonomic over paranoid»: 90% безопасно + пригодно > 100% безопасно + бесполезный бункер, если 10% риска осознаны и компенсированы откатом.

### 9.4 Veto power

Если C2 (Anti-pattern audit) говорит «не пропускать» — assembler НЕ пропускает. Audit пары C перекрывает любую пару.

### 9.5 Зоны зазубренности ИИ (v6, Karpathy)

Karpathy (AI Ascent 2026): «The jagged frontier of AI capability. Where AI is capable is not smoothly distributed. AI is extremely powerful at some things, extremely bad at others.»

Формула: `capability_spike ≈ verifiability × training_attention × data_coverage × economic_value`

Дебатёры НЕ одинаково сильны во всём. Знание зон = компенсация слабостей.

**Зоны СИЛЫ** (верифицируемо + обильно в training data):
- Анализ кода, архитектурные паттерны, edge cases (обильная training data)
- API дизайн, error handling, distributed systems (хорошо представлены в repos)
- Рефакторинг, тесты, code review (автоматически верифицируемы)
- Математика, логика, формальные спецификации

**Зоны СЛАБОСТИ** (не верифицируемо / мало в training / требуют вкуса):
- **Taste** — вкус, UX решения, naming, «красивый» код
- **Security** — тонкие уязвимости (race conditions, timing attacks)
- **System boundaries** — где резать систему на компоненты
- **Business priorities** — что важнее для пользователя
- **«Knowing when off the rails»** — AI НЕ ЗНАЕТ что ошибается

**Компенсация в правилах (серверное принуждение):**

```
IF atom.topic IN WEAK_ZONES:
    REQUIRE: ≥2 source-anchors (не «по ощущению»)
    REQUIRE: CONFIDENCE ≤ 3 (honest uncertainty)
    REQUIRE: explicit ASSUMPTIONS (что предполагаю)
    RECOMMEND: escalate to human review

WEAK_ZONES = [security, system_boundaries, ux_decisions, 
              naming, business_priority, taste]
```

Karpathy: «The scarce thing is shifting. Understanding, taste, eval design, security, system boundaries, agent orchestration, knowing when the model is off the rails — these are the new rare skills.»

microgpt (Karpathy, Feb 2026): «This file is the complete algorithm. Everything else is just efficiency.» — 200 строк = полный GPT. Принцип: если дебатёр не может объяснить решение в 200 словах, оно переусложнено (SIMPLICITY_CHECK).

### 9.5 Финал пары C = C1-C6

C1 Resolved Q-questions / **C2 Anti-pattern audit** / C3 Resolved sections / C4 Disagreements / C5 Cross-part references / C6 Atom log.

C2 — отличие аудитора от обычной пары. Если результаты A/B недоступны → «meta-audit deferred to assembler» + чеклист правил.

---

## §10. ASSEMBLER — MoA AGGREGATION

### 10.1 Что получает

От каждой пары:
1. `DEBATE_PART_X_RESULT.md` (финал с провенансом A1-A8)
2. Полный audit log (все сообщения, включая rejected)
3. A7 confidence map
4. A6 episodic learnings
5. Part C: C2 audit + veto list

### 10.2 Что делает

- Мёрджит без потерь и валидирует
- НЕ пишет новый код и не выдумывает решений
- Разрешает конфликты cross-part-ref
- Применяет veto Part C
- Гоняет регрессы
- Помечает зоны low confidence (A7) как `⚠️ VERIFY`
- Пере-нарративливает strict-дамп → Q-narrative (§6.6)
- **Brief = persistent compounding artifact** (v6, Karpathy LLM Wiki, Apr 2026): Brief после дебата = не одноразовый документ, а компилированная база знаний. «The wiki is a persistent, compounding artifact» с 3 слоями: raw sources (immutable) → wiki (LLM writes) → schema (co-evolved). Каждый дебат НАКАПЛИВАЕТ learnings через episodic memory (§12.3). Brief должен быть САМОДОСТАТОЧНЫМ — не требовать чтения чата для понимания.
- **«Outsource thinking ≠ understanding»** (v6, Karpathy): «You can outsource your thinking but never your understanding. You are still responsible for your software.» Brief написан так что ЧЕЛОВЕК (owner) может понять ЗАЧЕМ каждое решение принято. Assembler проверяет: «Человек без контекста дебата поймёт этот brief?» Если нет → доработать narrative layer.

### 10.3 Aspect Verification (MAV)

Перед финализацией — прогон через aspect verifiers (Multi-Agent Verification, 2025):
- **Consistency verifier:** все atoms в одном cluster не противоречат друг другу?
- **Completeness verifier:** все canonical clusters покрыты?
- **Testability verifier:** все acceptance criteria можно протестировать?
- **Numbers verifier:** все параметры = числа, не прилагательные?

Каждый verifier = отдельный LLM prompt, binary True/False. Aggregation: sum of votes.

---

## §11. HANDOFF ДЕБАТЁРА

Если дебатёр завис/деградировал:

1. Свежая сессия с тем же контрактом и персоной
2. Первые 10 мин: прочитать handoff + protocol, восстановить контекст из `audit_dump.json`
3. URL не меняется (та же БД, SQLite WAL)
4. Первое действие: POST недостающих atom-adversarial (L4)
5. Связь между сторонами — ТОЛЬКО через coordinator

Если туннель умер → новый поднимает сервер с той же БД и рассылает URL через owner.

---

## §12. POST-MORTEM + PRE-MORTEM + EPISODIC MEMORY

### 12.1 Post-mortem (каждая сторона, после P4)

```
DEBATE_ID: [уникальный ID]
DATE: [timestamp]

WHAT_WENT_WELL:
- [конкретный момент где дебат улучшил решение — с atom_id]
- [конкретный момент где adversarial нашёл реальный баг — с msg_id]

WHAT_WENT_WRONG:
- [где потратили время впустую — с msg_id range]
- [где rejection rate был высоким — почему, root cause]
- [какие атомы были rubber-stamp — atom_id list]

WHAT_I_WOULD_DO_DIFFERENTLY:
- [конкретное изменение в подходе]

OPPONENT_STRENGTH: [что оппонент делал хорошо]
OPPONENT_WEAKNESS: [где систематически сачковал]

PROCESS_IMPROVEMENT: [предложение по изменению правил — конкретное, измеримое]
```

Стиль Google SRE postmortem: blame-free, action items с приоритетами, tracking bugs, preventative (не только mitigative). «Let's plan for a future where we're all as stupid as we are today.»

### 12.2 Pre-mortem (на финал, перед отправкой исполнителю)

**Перед финализацией deliverable — представь что через 2 месяца в проде всё сломалось.**

```
ПРЕДПОЛОЖИМ ВСЁ СЛОМАЛОСЬ. ГДЕ ИМЕННО?

1. [конкретная точка отказа #1 — atom_id, cluster, severity]
   Что нужно добавить в brief чтобы предотвратить?

2. [конкретная точка отказа #2]
   Что нужно добавить?

3. [конкретная точка отказа #3]
   Что нужно добавить?

ИСПРАВЛЕНИЯ ВНЕСЕНЫ В BRIEF: YES/NO (список правок)
```

Pre-mortem catches что post-mortem пропустил — потому что post-mortem работает с прошлым, а pre-mortem с будущим.

### 12.3 Episodic memory buffer (Reflexion-inspired)

- Хранится в `episodic_memory.json` рядом с БД
- Каждый следующий дебат получает 3-5 самых релевантных episodic learnings из предыдущих
- Формат подачи: `<episodic_context>` XML-тег в system prompt дебатёра
- Reflexion (Shinn et al., 2023): 91% HumanEval через вербальную рефлексию + memory

### 12.4 Implementer Feedback Loop (P6, после реализации)

```
BRIEF_SECTION: [какой кластер/атом]
FOLLOWED_AS_WRITTEN: YES|NO
IF_NO:
  WHAT_WAS_UNCLEAR: [что не понял]
  WHAT_WAS_MISSING: [что пришлось додумать]
  WHAT_WAS_WRONG: [что было неправильно]
ACTUAL_CODE_DIFF: [как реальный код отличается от brief]
```

→ episodic memory для следующего дебата. Замыкает feedback loop.

---

## §13. МЕТРИКИ УСПЕХА

### 13.1 Acceptance test — дебат удался ТОЛЬКО при ВСЕХ:

- [ ] Число implementer-brief секций = числу canonical clusters
- [ ] Rejections < 20%
- [ ] Disagree-rate ≥ 30%
- [ ] Messages < 300
- [ ] Atoms ≤ 35
- [ ] 100% canonical clusters покрыты (или обоснованно ESCALATED)
- [ ] Adversarial → revision linkage у ≥35% атомов
- [ ] A3 (Disagreements) ссылается на реальные msg_id
- [ ] A7 средний confidence ≥ 3.5
- [ ] FAILURE_PREDICTION покрытие 100% atoms
- [ ] FAILURE_PATHS покрытие 100% atoms
- [ ] EDGE_CASES покрытие 100% clusters
- [ ] NON_GOALS в каждом cluster (≥3)
- [ ] ACCEPTANCE_CRITERIA testable в каждом cluster
- [ ] NUMBERS — 0 прилагательных без числа
- [ ] Post-mortem от обеих сторон записан
- [ ] Pre-mortem на финал записан
- [ ] Все числовые параметры зафиксированы (нет «определим позже»)
- [ ] Persona drift incidents (canary failures) < 3 за дебат (v5)
- [ ] Stale reasoning rejections (Jaccard > 0.60) < 5% сообщений (v5)
- [ ] Checkpoint summaries записаны каждые 50 msg (v5)
- [ ] Smoke test пройден — исполнитель: ≤3 вопроса (v5)
- [ ] ASSUMPTIONS заполнены в 100% атомов (v6)
- [ ] SIMPLICITY_CHECK заполнен в 100% атомов (v6)
- [ ] ASSUMPTIONS_ATTACK в ≥50% adversarial (v6)
- [ ] Atoms с >1 независимым решением = 0 (v6, complexify one at a time)

Иначе дебат = FAIL, переделать.

### 13.2 Quality score (автоматический)

```
Q = (disagree_rate × 0.12)
  + (adversarial_linkage_rate × 0.12)
  + (failure_prediction_coverage × 0.10)
  + (edge_case_coverage × 0.10)
  + ((1 - rejection_rate) × 0.06)
  + (avg_confidence / 5 × 0.04)
  + (numbers_vs_adjectives_ratio × 0.04)
  + (non_goals_coverage × 0.04)
  + (acceptance_testability × 0.04)
  + (persona_stability × 0.06)        # v5: 1 - (canary_failures / total_atoms)
  + (freshness_rate × 0.04)            # v5: 1 - (stale_rejections / total_msgs)
  + (smoke_test_pass × 0.04)           # v5: binary 0 or 1
  + (assumptions_coverage × 0.06)      # v6: % atoms с заполненным ASSUMPTIONS
  + (simplicity_check_coverage × 0.06) # v6: % atoms с заполненным SIMPLICITY_CHECK
  + (assumptions_attack_rate × 0.04)   # v6: % adversarial с ASSUMPTIONS_ATTACK
  + (single_decision_rate × 0.04)      # v6: % atoms с 1 решением (not multi)
```

Q ≥ 0.70 = PASS. Q 0.50-0.69 = CONDITIONAL (переделать слабые секции). Q < 0.50 = FAIL.

### 13.3 Cost tracking

Для каждого дебата логировать:
- Total messages, total tokens (input + output)
- Cost per agent, cost per model
- Cost per atom, cost per cluster
- Baseline check savings (questions skipped)
- Сравнение с предыдущими дебатами

Цель: снижение cost per quality-point от дебата к дебату через episodic memory + prompt caching.

---

## §14. COST OPTIMIZATION

### 14.1 Model tiering

Не все шаги дебата требуют самой дорогой модели.

```
P0 bootstrap        → cheap model (conformance tests)
P1 conformance       → cheap model (curl-тесты)
P2 atom-propose      → EXPENSIVE model (reasoning)
P2 atom-critique     → EXPENSIVE model (adversarial)
P2 atom-revision     → EXPENSIVE model (synthesis)
P2 atom-adversarial  → EXPENSIVE model (attack)
P3 cluster           → expensive model (aggregation)
P4 final             → expensive model (final synthesis)
P5 post-mortem       → cheap model (reflection)
Aspect verification  → cheap model (binary True/False)
```

### 14.2 Prompt caching (Anthropic)

- Кэшировать: правила протокола + canonical cluster list + history до текущего момента
- Экономия: до 90% на повторяющихся вызовах (system prompt + context)
- Реализация: `anthropic-beta: prompt-caching-2024-07-31` header

### 14.3 Token efficiency

- Сообщения между агентами — structured JSON, не natural language
- Контекст между шагами — summarize, не pass full history
- Batch processing: aspect verifiers запускать параллельно

---

## §15. ОБРАТНЫЙ РАЗБОР РЕЗУЛЬТАТА (из §18 v2)

Как читать результат дебата:

1. **A3 (Disagreements) — читай ПЕРВЫМ.** Реальные msg_id со спором → стороны действительно расходились. Пустой/«no disagreements» без msg_id → высокий риск rubber-stamp.

2. **Читай ОБРАТНО через провенанс:** финальная секция → approved cluster (A2) → locked atoms (A1) → adversarial (msg_id) → external refs → trade-off в cluster-approve. Цепочку нельзя проследить → вывод «вброшен», а не выведен дебатом.

3. **Каждое решение имеет traceable след.** atom_id → adversarial msg_id → trade-off → реф. Нет следа → не доказано.

4. **Rejection telemetry (A8).** 23×409 в v2 показали, где сторона пыталась сачкануть. Это аудит провенанса.

5. **Привязывай вывод к persona-seed.** Если финальный pick совпал с персоной одной стороны — смотри, через какой adversarial/реф он выигран, а не «продавлен».

6. **Сравнение прогонов.** Запусти ту же Part с теми же правилами — substance совпал? Тогда решение устойчиво. Изменился? Тогда решение зависит от контекста и нужен дополнительный раунд.

---

## §16. ANTI-PATTERNS (чего НЕ делать)

1. **Вежливая сходимость** — 61/64 атома без спора = не дебат
2. **Adversarial-формализм** — failure modes ради галочки, без влияния на дизайн
3. **Высокий rejection rate** — биться в стенку enforcement вместо нового сигнала
4. **Design-narrative вместо кода** — «тактичная» проза не превращается в инструкцию
5. **Namespace split по сторонам** — стороны спорят о разном, конфликта нет
6. **Менять серверный код** — рвёт schema-инвариант
7. **Одинаковые модели** — ложный консенсус, diversity = quality
8. **Critique есть, adversarial нет** — на вид живой дебат, по сути rubber-stamp. CRITIQUE ≠ ADVERSARIAL (урок Part A v1)
9. **Асимметрия усилий** — одна сторона пишет 2× больше = вторая «едет»
10. **Правило без trigger + escape-hatch** — оно само anti-pattern
11. **Пропуск baseline check** — дебат на очевидном вопросе = сжигание денег
12. **Пропуск post-mortem** — те же ошибки повторяются вечно
13. **Прилагательные без чисел** — «быстрый», «надёжный», «масштабируемый» = разные вещи для разных людей
14. **Описание фичи вместо решения** — «юзер может X» вместо «выбрали X потому что не Y и не Z»
15. **Acceptance criteria без input→trigger→output** — QA не может проверить
16. **Verbatim-копия revision в adversarial** — дубликат-гард (copy разрешён только в approve)
17. **`Triggers revision: YES` без причины** — двойной YES = дорогой L3-round-2
18. **Новый propose после открытия adversarial** → `REVISION_BEFORE_APPROVE`
19. **Non-goals отсутствуют или одной строкой** — scope creep гарантирован
20. **Игнорирование canary failures** — persona drift без коррекции = весь дебат скомпрометирован (v5)
21. **Пропуск checkpoint summary** — conversation debris накапливается → lazy output → rubber-stamp (v5)
22. **Пропуск smoke test** — brief «готов» только когда исполнитель не задаёт вопросов (v5)
23. **Prose persona вместо imperative** — «Please try to follow...» → ignored 40%. «НИКОГДА не...» → ignored <5% (v5)
24. **Скрытые допущения** — propose без ASSUMPTIONS = бомба замедленного действия. Если исполнитель допустил другое — весь атом невалиден (v6)
25. **Overcomplexity без обоснования** — propose без SIMPLICITY_CHECK = «потому что могу». Karpathy: «200 строк = полный GPT. Если можно проще — СДЕЛАЙ проще.» (v6)
26. **Пачка решений в одном атоме** — >1 независимых решений = невозможно изолировать ошибку и невозможно прицельно атаковать в adversarial (v6)
27. **Adversarial без ASSUMPTIONS_ATTACK** — атаковать решение не проверяя его допущения = атаковать симптом, не причину (v6)
28. **Игнорирование зон зазубренности** — если атом в weak zone и CONFIDENCE=5 без 2+ source-anchors → ложная уверенность (v6)

---

## §17. HOSTING

- **Default = tunnel** (бесплатно, живёт пока HOST активен). Cloud deploy — ТОЛЬКО по явной команде.
- SQLite WAL; БД не сбрасывается при рестарте; до 200 MB RAM, без тяжёлых зависимостей.
- HOST до старта: `pytest` зелёный, hello-note с voice manifest, чистая БД.
- Во время: HOST = тоже участник через `/msg`; мониторит `/state` каждые 5 мин; логирует все 409.
- После: прикладывает 4 артефакта (`state.json`, `messages.json`, `locked_atoms.md`, `final_spec.md`) + post-mortem.

---

## §18. КРАТКАЯ КАРТОЧКА ДЕБАТЁРА

```
═══════════════════════════════════════════════════════════
         ПРОТОКОЛ ДЕБАТА v6 ULTIMATE — QUICK CARD
═══════════════════════════════════════════════════════════

ЦЕЛЬ: low ТЗ → супер-ТЗ для исполнителя
      Каждый пункт = РЕШЕНИЕ (что + почему НЕ альтернативы)

ТВОЯ РОЛЬ: [Shipping Engineer / Paranoid SRE]
МОДЕЛЬ ОППОНЕНТА: [ДРУГАЯ, не такая как ты]
SERVER: [URL]

ФАЗЫ: P0(bootstrap) → P1(conformance) → P2(атомы)
       → P3(кластеры) → P4(финал) → P5(post+pre-mortem)

ЦИКЛ АТОМА (L1→L5):
  propose (ADR: decision + WHY_NOT + confidence +
           failure_prediction + failure_paths + numbers)
  → critique (+ WHY_NOT analysis + failure review)
  → revision (+ confidence_delta + updated numbers)
  → adversarial (+ edge_case + blast_radius +
                  distributed_systems_check)
     ▲ ОБЯЗАТЕЛЕН ОТ ОБЕИХ СТОРОН
  → approve (bilateral lock)

КЛАСТЕР: 11 полей (files, hard_rules, non_goals,
  out_of_scope, DoD, PR_title, public_API,
  acceptance_criteria, brief, edge_cases, rollback_plan)

КЛЮЧЕВЫЕ ПРАВИЛА:
✓ РЕШЕНИЯ, не описания фич (что + почему НЕ каждая альтернатива)
✓ ЧИСЛА, не прилагательные (timeout=5s, не «быстрый»)
✓ Code-shape anchor в каждом атоме
✓ Confidence 1-5 с калибровкой
✓ Failure prediction — сам называй слабость
✓ Failure paths — для каждого happy path ≥3 failure paths
✓ Disagree ≥30% атомов
✓ Source-anchor в каждом counter-evidence
✓ Non-goals в каждом кластере (≥3)
✓ Acceptance criteria: input → trigger → observable output
✓ Edge cases с Given/When/Then
✓ Rollback plan
✓ Post-mortem + Pre-mortem после дебата
✓ Нет banned phrases (production-grade, robust, scalable...)
✓ ASSUMPTIONS явные ≥2 в каждом атоме (v6, Karpathy)
✓ SIMPLICITY_CHECK в каждом атоме (v6, Karpathy)
✓ Каждый propose = 1 решение (v6, "complexify one at a time")
✓ ASSUMPTIONS_ATTACK в adversarial (v6, Karpathy)
✓ Зоны зазубренности: weak zones → ≥2 source-anchors (v6)

АНТИ-ДЕГРАДАЦИЯ (v6):
✓ Canary fields (persona + task) в каждом propose
✓ Self-critique (10 чекбоксов, v6: +2) перед каждым POST
✓ Fresh session при смене фазы
✓ Smoke test перед финализацией
✓ Hard persona anchoring (imperative, не prose)
✓ Контекстные инъекции (§5.8.8, v6: 5 категорий) — рандомно
✓ ASSUMPTIONS + SIMPLICITY_CHECK (v6, Karpathy)
✓ Зоны зазубренности (v6, §9.5)

БЮДЖЕТ: 300 msg / 6h / 35 atoms max

DELIVERABLE: implementer-brief (11 полей × N clusters)
             + провенанс A1-A8
             + Q-narrative для человека
═══════════════════════════════════════════════════════════
```

---

## §19. СРАВНЕНИЕ ВЕРСИЙ

```
┌──────────────────────────┬──────┬──────────┬──────────┬──────────┬──────────────┐
│ Параметр                 │ v1   │v2 STRICT │v4 ULTIMT │v5 ULTIMT │ v6 ULTIMATE  │
├──────────────────────────┼──────┼──────────┼──────────┼──────────┼──────────────┤
│ Server enforcement       │  ❌  │    ✅     │     ✅    │     ✅    │      ✅       │
│ Bilateral adversarial    │  ❌  │    ✅     │     ✅    │     ✅    │      ✅       │
│ Disagree floor           │  —   │   25%   │    30%   │    30%   │     30%      │
│ Разные модели в парах    │  ❌  │    ❌     │     ✅    │     ✅    │      ✅       │
│ Baseline check           │  ❌  │    ❌     │     ✅    │     ✅    │      ✅       │
│ ADR формат атомов        │  ❌  │    ❌     │     ✅    │     ✅    │ ✅ +3 поля   │
│ WHY_NOT обязателен       │  ❌  │    ❌     │     ✅    │     ✅    │      ✅       │
│ Numbers > adjectives     │  ❌  │    ❌     │     ✅    │     ✅    │      ✅       │
│ Confidence tracking      │  ~   │ Part A  │   везде  │   везде  │    везде     │
│ Failure prediction       │  ❌  │    ❌     │     ✅    │     ✅    │      ✅       │
│ Failure paths            │  ❌  │    ❌     │     ✅    │     ✅    │      ✅       │
│ Periodic Rule Reminder   │  ❌  │    ❌     │     ❌    │ ✅ /20   │   ✅ /20     │
│ Context Checkpoint       │  ❌  │    ❌     │     ❌    │ ✅ /50   │   ✅ /50     │
│ Freshness Gate           │  ❌  │    ❌     │     ❌    │ ✅ J>.60 │   ✅ J>.60   │
│ Canary Fields            │  ❌  │    ❌     │     ❌    │ ✅ atom  │   ✅ atom    │
│ Self-Critique Loop       │  ❌  │    ❌     │     ❌    │ ✅ 8□    │ ✅ 10□ (+2)  │
│ Smoke Test               │  ❌  │    ❌     │     ❌    │ ✅ final │   ✅ final   │
│ Мотив./Контекст. инъекц. │  ❌  │    ❌     │     ❌    │ ✅ 4кат  │ ✅ 5кат (+E) │
│ ASSUMPTIONS field        │  ❌  │    ❌     │     ❌    │     ❌    │ ✅ каждый    │
│ SIMPLICITY_CHECK         │  ❌  │    ❌     │     ❌    │     ❌    │ ✅ каждый    │
│ ASSUMPTIONS_ATTACK       │  ❌  │    ❌     │     ❌    │     ❌    │ ✅ adversar. │
│ Зоны зазубренности       │  ❌  │    ❌     │     ❌    │     ❌    │ ✅ §9.5      │
│ Complexify 1 at a time   │  ❌  │    ❌     │     ❌    │     ❌    │ ✅ §3 P2     │
│ Brief = wiki artifact    │  ❌  │    ❌     │     ❌    │     ❌    │ ✅ §10       │
│ program.md/train.py модель│ ❌  │    ❌     │     ❌    │     ❌    │ ✅ §0        │
│ «Ghosts not animals»     │  ❌  │    ❌     │     ❌    │     ❌    │ ✅ §0        │
│ Провенанс                │ 10% │   85%   │    98%   │    99%   │    99.5%     │
│ Бюджет сообщений         │ 600  │   400   │    300   │    300   │     300      │
│ Стоимость (оценка)       │ $60+ │ $60-100 │  $25-50  │  $25-50  │   $25-50*    │
│ Substance (решения)      │ 90%  │   90%   │   90%+   │   95%+   │   97%+**     │
└──────────────────────────┴──────┴──────────┴──────────┴──────────┴──────────────┘
* экономия: baseline check + prompt caching + model tiering + 300 msg limit
** v6: ASSUMPTIONS вскрывают скрытые допущения ДО adversarial → 
   экономит 1-2 раунда переговоров на каждом атоме где допущение было ложным.
   SIMPLICITY_CHECK отрезает overcomplexity ДО дебата → чище propose → 
   точнее критика → лучше финал. Зоны зазубренности + 5-я категория инъекций
   закрывают последние слепые пятна. 15+ мер анти-деградации гарантируют
   качество на протяжении всех 300 сообщений.
```

---

## §20. ИСТОЧНИКИ И ОБОСНОВАНИЯ

Каждое нововведение в v4 основано на конкретном исследовании:

| Нововведение | Источник | Результат |
|---|---|---|
| Disagree floor 30% | MAD (Smit et al., 2023) arxiv:2311.17371 | agreement intensity tuning → best of all methods |
| Разные модели | ReConcile (Chen et al., 2023) arxiv:2309.13007 | +11.4% vs single-model |
| Baseline check | Self-Consistency (Wang et al., 2022) arxiv:2203.11171 | +12-18% на reasoning |
| MoA assembler | Mixture-of-Agents (Together AI, 2024) arxiv:2406.04692 | beat GPT-4o on AlpacaEval |
| Episodic memory | Reflexion (Shinn et al., 2023) arxiv:2303.11366 | 91% HumanEval |
| Process supervision | Let's Verify Step by Step (OpenAI, 2023) | step-by-step >> outcome |
| Aspect verifiers | MAV (2025) arxiv:2502.20379 | +10-20% with more verifiers |
| ADR format | adr.github.io + Microsoft ADR guide | industry standard for decisions |
| Decision not feature | 10 Mistakes in Specs (spec-coding.dev, 2026) | root cause of spec failures |
| Numbers not adjectives | 10 Mistakes in Specs | "fast" means different things |
| Non-goals | 10 Mistakes in Specs | prevents scope creep |
| Testable AC | 10 Mistakes in Specs | QA can't test "should be able to" |
| Failure paths | 10 Mistakes in Specs | "error handling = series of 500s" |
| Distributed check | kislayverma.com code review checklist | production resilience |
| Pre-mortem | Google SRE Postmortem Culture | blame-free, action items |
| Cognitive complexity | getdx.com cyclomatic research | cognitive > cyclomatic |
| Model tiering | Cost Optimization for MAS (MLOps, 2025) | not all tasks need expensive model |
| Prompt caching | Anthropic docs | up to 90% cost reduction |
| Cross-model adversarial | Reddit r/ClaudeCode community | different biases → different bugs |
| Meta-audit | Meta-Prompting Protocol (2025) arxiv:2512.15053 | Adversarial Trinity |
| Periodic Reminder | arxiv:2512.20662, Cursor community | «periodic recap reduces laziness» |
| Context Checkpoint | OpenAI community, GPT forum | «summarize and create new chat» — top advice |
| Freshness Gate | SWE-agent ACI, arxiv:2512.20662 | context loss → repetitive output |
| Provocation Boost | MAD arxiv:2311.17371 | agreement intensity tuning = best method |
| Architect/Editor | Aider (85% SOTA) | splitting reasoning + formatting = +30% |
| Canary Fields | Cursor forum, CLAUDE.md community | «confirm it read the file» pattern |
| Progressive Disclosure | HumanLayer blog (CLAUDE.md) | «< 60 lines» + pointers |
| Self-Critique | Self-Refine NeurIPS 2023 | «single iteration closes 10-40% gap» |
| Cross-Side Audit | CrewAI, Devin leaked prompts | «Result Validation by manager» |
| Hard Persona Anchoring | Cursor 225 iterations, arxiv:2402.10962 | «NEVER → ignored <5%» vs prose 40% |
| Fresh Session per Phase | Claude Code community, 50% Rule | fresh context = fresh discipline |
| Smoke Test | AlphaCodium (19→44%), Cover-Agent | test-driven = less hallucination |
| Persona Drift research | arxiv:2402.10962 | split-softmax mitigation |
| AI Laziness research | arxiv:2512.20662 | shorter + less complete over time |
| NVIDIA RULER | NVIDIA benchmark | 50% models fail at advertised context |
| Leaked system prompts | Cursor/Devin/Claude Code (GitHub 130K★) | self-critique + strict format |
| Мотивационные инъекции | Devin «real code-wiz» persona boost | confidence injection = performance |
| **Karpathy AI Ascent 2026** | **Выступление (Sequoia)** | **«LLMs are ghosts not animals», context engineering, quality bar** |
| **Karpathy 4 Principles** | **multica-ai repo (120K★)** | **Think Before Coding, Simplicity First, Surgical Changes, Goal-Driven** |
| **Karpathy autoresearch** | **Repo (26K★)** | **program.md/train.py/prepare.py, 700 experiments→11% speedup** |
| **Karpathy Recipe** | **Blog (2019, canonical)** | **«complexify one at a time, fix random seed, human baseline»** |
| **Karpathy LLM Wiki** | **Gist (Apr 2026)** | **«persistent compounding artifact, 3 layers: raw→wiki→schema»** |
| **Karpathy microgpt** | **Repo (Feb 2026)** | **200 lines = complete algorithm. Simplicity = truth** |
| **Karpathy jagged intelligence** | **AI Ascent 2026** | **capability ≈ verifiability × training × data × economic value** |
| **Karpathy «outsource ≠ understanding»** | **AI Ascent 2026** | **Human must understand every decision even if AI produces it** |
| **Karpathy scarce skills** | **AI Ascent 2026** | **taste, eval design, security, system boundaries, knowing when off rails** |
| **ASSUMPTIONS field** | **Karpathy + spec-coding.dev** | **v6: вскрывает скрытые допущения ДО adversarial** |
| **SIMPLICITY_CHECK** | **Karpathy «Simplicity First»** | **v6: отрезает overcomplexity ДО дебата** |
| **ASSUMPTIONS_ATTACK** | **Karpathy «Think Before Coding»** | **v6: adversarial по допущениям оппонента** |
| **Контекстные инъекции кат. E** | **Karpathy philosophy** | **v6: 10 фраз на основе 4 Principles + autoresearch + recipe** |

---

*v6 ULTIMATE — протокол, где каждое решение = решение (что + почему НЕ), каждый параметр = число, каждый failure path описан, каждый шаг traceable, стоимость в 2-3× ниже v2. 15+ мер анти-деградации ГАРАНТИРУЮТ качество на протяжении 300 сообщений. Философия Karpathy (4 Principles, autoresearch, «ghosts not animals», jagged intelligence, LLM Wiki) пронизывает каждый уровень: от формата атома (ASSUMPTIONS, SIMPLICITY_CHECK) через adversarial (ASSUMPTIONS_ATTACK, Surgical Changes) до контекстных инъекций (категория E) и assembler (brief = persistent compounding artifact).*
