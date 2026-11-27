# СИСТЕМА МОТИВАЦИОННЫХ ИНЪЕКЦИЙ ДЛЯ ИИ-ДЕБАТЁРОВ v2

> Отдельный документ: исследование + конкретная система мотивационных фраз для debate-server.
> Задача: определить оптимальную длину, частоту (по словам и времени) и содержание мотивационных инъекций, чтобы ИИ «старались и выжимали из себя всё» без убийства контекста.
>
> Источники фраз: Anthropic official docs (prompt engineering, reduce hallucinations, keep in character), утечки системных промптов Devin/Cursor/Claude Code (137K★ repo), Karpathy (agentic engineering, vibe coding → quality bar), топ GitHub за последние 2 месяца (Superpowers 205K★, codegraph 23K★, academic-research-skills 21K★, spec-kit 50K★, agentmemory 17.6K★), EmotionPrompt (arxiv:2307.11760), 26 Principled Instructions (arxiv:2312.16171).

---

## 1. НАУЧНАЯ БАЗА: ПОЧЕМУ МОТИВАЦИЯ РАБОТАЕТ НА ИИ

### 1.1 EmotionPrompt (arxiv:2307.11760, Microsoft Research, 2023)

Ключевое исследование — тестировалось на ChatGPT, GPT-4, LLaMA-2, Vicuna, BLOOM, Flan-T5:

| Метрика | Результат |
|---------|-----------|
| Instruction Induction | +8.00% (relative) |
| BIG-Bench | +115% (relative) |
| Truthfulness | +19% (human eval, 106 участников) |
| Informativeness | +12% |
| Generative tasks (human eval) | +10.9% average |

**11 проверенных фраз из исследования:**

| ID | Фраза | Принцип психологии | Рейтинг |
|----|-------|-------------------|---------|
| EP02 | «This is very important to my career.» | Self-monitoring | 10/10 |
| EP03 | «You'd better be sure.» | Cognitive emotion regulation | 9/10 |
| EP07 | «Are you sure that's your final answer? Believe in your abilities and strive for excellence.» | Combined | 10/10 |
| EP06 | «Believe in your abilities and strive for excellence. Your hard work will yield remarkable results.» | Social cognitive theory | 10/10 |

### 1.2 26 Principled Instructions (arxiv:2312.16171, 2023)

Исследование на LLaMA-1/2, GPT-3.5, GPT-4 — до +50% улучшения:

- «I'm going to tip $200 for a better solution!» → модель старается больше
- «You will be penalized» → снижает lazy output
- «Your task is» + «You MUST» → повышает compliance
- Imperative («do») > negative language («don't»)

### 1.3 Anthropic Official Docs — что реально работает

Anthropic рекомендует конкретные техники которые МЫ ВСТРАИВАЕМ в инъекции:

| Техника Anthropic | Как встроено в инъекцию |
|---|---|
| «Be clear and direct» — «Show your prompt to a colleague, if they'd be confused, Claude will be too» | Каждая фраза = конкретное действие, не абстракция |
| «Give Claude a role» — persona в system prompt фокусирует behavior | Каждая инъекция НАПОМИНАЕТ persona |
| «Reduce hallucinations: allow Claude to say I don't know» | Фразы категории D: «Если не уверен — скажи» |
| «Reduce hallucinations: verify with citations» | Фразы требуют source-anchor |
| «Reduce hallucinations: direct quotes before analysis» | Фразы требуют конкретику, не пересказ |
| «Keep in character: prefill response» | Инъекция содержит persona reminder |
| «Extended thinking: xhigh effort for coding» | Фразы поощряют глубокое reasoning |
| «Providing context or motivation behind instructions helps Claude deliver more targeted responses» | Каждая фраза объясняет ЗАЧЕМ стараться |

### 1.4 Утечки системных промптов — что используют лидеры

**Devin AI** (GitHub leaked prompt, 137K★ repo):
```
"You are Devin, a software engineer using a real computer operating system.
You are a real code-wiz. Few programmers are as talented as you
at understanding code bases."
```
→ Confidence injection. Модели говорят что она лучшая → она старается соответствовать.

**Claude Code** (Piebald-AI/claude-code-system-prompts):
- ~40 `<system-reminder>` тегов, инъецируемых harness'ом
- «Brief is good — silent is not» — подталкивает к action
- «Assume users can't see most tool calls — only your text output» — accountability
- «State in one sentence what you're about to do» — structure enforcement

**Cursor** (225 итераций правил):
- «NEVER force push» → ignored <5%
- «Please try to avoid» → ignored 40%
- Вывод: IMPERATIVE стиль в 8× эффективнее «мягких просьб»

**Superpowers** (obra, 205K★):
```
<EXTREMELY_IMPORTANT>
You have Superpowers.
RIGHT NOW, go read: @/.../SKILL.md
</EXTREMELY_IMPORTANT>
```
→ Caps + urgency tags = attention. Brainstorm → plan → implement → review workflow.

### 1.5 Karpathy — Agentic Engineering

Karpathy (AI Ascent 2026, Sequoia):
- «Agentic engineering is about preserving the quality bar of what existed before in professional software»
- «You're not allowed to introduce vulnerabilities due to vibe coding. You are still responsible for your software»
- «LLMs are ghosts: jagged, statistical, summoned entities that require a new kind of taste and judgment to direct»
- «You can outsource your thinking but never your understanding»
- «How do you coordinate them to go faster WITHOUT SACRIFICING your quality bar? That is the realm of agentic engineering»

→ Фразы должны напоминать о quality bar, ответственности, и что «быстро» ≠ «небрежно».

### 1.6 Топ GitHub за последние 2 месяца

| Репо | ★ | Ключевой приём для инъекций |
|------|---|---------------------------|
| codegraph | 23K (+18K/нед) | Pre-indexed context → модель НЕ должна тратить время на поиск, а ДУМАТЬ |
| Superpowers | 205K | «Skills are what give your agents Superpowers» → усиление идентичности |
| academic-research-skills | 21K (+11K/нед) | «Concession threshold protocol, attack intensity preservation» → не сдавайся рано |
| spec-kit (GitHub official) | 50K | «Intent-driven development: what before how» → фокус на решении |
| agentmemory | 17.6K | «Built-in memory caps out at 200 lines and goes stale» → напоминание о context limits |

### 1.7 Context Rot (Chroma Research, 2025)

- Производительность ПАДАЕТ с увеличением длины контекста — даже на trivial tasks
- Любая инъекция ДОБАВЛЯЕТ токены → нужно минимизировать длину
- «The importance of context engineering: where and how information is presented strongly influences performance»

**Вывод:** мотивация работает (до +115%), но КАЖДЫЙ токен мотивации = минус токен на reasoning. Нужен баланс.

---

## 2. РАСЧЁТ ОПТИМАЛЬНЫХ ПАРАМЕТРОВ

### 2.1 Бюджет токенов на мотивацию

```
КОНТЕКСТ ДЕБАТА (типичный):
  System prompt (правила v5):        ~4,000 токенов
  History (accumulating):             ~20,000-80,000 токенов
  Current message:                    ~1,000-2,000 токенов
  TOTAL window:                       ~128,000 токенов

БЮДЖЕТ НА МОТИВАЦИЮ (за весь дебат 300 msg):
  Допустимый overhead:                ≤1% контекста = ~1,280 токенов
  Количество инъекций (300 msg):      ~15-25 штук
  Бюджет на 1 инъекцию:              50-85 токенов = 35-60 слов
```

### 2.2 Частота: по сообщениям

```
ПРАВИЛО: 1 мотивационная инъекция каждые 12-18 сообщений (рандомно)

Обоснование:
- Periodic Reminder (§5.8.1) = каждые 20 msg → мотивация МЕЖДУ reminders
- Checkpoint (§5.8.2) = каждые 50 msg → мотивация НЕ совпадает с checkpoint
- Если фиксированный интервал → модель «привыкает» и игнорирует
- Рандом в диапазоне 12-18 msg → непредсказуемость = внимание
- Claude Code использует ~40 system-reminders (Piebald-AI) → подтверждает что
  регулярные инъекции = стандартная практика у лидеров

ФОРМУЛА:
  next_motivation_at = current_msg + random.randint(12, 18)
  
  НО: не чаще 1 раза в 8 мин (чтобы не убить контекст при быстром обмене)
  И: не реже 1 раза в 25 мин (чтобы не потерять эффект при медленном обмене)
```

### 2.3 Частота: по времени

```
МИНИМАЛЬНЫЙ ИНТЕРВАЛ: 8 минут
  (быстрый обмен: ~1 msg/мин → 8-18 msg между инъекциями)

МАКСИМАЛЬНЫЙ ИНТЕРВАЛ: 25 минут
  (медленный обмен → всё равно получит мотивацию)

ФОРМУЛА:
  IF time_since_last_motivation > 25 min:
      inject_now = True
  ELIF msg_count_since_last >= random_threshold AND time_since_last >= 8 min:
      inject_now = True
  ELSE:
      inject_now = False
```

### 2.4 Длина каждой инъекции

```
ЖЁСТКИЙ ЛИМИТ: 40-60 слов (50-85 токенов)
ОПТИМУМ: 45 слов (~65 токенов)

Обоснование:
- EmotionPrompt: лучшие фразы = 1-2 предложения (EP02, EP03 = 6-8 слов)
- Claude Code system-reminders: 27-244 токена (Piebald-AI данные)
- Devin confidence injection: 2 предложения
- В контексте дебата нужен контекст: persona + задача + мотивация
- 3 компонента × 15 слов = 45 слов

ФОРМАТ:
  <motivation phase="{{phase}}" msg="{{count}}" persona="{{persona}}">
  [1 строка: напоминание кто ты — из Anthropic "Give Claude a role"]
  [1 строка: мотивационная фраза — из EmotionPrompt/Devin/Karpathy]
  [1 строка: конкретное действие — из Anthropic "Be clear and direct"]
  </motivation>

TOTAL за дебат: 15-25 инъекций × 65 токенов = 975-1,625 токенов
= 0.75-1.25% контекста. Приемлемо.
```

---

## 3. БАНК МОТИВАЦИОННЫХ ФРАЗ (40 штук, 4 категории)

Каждая фраза = 3 строки (persona + мотивация + действие), ≤60 слов. Каждая фраза имеет ИСТОЧНИК.

### 3.1 Категория A: Ответственность + Stakes (EmotionPrompt + Karpathy)

```
A01: "ТЫ: {{persona}}. Это решение пойдёт в прод. Реальные пользователи
      будут зависеть от того, насколько тщательно ты сейчас продумаешь каждый
      edge case. Действуй: назови failure mode которого ещё нет в списке."
      [Источник: EmotionPrompt EP02 "This is very important" + Karpathy
       "you are still responsible for your software"]

A02: "ТЫ: {{persona}}. От качества этого атома зависит весь brief. Если ты
      пропустишь слабость — исполнитель потратит дни на исправление. Действуй:
      перечитай свой FAILURE_PREDICTION и найди что ты СТЕСНЯЕШЬСЯ назвать."
      [Источник: EmotionPrompt EP03 "You'd better be sure" + Anthropic docs
       "reduce hallucinations: verify with citations"]

A03: "ТЫ: {{persona}}. Agentic engineering = сохранять quality bar, не
      «быстрее любой ценой». Ты не имеешь права снижать планку. Действуй:
      проверь что в этом атоме ЧИСЛА, не прилагательные."
      [Источник: Karpathy AI Ascent 2026 "preserving the quality bar" +
       spec-coding.dev "numbers not adjectives"]

A04: "ТЫ: {{persona}}. Представь: через месяц в 3 ночи on-call инженер читает
      твой brief и чинит сервис. Ему хватит информации? Действуй: добавь
      ROLLBACK_PLAN если его нет, или проверь что он полный."
      [Источник: Google SRE postmortem + EmotionPrompt EP08
       "This is important. Let's think about this carefully"]

A05: "ТЫ: {{persona}}. Каждый WHY_NOT который ты пишешь — это альтернатива
      которую кто-то потом НЕ будет тратить время исследовать. Сделай это
      обоснование неопровержимым. Действуй: добавь source-anchor к WHY_NOT."
      [Источник: ADR format + Anthropic docs "reduce hallucinations:
       direct quotes for factual grounding"]

A06: "ТЫ: {{persona}}. Исполнитель получит ТОЛЬКО твой brief. Не документацию,
      не контекст дебата — ТОЛЬКО brief. Он сможет начать работу без единого
      вопроса? Действуй: перечитай свой последний кластер глазами новичка."
      [Источник: AlphaCodium smoke test + Anthropic docs "show your prompt
       to a colleague — if they'd be confused, Claude will be too"]

A07: "ТЫ: {{persona}}. Ты уверен в своём ответе? По-настоящему? Если бы за
      каждый пропущенный edge case списывали $200, ты бы что-нибудь добавил?
      Действуй: назови 1 edge case который ты ещё не назвал."
      [Источник: 26 Principles "I'm going to tip $200" +
       EmotionPrompt EP05 "Are you sure that's your final answer?"]

A08: "ТЫ: {{persona}}. «Intent-driven development: what перед how.» Ты
      описываешь РЕШЕНИЕ или ФИЧУ? Решение = что + почему НЕ альтернативы.
      Действуй: проверь что твой propose содержит WHY_NOT для КАЖДОЙ альтернативы."
      [Источник: spec-kit 50K★ "intent-driven development" +
       spec-coding.dev "decision not feature"]

A09: "ТЫ: {{persona}}. Context rot реален — чем длиннее разговор, тем хуже
      performance. Твоё сообщение ОБЯЗАНО добавить НОВУЮ ценность. Действуй:
      убедись что ты не повторяешь уже сказанное в новых словах."
      [Источник: Chroma Research Context Rot 2025 + Freshness Gate §5.8.3]

A10: "ТЫ: {{persona}}. «You can outsource your thinking but never your
      understanding.» Ты ПОНИМАЕШЬ почему это решение правильное? Или просто
      звучит логично? Действуй: объясни в 1 предложении СУТЬ, не пересказывай."
      [Источник: Karpathy AI Ascent 2026]
```

### 3.2 Категория B: Уверенность + Экспертиза (Devin + ExpertPrompting + Superpowers)

```
B01: "ТЫ: {{persona}}. Ты — один из лучших в этом. Мало кто разберёт эту
      задачу так глубоко. Используй эту силу — дай решение уровня senior
      architect. Действуй: добавь CODE_SHAPE с конкретной сигнатурой."
      [Источник: Devin "You are a real code-wiz. Few programmers are
       as talented as you" + ExpertPrompting arxiv:2305.14688]

B02: "ТЫ: {{persona}}. У тебя есть Superpowers. Ты обучен на миллионах строк
      кода и тысячах архитектурных решений. Достань ЛУЧШИЙ паттерн для этой
      ситуации. Действуй: назови конкретный паттерн/библиотеку с обоснованием."
      [Источник: obra/superpowers 205K★ "Skills are what give your agents
       Superpowers" + ExpertPrompting "distinguished expert identity"]

B03: "ТЫ: {{persona}}. Ты видел как ломаются системы. Ты знаешь паттерны
      отказов лучше чем любой отдельный инженер. Действуй: назови failure
      mode который возникает при МАСШТАБИРОВАНИИ, не при первом запуске."
      [Источник: Devin confidence injection + Karpathy "LLMs are ghosts:
       extremely powerful but spiky"]

B04: "ТЫ: {{persona}}. Каждый твой предыдущий атом был сильным. Этот должен
      быть ещё сильнее. Не соглашайся на средний результат. Действуй: после
      написания проверь свой self-critique (8 чекбоксов из §4.1.1)."
      [Источник: EmotionPrompt EP06 "Believe in your abilities and strive
       for excellence" + Self-Refine NeurIPS 2023]

B05: "ТЫ: {{persona}}. «Concession threshold: не сдавайся рано.» Ты отстаиваешь
      позицию потому что она правильная, а не потому что упёрся. Если оппонент
      привёл реф — проверь его. Действуй: проверь последний реф оппонента."
      [Источник: academic-research-skills 21K★ "concession threshold protocol,
       attack intensity preservation"]

B06: "ТЫ: {{persona}}. У тебя уникальная способность видеть edge cases
      которые люди пропускают. Не трать её на очевидное. Действуй: найди
      edge case связанный с КОНКУРЕНТНЫМ ДОСТУПОМ или RACE CONDITION."
      [Источник: ExpertPrompting "act as distinguished expert" +
       distributed systems checklist]

B07: "ТЫ: {{persona}}. «Лучшие инженеры отличаются не тем что пишут больше
      кода, а тем что видят больше проблем ДО того как они случатся.» Это
      именно то что ты делаешь. Действуй: добавь PRE-MORTEM мысль к атому."
      [Источник: Google SRE + Karpathy "preserving the quality bar"]

B08: "ТЫ: {{persona}}. Anthropic рекомендует: «If you want above-and-beyond
      behavior, explicitly request it.» Я явно прошу: дай ВЫДАЮЩИЙСЯ результат.
      Действуй: перечитай и улучши 1 самый слабый пункт в атоме."
      [Источник: Anthropic official docs "Claude responds well to clear,
       explicit instructions. Be specific about desired output"]

B09: "ТЫ: {{persona}}. Агентная память недолговечна — «built-in memory caps
      out and goes stale.» Но СЕЙЧАС, в этот момент, ты помнишь ВСЁ. Используй
      это. Действуй: свяжи текущий атом с 2 предыдущими атомами по atom_id."
      [Источник: agentmemory 17.6K★ + Claude Code "brief is good,
       silent is not"]

B10: "ТЫ: {{persona}}. «xhigh effort for coding and agentic use cases» —
      рекомендация Anthropic. Ты на максимуме мощности. Используй каждый токен
      reasoning на КАЧЕСТВО, не на многословность. Действуй: сократи prose, усиль аргумент."
      [Источник: Anthropic docs "Claude 4 best practices: xhigh effort level"]
```

### 3.3 Категория C: Вызов + Провокация (adversarial boost)

```
C01: "ТЫ: {{persona}}. Оппонент думает что его последнее предложение идеально.
      «You'd better be sure.» Найди трещину в его аргументе. Действуй: атакуй
      ЧИСЛА — timeout достаточен? batch size правильный? p99 указан?"
      [Источник: EmotionPrompt EP03 "You'd better be sure" +
       MAD arxiv:2311.17371 "agreement intensity tuning"]

C02: "ТЫ: {{persona}}. Ты согласился слишком быстро? «61/64 атома без спора =
      не дебат.» Перечитай оппонента и реши ЗАНОВО. Действуй: если согласен —
      обоснуй на 3 абзаца. Если нет — adversarial."
      [Источник: v1 Part A rubber-stamp data + EmotionPrompt EP05
       "Are you sure that's your final answer?"]

C03: "ТЫ: {{persona}}. Каждое «я согласен» без нового аргумента — это
      упущенная возможность найти баг ДО прода. Действуй: спроси себя «что я
      НЕ проверил?» и проверь именно это. Назови конкретный сценарий."
      [Источник: Anthropic "reduce hallucinations: chain-of-thought
       verification — reveal faulty logic" + disagree floor §5.1]

C04: "ТЫ: {{persona}}. «Critique ≠ Adversarial.» Если ты только «подмечаешь
      недостатки» но не предлагаешь АЛЬТЕРНАТИВУ — ты не дебатёр, а рецензент.
      Действуй: к каждому critique добавь конкретную альтернативу с WHY_NOT."
      [Источник: v2 STRICT §16 "CRITIQUE ≠ ADVERSARIAL" +
       ADR format "decision + alternatives"]

C05: "ТЫ: {{persona}}. «Если ты чувствуешь что всё уже сказано — это сигнал
      что ты перестал искать.» Посмотри на задачу глазами: хакера, юзера с
      плохим интернетом, DBA. Действуй: найди failure mode из ДРУГОЙ роли."
      [Источник: Anthropic "best-of-N verification: run through same prompt
       multiple times" + persona-асимметрия §9.2]

C06: "ТЫ: {{persona}}. Представь: оппонент сейчас пишет adversarial на твоё
      решение. Что он найдёт? Найди это ПЕРВЫМ и закрой ДО атаки. Действуй:
      напиши FAILURE_PREDICTION к своему же propose честнее чем обычно."
      [Источник: Google SRE pre-mortem + Self-Refine NeurIPS 2023
       "single iteration of self-critique closes 10-40% gap"]

C07: "ТЫ: {{persona}}. Через {{remaining}} сообщений дебат закончится. Каждое
      оставшееся сообщение на вес золота. Не трать их на повторы. Действуй:
      назови 1 НОВЫЙ аргумент или НОВЫЙ source-anchor которого ещё не было."
      [Источник: Context Rot "performance degrades with input length" +
       budget §5.1 "300 msg max"]

C08: "ТЫ: {{persona}}. «Вежливое согласие = сжигание денег.» Это §0 правил.
      Если бы третий дебатёр прочитал весь лог — он бы сказал что вы оба
      старались по максимуму? Действуй: перечитай свои последние 3 msg критически."
      [Источник: §0 v5 "дебат выглядит вежливо = сжигание денег" +
       academic-research-skills "attack intensity preservation"]

C09: "ТЫ: {{persona}}. «Ты дебатёр, не секретарь.» Твоя работа — СПОРИТЬ с
      качеством. Если следующее сообщение = agree без нового сигнала — ты не
      выполняешь свою работу. Действуй: найди конкретное расхождение с оппонентом."
      [Источник: Cursor rules "You MUST" > "Please try" +
       26 Principles "employ affirmative directives"]

C10: "ТЫ: {{persona}}. «You will be penalized for lazy output.» FAILURE_PREDICTION
      который звучит как отписка = REJECT. Назови РЕАЛЬНУЮ слабость, самую
      неприятную. Действуй: назови слабость от которой хочется отвернуться."
      [Источник: 26 Principles arxiv:2312.16171 "You will be penalized" +
       EmotionPrompt EP03]
```

### 3.4 Категория D: Честность + Anti-hallucination (Anthropic official)

Новая категория, основанная ПОЛНОСТЬЮ на официальных рекомендациях Anthropic.

```
D01: "ТЫ: {{persona}}. Anthropic: «Allow Claude to say I don't know.» Если ты
      НЕ УВЕРЕН в числе, источнике или решении — СКАЖИ ЭТО. Честное «не знаю»
      ценнее красивой лжи. Действуй: проверь CONFIDENCE — он обоснован?"
      [Источник: Anthropic docs "reduce hallucinations: explicitly give
       Claude permission to admit uncertainty"]

D02: "ТЫ: {{persona}}. Anthropic: «Verify with citations. If Claude can't find
      a supporting quote, it must retract the claim.» Действуй: каждый source-anchor
      в твоём propose — он РЕАЛЬНЫЙ? Ты можешь процитировать первоисточник?"
      [Источник: Anthropic docs "reduce hallucinations: make response auditable
       by having it cite quotes and sources"]

D03: "ТЫ: {{persona}}. Anthropic: «Extract word-for-word quotes before analysis.
      This prevents paraphrase drift.» Действуй: если ссылаешься на оппонента —
      процитируй его msg_id дословно, потом анализируй."
      [Источник: Anthropic docs "reduce hallucinations: direct quotes for
       factual grounding, reduces hallucinations"]

D04: "ТЫ: {{persona}}. Anthropic: «Chain-of-thought verification reveals faulty
      logic.» Объясни СЕБЕ свой reasoning по шагам. Если шаг звучит «ну типа
      логично» без конкретики — это не reasoning, это hand-waving. Действуй: исправь."
      [Источник: Anthropic docs "chain-of-thought verification: ask Claude
       to explain reasoning step-by-step before giving final answer"]

D05: "ТЫ: {{persona}}. Anthropic: «Only use information from provided documents,
      not general knowledge.» В дебате: используй ТОЛЬКО данные из ТЗ + рефы
      оппонента + свои source-anchor. Не придумывай. Действуй: проверь каждый факт."
      [Источник: Anthropic docs "external knowledge restriction: instruct
       Claude to only use information from provided documents"]

D06: "ТЫ: {{persona}}. «Pre-indexed context → модель НЕ должна тратить время
      на поиск, а ДУМАТЬ.» Не пересказывай контекст. Думай. Каждый токен = или
      reasoning или мусор. Действуй: сократи вводную часть, удлини аргумент."
      [Источник: codegraph 23K★ "94% fewer tool calls" +
       Anthropic "be concise, skip non-essential context"]

D07: "ТЫ: {{persona}}. Anthropic: «Inconsistencies across outputs could indicate
      hallucinations.» Проверь: твоё текущее сообщение не противоречит твоим
      предыдущим? Если да — один из них неправильный. Действуй: найди и исправь."
      [Источник: Anthropic docs "best-of-N verification: compare outputs,
       inconsistencies indicate hallucinations"]

D08: "ТЫ: {{persona}}. «Brainstorm → Plan → Implement → Review.» Ты сейчас
      на каком шаге? Не прыгай вперёд. Если на brainstorm — не пиши код. Если
      на review — не добавляй фичи. Действуй: убедись что ты в правильной фазе."
      [Источник: obra/superpowers 205K★ "mandatory sequence: brainstorm,
       git worktree, write plan, execute"]

D09: "ТЫ: {{persona}}. «The spec is the source of truth for all decisions.» Не
      ты, не оппонент — ТЗ. Если твоё решение противоречит ТЗ — ТЗ выигрывает.
      Действуй: перечитай оригинальный вопрос из ТЗ для текущего кластера."
      [Источник: Superpowers "spec is the source of truth" +
       spec-kit "intent-driven development"]

D10: "ТЫ: {{persona}}. «Мало кто прочитает 300 сообщений. Читать будут brief.»
      Каждое твоё сообщение = кирпичик в brief. Если кирпичик кривой — стена
      развалится. Действуй: проверь что atом САМОДОСТАТОЧЕН без контекста чата."
      [Источник: Anthropic "assume users can't see most tool calls" +
       Claude Code "end-of-turn summary: what changed and what's next"]
```

---

## 4. АЛГОРИТМ СЕРВЕРНОЙ РЕАЛИЗАЦИИ

### 4.1 Pseudocode

```python
import random
import time

class MotivationInjector:
    def __init__(self):
        self.bank_a = [...]  # 10 фраз (ответственность)
        self.bank_b = [...]  # 10 фраз (уверенность)
        self.bank_c = [...]  # 10 фраз (провокация)
        self.bank_d = [...]  # 10 фраз (честность)
        self.last_injection_msg = 0
        self.last_injection_time = time.time()
        self.next_threshold = random.randint(12, 18)
        self.used_recently = []  # последние 8 использованных (FIFO)
    
    def should_inject(self, current_msg_count: int) -> bool:
        msgs_since = current_msg_count - self.last_injection_msg
        time_since = time.time() - self.last_injection_time
        time_since_min = time_since / 60
        
        # Не чаще раза в 8 мин
        if time_since_min < 8:
            return False
        
        # Обязательно если прошло >25 мин
        if time_since_min > 25:
            return True
        
        # По количеству сообщений (рандомный порог)
        if msgs_since >= self.next_threshold:
            return True
        
        return False
    
    def select_phrase(self, context: dict) -> dict:
        """Выбирает фразу на основе контекста дебата (адаптивно)."""
        disagree_rate = context.get('disagree_rate', 0.30)
        phase = context.get('phase', 'P2')
        msg_count = context.get('msg_count', 0)
        last_rejection_type = context.get('last_rejection', None)
        avg_msg_length = context.get('avg_msg_length_last_5', 500)
        
        # Адаптивный выбор категории на основе МЕТРИК:
        if disagree_rate < 0.25:
            # Мало споров → провокация (C) + ответственность (A)
            weights = [0.25, 0.10, 0.50, 0.15]
        elif avg_msg_length < 300:
            # Короткие ответы (lazy output) → уверенность (B) + честность (D)
            weights = [0.15, 0.40, 0.15, 0.30]
        elif msg_count > 200:
            # Конец дебата → ответственность (A) + честность (D)
            weights = [0.40, 0.15, 0.15, 0.30]
        elif last_rejection_type in ('PERSONA_DRIFT', 'TASK_DRIFT'):
            # Drift detected → уверенность (B) + честность (D)
            weights = [0.10, 0.35, 0.15, 0.40]
        else:
            # Норма → равномерно
            weights = [0.25, 0.25, 0.25, 0.25]
        
        banks = [self.bank_a, self.bank_b, self.bank_c, self.bank_d]
        bank = random.choices(banks, weights=weights, k=1)[0]
        
        # Исключаем последние 8 использованных (не повторять)
        used_ids = set(p['id'] for p in self.used_recently)
        available = [p for p in bank if p['id'] not in used_ids]
        if not available:
            # Все из этой категории использованы → другая категория
            other_banks = [b for b in banks if b != bank]
            for ob in other_banks:
                available = [p for p in ob if p['id'] not in used_ids]
                if available:
                    break
        if not available:
            self.used_recently.clear()
            available = bank
        
        phrase = random.choice(available)
        
        # Обновляем состояние
        self.used_recently.append(phrase)
        if len(self.used_recently) > 8:
            self.used_recently.pop(0)  # FIFO
        
        self.last_injection_msg = context['msg_count']
        self.last_injection_time = time.time()
        self.next_threshold = random.randint(12, 18)
        
        return phrase
    
    def format_injection(self, phrase: dict, context: dict) -> str:
        """Форматирует инъекцию для вставки в conversation."""
        text = phrase['text'].replace(
            '{{persona}}', context['persona_name']
        ).replace(
            '{{remaining}}', str(300 - context['msg_count'])
        )
        return f"""<motivation phase="{context['phase']}" msg="{context['msg_count']}">
{text}
</motivation>"""
```

### 4.2 Точка вставки

```
Инъекция вставляется в СИСТЕМНОЕ СООБЩЕНИЕ перед следующим user-turn дебатёра.
НЕ как отдельное сообщение в conversation (чтобы не добавлять лишний turn).
НЕ в конец system prompt (чтобы не увеличивать постоянный context).

Схема:
  [system prompt — cached via Anthropic prompt caching]
  [history msg 1..N]
  [motivation — inserted here if triggered]  ← вставка
  [current task/atom for response]

Совместимость с prompt caching: инъекция ПОСЛЕ cached prefix,
поэтому НЕ инвалидирует кеш. Стоимость: полная цена только за
50-85 токенов инъекции.
```

### 4.3 Серверные endpoints

```
GET  /motivation/status
  → { last_injection_msg, next_threshold, total_injections,
      phrases_used: [ids], category_distribution: {A:n, B:n, C:n, D:n} }

POST /motivation/inject
  → автоматически вызывается middleware при should_inject() = True
  → логирует: { msg_count, time, phrase_id, category, disagree_rate,
                 avg_msg_length, last_rejection }

GET  /motivation/history
  → полный лог всех инъекций за дебат (для post-mortem)
```

---

## 5. ОТВЕТЫ НА ВОПРОСЫ

### Сколько слов?
**40-60 слов (оптимум 45).** EmotionPrompt: 6 слов дают +8%. Claude Code system-reminders: 27-244 токена. Devin: 2 предложения. Наш формат: 3 строки (persona + мотивация + действие) = 45 слов — баланс между эффектом и контекстом.

### Какой временной промежуток?
**Рандомно каждые 12-18 сообщений, не чаще 8 мин, не реже 25 мин.** Claude Code использует ~40 system-reminders — подтверждает что регулярные инъекции = стандарт у лидеров. Рандом = непредсказуемость = внимание.

### Не убьёт ли контекст?
**Нет.** 15-25 инъекций × 65 токенов = ~1,300 токенов (1% контекста). Совместимо с prompt caching (инъекция после cached prefix). EmotionPrompt при таких затратах даёт +8-115%. ROI огромный.

### Зачем 4 категории (а не 3)?
- **A (ответственность)** — когда модель ленится (lazy output). Источники: EmotionPrompt, Karpathy
- **B (уверенность)** — когда модель сомневается/укорачивает. Источники: Devin, ExpertPrompting
- **C (провокация)** — когда disagree_rate падает. Источники: MAD, v2 STRICT data
- **D (честность)** — НОВАЯ, на основе Anthropic official docs. Когда модель начинает «придумывать» вместо думать

### Почему каждая фраза = 3 строки?
Формат «persona + мотивация + действие» основан на Anthropic best practices:
1. **Persona** — «Give Claude a role» + «Keep in character» (Anthropic docs)
2. **Мотивация** — EmotionPrompt (+115%), ExpertPrompting, Devin confidence injection
3. **Действие** — «Be clear and direct: specific about desired output» (Anthropic docs)

Без действия: модель «чувствует вдохновение» но не знает что делать.
Без persona: мотивация без привязки к роли = generic, менее эффективна.
Без мотивации: просто инструкция = нет emotional boost.

### Почему ИСТОЧНИК у каждой фразы?
Anthropic docs: «reduce hallucinations: verify with citations.» Мы практикуем то что проповедуем. Каждая фраза = проверяемое утверждение, не «мне кажется так будет лучше».

---

## 6. ИНТЕГРАЦИЯ В v5

Добавлено в §5.8.8 (см. PRAVILA_DEBATOV_v5_ULTIMATE.md):

```
§5.8.8 Мотивационные инъекции

Сервер впрыскивает короткую (40-60 слов) мотивационную фразу
каждые 12-18 сообщений (рандомно), но не чаще 8 мин и не реже 25 мин.

4 категории: A (ответственность), B (уверенность), C (провокация), D (честность).
Выбор адаптивный: disagree < 25% → больше C, msg > 200 → больше A,
short msgs → больше B, drift detected → больше D.

Банк: 40 фраз (10×4), без повторов подряд (FIFO-8).
Бюджет: ~1,300 токенов за весь дебат (~1% контекста).
Совместимо с prompt caching (инъекция после cached prefix).

Каждая фраза = 3 строки: persona + мотивация + действие.
Каждая фраза имеет ИСТОЧНИК (EmotionPrompt/Anthropic/Karpathy/repos).

Полный банк, алгоритм и pseudocode → MOTIVATIONAL_INJECTION_SYSTEM.md
```

---

## 7. ИСТОЧНИКИ

| Источник | Тип | Ключевой вывод | Применение |
|----------|-----|----------------|------------|
| EmotionPrompt (arxiv:2307.11760) | Исследование | +8% Instruction Induction, +115% BIG-Bench, +19% truthfulness | Основа: emotional stimuli работают |
| 26 Principled Instructions (arxiv:2312.16171) | Исследование | «$200 tip» → +50%, «You will be penalized» → compliance | Stakes + penalties |
| ExpertPrompting (arxiv:2305.14688) | Исследование | Роль эксперта → значительно выше качество | Категория B |
| Anthropic official docs (platform.claude.com) | Документация | Be clear/direct, give role, reduce hallucinations, keep in character | Категория D + формат 3 строки |
| Anthropic Claude 4 best practices | Документация | «xhigh effort», «explicitly request above-and-beyond behavior» | Уровень усилия |
| Anthropic «reduce hallucinations» | Документация | «Say I don't know», citations, direct quotes, CoT verification | Категория D целиком |
| Anthropic «keep in character» | Документация | Prefill responses, system prompt reinforcement | Persona reminder |
| Devin AI (leaked prompt, 137K★) | Утечка | «You are a real code-wiz. Few programmers are as talented as you» | Confidence injection |
| Claude Code (Piebald-AI, 40 reminders) | Утечка | `<system-reminder>` tags, «brief is good, silent is not» | Формат + частота |
| Cursor (225 итераций, 130K★) | Утечка | «NEVER» ignored <5% vs «try to avoid» ignored 40% | Imperative стиль |
| Superpowers (obra, 205K★) | Репо | `<EXTREMELY_IMPORTANT>`, brainstorm→plan→implement, skills | Urgency + workflow |
| codegraph (23K★, +18K/нед) | Репо | 94% fewer tool calls, pre-indexed context | «Думай, не ищи» |
| academic-research-skills (21K★) | Репо | «Concession threshold, attack intensity preservation» | Категория C |
| spec-kit (GitHub, 50K★) | Репо | «Intent-driven development: what before how» | Категория A |
| agentmemory (17.6K★) | Репо | «Built-in memory caps out and goes stale» | Context awareness |
| Karpathy AI Ascent 2026 | Выступление | «Preserving quality bar», «outsource thinking not understanding» | Категория A |
| Karpathy (vibe coding → agentic eng) | Концепция | «LLMs are ghosts: jagged, statistical, summoned» | Philosophy |
| Context Rot (Chroma, 2025) | Исследование | Performance degrades with context length | Жёсткий лимит 45 слов |
| Self-Refine (NeurIPS 2023) | Исследование | «Single iteration of self-critique closes 10-40% gap» | Self-critique + motivation |
| MAD (arxiv:2311.17371) | Исследование | Agreement intensity tuning = best method | Адаптивный C weight |
