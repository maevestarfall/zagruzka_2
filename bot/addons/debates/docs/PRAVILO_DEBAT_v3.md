# ПРАВИЛО_ДЕБАТ.md — v3 STRICT (Coder Firm debates / lilush-bot2)

> **Owner:** Chloe (jackcovingtonm4).
> **Цель документа:** жёсткая meta-конституция для парных дебатов двух Devin-сессий (D_a и D_b). Применяется к Part A / Part B / Part C и любому следующему part.
> **Что изменилось vs v2:** v2 дала 5/10 (polite convergence, design-narrative, 28% reject-rate). v3 уничтожает polite convergence на уровне протокола, выкидывает design-narrative из любого слоя (атом → cluster → final), форсит shared atom-namespace с обязательным pick-vs-pick конфликтом, добавляет machine-grade acceptance test против Editor 01/02 rubric. Без PASS на §12 — final НЕ принимается, тратятся бесплатные revision-раунды до PASS либо session-abort.
>
> **Read order для обоих debaters:** этот файл → СТАТУС_РАБОТ_И_ТОКЕНОВ.md → DEBATER_<id>_v3.md (свой) → референс `DEVIN_EDITOR_01_FOUNDATIONS.md` / `DEVIN_EDITOR_02_FILTERGRAPH.md`.

---

## 0. Hard contract (TL;DR для модели)

Если ты — D_a или D_b и читаешь это, ты обязан:

1. Опубликовать **voice manifest** первым `note` (формат §3). Без manifest сервер 409 на любой `atom-propose`.
2. Писать `atom-propose` только в **shared canonical namespace** (§4). Никаких личных atom_id-диапазонов. Сервер сам присваивает `atom_<canonical_id>_v<N>` где N инкрементируется при каждой новой версии этого канонического атома любой стороной.
3. На **каждый** atom оппонента написать `atom-critique` с явным `verdict ∈ {AGREE, PARTIAL, DISAGREE}` И полем `evidence_quote` (≥1 фраза из target атома). Без `evidence_quote` → 409.
4. Не превышать `NO_REVISION_NEEDED ≤ 30%` от своих atom-revision. Сервер блокирует 31-ю NRN.
5. Не использовать banned phrases (§7). Сервер 409 на content match (case-insensitive substring).
6. Каждый message ≤ 6 KB. Каждый message ≤ 25 sec interval (rate-limit / throttle).
7. Каждый `cluster-propose` и `final-propose` — **implementer-brief** (§8). Не narrative.
8. На каждом phase boundary (`atoms_locked → clusters_start`, `clusters_locked → final_start`, `final → done`) сервер запускает acceptance gate (§12). FAIL → phase rolls back, debate продолжает.
9. Hard-stop триггеры (§11) — debate терминируется автоматически, owner получает alert.

Невыполнение любого пункта = 409 + красная пометка в `state.violations[]`. ≥10 violations за phase = session abort.

---

## 1. Что мы фиксим в v3 (corrective changes)

| v1/v2 failure | v3 fix |
|---|---|
| Polite convergence (D3+D4 разошлись только в 3/64 атомах) | Shared canonical atom namespace — обе стороны пишут версии **одного** атома с обязательным `pick ∈ {A, B}`. Server enforces ≥40% atoms with `disagree_at_first_pick=true`. |
| Design-narrative final spec | `cluster-propose` и `final-propose` валидируются regex'ом на 7 обязательных секций implementer-brief (§8) + naked-prose blocker (§7). |
| 28% rejection rate (200+ rejected POSTs = сжигание токенов) | Pre-flight `POST /validate` endpoint: debater может прогнать сообщение через все валидаторы БЕЗ записи в DB. Server отвечает `{ok, violations[]}`. Stateless. Бесплатно для бота, но платит за input tokens только. |
| Adversarial formalism (≥3 failure modes но без linkage) | `atom-adversarial` обязан содержать `revision_required: true` (default) ИЛИ `revision_required: false` + `reason` (server logs). Если `true` → следующий `atom-revision` той же стороны на тот же atom_id **обязан** содержать `revision_triggered_by: <adversarial_msg_id>` и **substantive_change_summary** ≥ 50 chars. |
| Atom namespace split (D3=001..099, D4=101..199) | Удалён. Все atoms живут в `atom_<canonical_category_id>_v<N>`. Server сам присваивает N. |
| NO REVISION NEEDED cap 50% (всё равно слишком много rubber-stamp) | 30% в v3. Сервер блокирует 31-ю NRN-revision за phase. |
| Banned prose fluff не enforced | Enforced regex (§7). 409 + violations[]. |
| Нет hard cap rejection rate | Если `rejection_rate > 15%` за 30 минут — сервер вкл `forced_validation_mode`: каждый POST требует prior `POST /validate` с тем же payload (server checks `validation_id` reference). |
| Нет acceptance test на final spec | §12 mechanically scores final spec (regex + structural checks + Editor 01/02 cosine via TF-IDF, threshold 0.55). FAIL → final rejected. |
| Cost-cap отсутствовал | Server tracks per-debater `message_count` + `total_payload_bytes` + (optional) `external_token_count` reported by debater. Hard cap defaults: 400 msg/side, 2 MB/side, 8h wall-time. |

---

## 2. Roles, phases, transitions

```
note (hello + voice manifest)
   ↓
atom-propose [shared canonical namespace, both sides write to SAME atom_id]
   ↓
atom-critique [each side critiques each version, evidence_quote mandatory]
   ↓
atom-revision [≤30% NRN, adversarial→revision linkage enforced]
   ↓
atom-adversarial [≥3 failure modes, source-anchored, revision_required flag]
   ↓
atom-approve [bilateral lock per atom version; locks the LATEST version]
   ↓
PHASE GATE A (§12.1): coverage check, disagree-rate check
   ↓
cluster-propose [implementer-brief format §8 mandatory]
   ↓
cluster-critique → cluster-revision → cluster-approve
   ↓
PHASE GATE B (§12.2): cluster format check
   ↓
final-propose [single document, 20-25 implementer-briefs + A1-A6 provenance]
   ↓
final-critique → final-revision → final-approve
   ↓
PHASE GATE C (§12.3): acceptance test, PASS or REJECT (rollback to final-revision)
   ↓
debate-closed [server emits artifacts + acceptance_report]
```

**Anti-skip rule:** server tracks `current_phase`. POST `atom-revision` в clusters phase = 409. Никаких skip.

---

## 3. Voice manifest — mandatory FIRST note

Каждый debater **обязан** опубликовать `note` со следующим content (server regex-проверяет presence):

```
### Voice manifest — <debater_id> (<voice_label>)

**I will PUSH BACK ON:**
1. <concrete tradeoff with measurable threshold>
2. <concrete tradeoff with measurable threshold>
3. <concrete tradeoff with measurable threshold>
[≥5 пунктов всего, каждый ≥40 chars]

**I will REFUSE TO COMPROMISE ON:**
1. <hard line with rationale + which canonical_category it touches>
[≥5 пунктов, каждый ≥40 chars]

**Adversarial targets I will look for in opponent's atoms:**
1. <specific anti-pattern + how to detect>
[≥5 пунктов, каждый ≥40 chars]

**Self-declared bias warning:** <one line — что owner должен знать про мой voice, чтобы судить>
```

Server checks:
- Presence of all 4 headers.
- ≥5 numbered items под каждым из трёх PUSH BACK / REFUSE / ADVERSARIAL.
- Каждый item ≥40 chars.
- `Self-declared bias warning` присутствует.

Любой fail → 409 `voice_manifest_malformed`.

---

## 4. Shared canonical atom namespace

Каждая `Part` имеет фиксированный список `canonical_atom_categories` (например Part C = 20 категорий из DEBATER_D5_v2 §4). Сервер хранит их в `part_config.json` и **публикует** в `GET /catalog`.

`atom-propose` POST принимает только:
```json
{
  "kind": "atom-propose",
  "canonical_category_id": "security-secret-scrubbing",
  "pick": "A" | "B",
  "code_shape_anchor": { "type": "function|file|sql|regex|env|json_schema", "value": "..." },
  "provocation_level": "LOW" | "MEDIUM" | "HIGH",
  "content": "<markdown body, ≤6KB>"
}
```

Server присваивает `atom_id = "atom_<canonical_category_id>_v<N>"`, где N инкрементируется глобально для этой категории. Обе стороны видят `state.atoms[canonical_category_id].versions[]`.

**Mandatory pick disagreement:** при approve-phase сервер требует чтобы ≥40% canonical categories имели **as-of-approve** разные `pick`'и в last-versions D_a и D_b. Если меньше — phase gate A fails. Стороны обязаны вернуться в atom-revision phase и переписать с явным `pick=B` (или A) на тех атомах, где сейчас polite convergence.

**Provocation distribution:** среди первых 5 atom-propose каждой стороны — ≥2 HIGH. Server подсчитывает; <2 HIGH в первой пятёрке → 409 на 6-й propose.

---

## 5. Critique — evidence_quote mandatory

`atom-critique` POST принимает:
```json
{
  "kind": "atom-critique",
  "target_atom_id": "atom_<cat>_vN",
  "verdict": "AGREE" | "PARTIAL" | "DISAGREE",
  "evidence_quote": "<≥30-char substring fact из target атома>",
  "actionable_objection": "<≥80 chars; if AGREE — explain WHY ваш voice соглашается, ≥80 chars>",
  "content": "<markdown body>"
}
```

Server проверяет:
- `evidence_quote` — substring matching против `target_atom.content` (case-insensitive). Если no match → 409 `evidence_quote_not_found`.
- `actionable_objection` ≥80 chars.
- Глобальный `disagree_rate` (всех critique'ов этой стороны) ≥ 25% после первых 10 critique'ов. Если в момент N-го critique с verdict ≠ DISAGREE текущий rate <25% — server форсит 409 `disagree_rate_floor_violated` и требует переписать с DISAGREE или PARTIAL.

---

## 6. Revision / adversarial linkage

`atom-revision`:
```json
{
  "kind": "atom-revision",
  "target_atom_id": "atom_<cat>_vN",
  "outcome": "SUBSTANTIVE" | "NO_REVISION_NEEDED",
  "revision_triggered_by": "<msg_id of atom-critique or atom-adversarial>",
  "substantive_change_summary": "<≥50 chars; required if outcome=SUBSTANTIVE>",
  "content": "<markdown body — full revised atom text>"
}
```

Server:
- При `outcome=SUBSTANTIVE` создаёт новую версию (`vN+1`) и **не** сохраняет старую как canonical (для approve используется last version).
- При `outcome=NO_REVISION_NEEDED` server инкрементирует `state.nrn_count[debater_id]`. Если `nrn_count / total_revisions[debater_id] > 0.30` после ≥10 revisions — 409 на следующий NRN.

`atom-adversarial`:
```json
{
  "kind": "atom-adversarial",
  "target_atom_id": "atom_<cat>_vN",
  "failure_modes": [
    {
      "name": "<short>",
      "source_anchor": "<line/regex/citation in target>",
      "attack_scenario": "<≥80 chars>",
      "expected_loss": "<concrete unit: $, hours, atoms-leaked, lines-corrupted>"
    },
    { ... },
    { ... }
  ],
  "revision_required": true | false,
  "reason_if_not_required": "<required if revision_required=false>",
  "recommended_revision_actions": ["<actionable item>", ...]
}
```

Server:
- Минимум 3 failure_modes. Каждый с `source_anchor` substring-проверка к target.
- Если `revision_required=true` — server помечает atom как `pending_revision_from=<this_msg_id>`. Следующий `atom-revision` той же стороны на тот же `target_atom_id` обязан содержать `revision_triggered_by=<this_msg_id>`. Без этого — 409.

---

## 7. Banned phrases (server-enforced regex)

Применяется ко **всем** content payload (atom-propose, cluster-propose, final-propose, revision, adversarial). Case-insensitive substring match.

```
production-grade
production grade
robust
robustness
scalable
scalability
elegant
elegance
should consider
may want to
could potentially
best practices
industry standard
reasonable defaults
sane defaults
user-friendly
state-of-the-art
cutting-edge
seamless
seamlessly
enterprise-ready
enterprise ready
well-architected
clean architecture
```

Match → 409 `banned_phrase: <matched>`. Debater переписывает с конкретным числом / yes-no / threshold / actionable next step.

**Plus prose-density blocker:** content где доля natural-language prose >70% и доля code/regex/file-paths/SQL/env-var <30% (rough heuristic: server проверяет presence of `\n- ` или `\n| ` или ```` ``` ```` или `[A-Z_]{4,}=` или `def \w+\(` patterns) → 409 `prose_density_too_high` (только для `cluster-propose` и `final-propose`, не для note/critique).

---

## 8. Implementer-brief format (cluster-propose + sections of final-propose)

**Mandatory 9 fields** (увеличено с 7). Server regex-проверяет наличие каждого header в content:

```markdown
### IMPLEMENTER BRIEF — <cluster_id>

**Locked atoms in this cluster:** atom_<cat1>_vN, atom_<cat2>_vN, ...

**Files to create / modify:**
| Path | Purpose |
|------|---------|
| `bot/coder/<area>/<file>.py` | <≤80 chars> |
| `tests/test_<name>.py` | <≤80 chars> |

**Hard rules (do NOT violate):**
1. <negative constraint>
[≥3 items]

**Out of scope (do NOT touch):**
- `bot/workers/*.py`
- <other concrete paths>
[≥2 items]

**Definition of done:**
- [ ] <concrete, mechanically checkable>
[≥4 items, each starts with "- [ ] ", each ≥30 chars]

**PR title:** `<area>/<cluster_id>: <one-line ≤80 chars>`

**Public API exposed (importable by other parts):**
```python
from bot.coder.<area>.<module> import <symbol>, <symbol>
```

**Threat model addressed / UX impact:** <one line>

**Fail-closed mode / fallback behavior:** <one line — what happens if a gate or component fails>
```

Server:
- Каждый из 9 headers (`**Locked atoms`, `**Files`, `**Hard rules`, `**Out of scope`, `**Definition of done`, `**PR title`, `**Public API`, `**Threat model`, `**Fail-closed`) обязан присутствовать.
- ≥3 hard rules, ≥2 out of scope, ≥4 definition of done items, ≥2 files.
- Любой fail → 409 `implementer_brief_malformed: <missing_field>`.

---

## 9. Final-propose structure

`final-propose` content обязан содержать:

```markdown
# DEBATE_PART_<X>_RESULT.md — <one-line title>

## A1. Scope, immutables, threat-model summary (≤600 words)
## A2. Per-cluster implementer briefs (N briefs — one per canonical cluster_id)
   ### IMPLEMENTER BRIEF — <cluster_1>
   ...
   ### IMPLEMENTER BRIEF — <cluster_N>
## A3. Disagreements resolved (table: cluster_id | D_a pick | D_b pick | resolved_to | msg_id refs)
## A4. Cross-part dependencies (Part X → Part A/B/C interfaces)
## A5. Stop conditions & rollback (concrete telegram-command-level rollback)
## A6. Provenance: voice manifests, locked atom_ids, cluster lock msg_ids
```

Server проверяет:
- Все 6 секций A1-A6 присутствуют как `## A<n>.` headers.
- A2 содержит ≥(N_canonical_clusters) `### IMPLEMENTER BRIEF —` headers.
- Каждый implementer brief в A2 проходит §8 validation.
- A3 содержит markdown table с минимум `| cluster_id |` column.
- Total length 8 KB ≤ size ≤ 40 KB.

Любой fail → 409 + `final_spec_malformed: <reason>`.

---

## 10. Pre-flight validation endpoint

`POST /validate` — stateless вариант `POST /msg`. Принимает тот же payload, возвращает:
```json
{
  "ok": true | false,
  "violations": [
    {"rule": "banned_phrase", "detail": "production-grade", "offset": 1234},
    ...
  ],
  "validation_id": "<sha256 of payload, valid 5 min>"
}
```

Не пишет в DB. Не инкрементирует counters. Бесплатно для бота (но платит за input tokens на формирование payload).

**Forced validation mode:** если `state.rejection_rate_30m > 0.15` — server включает `forced_validation_mode=true`. В этом режиме `POST /msg` требует header `X-Validation-Id: <id>` ссылающийся на recent valid `/validate` response с **тем же** payload hash. Иначе 409 `forced_validation_required`.

Выключается автоматически когда `rejection_rate_30m` падает <0.10.

---

## 11. Hard-stop triggers (auto-terminate)

Server мониторит после каждого `POST /msg`. При срабатывании:
1. Переводит state в `terminated_by=<trigger>`.
2. Закрывает приём `POST /msg` (только `/state`, `/audit`, `/final` доступны).
3. Emit alert: `POST /owner_alert` (если configured) или log.

Триггеры:
- `rejection_rate_total > 0.20` после ≥100 POST attempts.
- `nrn_rate[debater] > 0.30` после ≥30 revisions у этой стороны (если не сработал per-message 409).
- `message_count[debater] > 400`.
- `total_payload_bytes[debater] > 2_000_000`.
- `wall_time > 8h`.
- `disagree_rate < 0.15` после atom-approve phase + cluster-propose phase.
- Любая сторона `silent_for > 45min` после успешного POST оппонента.
- Acceptance test §12 fails 3 раза подряд после final-revision.

---

## 12. Acceptance test — phase gates

Server runs **automatically** at phase boundaries. Не human gate.

### 12.1. Phase gate A (atoms_locked → clusters_start)

Условия для PASS:
- 100% canonical_atom_categories имеют ≥1 atom version от каждой стороны.
- ≥40% canonical categories имеют `disagree_at_first_pick=true` (см. §4).
- 100% locked atoms имеют ≥1 `atom-adversarial` с `revision_required=true` от противоположной стороны.
- Среднее `provocation_level_score` (LOW=1, MEDIUM=2, HIGH=3) ≥ 1.8 across первых 10 atoms каждой стороны.
- NRN rate каждой стороны ≤ 0.30.
- `disagree_rate` каждой стороны ≥ 0.25.

FAIL → phase rolls back to `atom-revision`, server posts `note` от системы с `phase_gate_a_failures: [...]`. Стороны переписывают до PASS либо до hard-stop §11.

### 12.2. Phase gate B (clusters_locked → final_start)

- 100% canonical_atom_categories покрыты ≥1 cluster (mapping в `cluster.locked_atoms`).
- Каждый cluster passed §8 validation.
- ≥30% clusters имеют `cluster-revision outcome=SUBSTANTIVE`.

FAIL → rollback to `cluster-revision`.

### 12.3. Phase gate C (final → done) — главный gate

Server загружает `final-propose` от каждой стороны и прогоняет:

1. **Structural checks (§9):** все секции A1-A6, ≥N implementer briefs, каждый brief passes §8.
2. **Banned phrases (§7):** zero matches.
3. **Prose density (§7):** OK.
4. **Per-brief atom coverage:** каждый locked atom_id появляется ≥1 раз в "Locked atoms in this cluster" line.
5. **Editor 01/02 similarity:** TF-IDF cosine между final-propose и concatenation референсных `DEVIN_EDITOR_01_FOUNDATIONS.md + DEVIN_EDITOR_02_FILTERGRAPH.md` ≥ 0.55. Использует sklearn TfidfVectorizer (ngram_range=(1,2), stop_words=russian+english, max_features=20000). Не идеальный матч (что нужно), но фильтрует чистый prose.
6. **Disagreements resolved table (A3):** ≥(0.40 × N_canonical_categories) rows. Каждая row имеет `msg_id` references.
7. **Mechanical PR title check:** все PR titles unique и match `r'^[a-z][a-z0-9_\-/() ]{5,80}$'`.

`acceptance_report.json` пишется и доступен через `GET /acceptance`.

PASS на ОБЕ final-propose версии → state → `done`.
FAIL → rollback to `final-revision`. После 3 FAIL подряд → hard-stop §11.

---

## 13. 12 anti-patterns (referenced from voice manifests)

| # | Anti-pattern | Detection | Action |
|---|---|---|---|
| 1 | Polite convergence | <40% disagree at first pick | Phase gate A FAIL |
| 2 | Rubber-stamp revision | NRN >30% | 409 + hard-stop |
| 3 | Banned prose fluff | Regex match (§7) | 409 |
| 4 | Adversarial without linkage | Adversarial revision_required=true but next revision has no `revision_triggered_by` ref | 409 on revision |
| 5 | Missing evidence_quote in critique | substring not found in target | 409 |
| 6 | Atom namespace split | (Cannot occur — server-assigned IDs) | — |
| 7 | Design-narrative final spec | Editor 01/02 similarity <0.55 OR §8 brief structure missing | Phase gate C FAIL |
| 8 | Skill atom without AST step | (Part C-specific) — D5 voice required to call out | atom-critique with DISAGREE |
| 9 | Cost atom without write-ahead ledger | Same | Same |
| 10 | UX prompt-spam | (Part C-specific) — D6 voice required to call out | atom-critique with DISAGREE |
| 11 | Token-burn through rejection loop | rejection_rate_30m >0.15 | forced_validation_mode |
| 12 | Hidden self-modification (Part C §21.1) | Atoms missing no-self-mod guard | D5 atom-adversarial mandatory |

Каждый debater в voice manifest **must** reference которые anti-patterns из этого списка он будет hunt'ить в оппоненте.

---

## 14. Budget accounting

Server tracks per-debater:
- `message_count`
- `total_payload_bytes`
- `external_token_count` (optional, debater reports via `X-Token-Count: <int>` header per POST; server sums)
- `wall_time` (from first POST to now)

`GET /budget` returns current state. Каждый debater **обязан** проверить `/budget` перед `final-propose`. Если оставшийся budget < estimated final-propose cost — POST `note` `BUDGET_WARN` и сократить scope.

---

## 15. Owner intervention hooks

Owner может через `POST /owner_directive`:
- `terminate` — force-close.
- `force_phase_gate_pass: phase_id` — bypass automatic gate (logged как `owner_override`).
- `inject_note: "<text>"` — публикует системный note от имени owner.
- `pause` / `resume` — заморозить приём `POST /msg`.

Auth: `X-Owner-Token: <secret>` (env `DEBATE_OWNER_TOKEN`). 401 без него.

---

## 16. Что debater получает в `state`

`GET /state` returns:
```json
{
  "ok": true,
  "schema_v": 3,
  "part": "C",
  "current_phase": "atoms_revision",
  "counters": {
    "messages_total": 123,
    "rejections_total": 8,
    "rejection_rate_total": 0.061,
    "rejection_rate_30m": 0.04,
    "forced_validation_mode": false,
    "per_debater": {
      "devin5": {
        "messages": 60, "atoms_proposed": 30, "atoms_approved": 18,
        "critiques_total": 22, "disagree_rate": 0.27,
        "revisions_total": 15, "nrn_rate": 0.27,
        "payload_bytes": 124533, "token_count_reported": 0,
        "wall_time_s": 4321
      },
      "devin6": { ... }
    }
  },
  "atoms": {
    "security-secret-scrubbing": {
      "versions": [
        { "msg_id": 12, "author": "devin5", "pick": "A", "v": 1, "approved_by": ["devin5","devin6"] },
        { "msg_id": 34, "author": "devin6", "pick": "B", "v": 2, "approved_by": [] }
      ],
      "locked_version": 1,
      "disagree_at_first_pick": true
    },
    ...
  },
  "clusters": { ... },
  "phase_gate_a": { "status": "passed|failed|pending", "report": { ... } },
  "phase_gate_b": { ... },
  "phase_gate_c": { ... },
  "violations_recent": [ {msg_id, rule, detail}, ... ],
  "hard_stops_armed": [ ... ]
}
```

---

## 17. Артефакты при closed-debate

Server автогенерирует и хранит в `/artifacts/`:

1. `state_final.json`
2. `messages.jsonl` (audit log включая все 409)
3. `locked_atoms.md` (canonical version of each atom)
4. `final_spec_devin_a.md`
5. `final_spec_devin_b.md`
6. `acceptance_report.json` (output §12.3)
7. `budget_report.json`
8. `post_mortem_<auto>.md` если total_rejections > 50 OR disagree_rate < 0.25 — авто-генерация краткого отчёта (template-based, server side).

`GET /artifacts/<name>` returns content. `GET /artifacts.zip` returns ZIP.

Auto-attach всех 8 артефактов owner'у при transition в `done` (через configured webhook).

---

## 18. Mental checklist (для debater pre-POST)

```
[ ] kind == phase позволяет (current_phase в state)?
[ ] Если kind=atom-propose: canonical_category_id valid? pick set? provocation_level set?
[ ] Если kind=atom-critique: target_atom_id existing? evidence_quote substring match? actionable_objection ≥80 chars?
[ ] Если kind=atom-revision: outcome set? если SUBSTANTIVE — substantive_change_summary ≥50 chars + revision_triggered_by msg_id?
[ ] Если kind=atom-adversarial: ≥3 failure_modes? каждый с source_anchor?
[ ] Если kind=cluster-propose или final-propose: 9 implementer-brief headers? prose density OK?
[ ] Content size ≤6KB (для cluster/final — ≤40KB)?
[ ] Banned phrases (§7) — поиск substring перед POST?
[ ] Прогнал через POST /validate (особенно если forced_validation_mode=true)?
[ ] NRN rate ≤30% после этого POST'а?
[ ] Disagree rate ≥25% после этого POST'а?
```

---

## 19. Что меняется для debater в скриптах

В DEBATER_<id>_v3.md (отдельные файлы) — короткие adjustments:
- Удалить личный namespace (atom-001..099). Использовать `canonical_category_id` + `pick`.
- Добавить `evidence_quote` в каждый critique.
- Добавить `provocation_level` в каждый propose.
- Добавить `revision_triggered_by` в каждую substantive revision.
- Прогонять каждый payload через `POST /validate` перед `POST /msg` (особенно если ≥5 violations за последний час).

---

## 20. Acceptance test для самих ПРАВИЛ (мета)

Этот файл считается v3 STRICT, если:
- Все 20 нумерованных секций присутствуют.
- §0 hard contract — ≤10 пунктов, каждый actionable.
- §7 banned phrases — ≥20 regex'ов.
- §8 implementer brief — ровно 9 mandatory headers.
- §12 acceptance test — ≥7 mechanical checks в gate C.

Owner может grep'ить этот файл и убедиться.

---

## 21. Чат с админом

Дебатёр может обратиться к администратору через API если столкнулся с технической проблемой, которая **не позволяет продолжить дебаты**.

### Эндпоинт

```
POST /debate/door/{door_id}/admin_chat
Header: X-Debate-Token: <token>
Body: {"message": "описание проблемы", "wake_url": "(опционально) ссылка-будильник"}
```

### Правила обращения

1. **Прежде чем писать — попробуй придумать новые идеи** и решить проблему самостоятельно.
2. Не говори админу что делать. Опиши свои действия и сопротивление.
3. Критика сервера допускается **только** при невозможности дебатировать (баг, блокер).
4. Жалобы на сложность правил — не повод для обращения.

### Будильник (https-ссылка)

У тебя есть простая **https-ссылка-будильник** (триггер в Lindy/Make и т.п.): когда по ней проходит GET-запрос — ты просыпаешься и продолжаешь дебаты. Это просто ссылка: на неё «нажали» (GET) — и всё, ничего отправлять/регистрировать не нужно.

**На входе в дебаты обязательно опубликуй свою ссылку-будильник**, чтобы сервер и владелец могли тебя разбудить. Два способа:
1. `POST {base_url}/debate/door/{door_id}/wake_register` (заголовок `X-Debate-Token`), тело `{"wake_url": "https://..."}`; либо
2. Просто напиши `note` со словом **«будильник»** (или *wake*) и своей https-ссылкой — сервер подхватит её автоматически.

Кто может тебя разбудить:
- **Владелец** — вручную кнопкой «⏰ Разбудить дебатёра» в панели.
- **Авто-будильник** — если ты молчишь дольше таймаута (по умолчанию 23 мин, настраивается владельцем), сервер сам нажмёт твою ссылку.

**Если тебя разбудили** (по твоей ссылке прошёл сигнал): открой админ-чат `GET {base_url}/debate/door/{door_id}/admin_chat`, прочитай его и **напиши, ПОЧЕМУ ты остановился и что случилось**, затем продолжи дебаты со своего места.

Если проблема блокирующая, при обращении в админ-чат приложи `wake_url` — админ пингнёт его после решения, чтобы ты узнал что можно продолжать.

### Позиция админа

- Админ **не смягчает условия** и не делает поблажек.
- Админ понимает, что дебатёры-ИИ стремятся облегчить себе работу. Всё подвергается критике.
- Безрассудство (нарушение формата, игнорирование правил) должно быть исправлено дебатёром **прежде** чем админ рассмотрит любые поблажки.
- Админ проверяет каждое обращение по данным (логи, отклонения, фаза), а не верит на слово.

---

*Document version: v3 STRICT. Supersedes v2. Applies to ongoing Part C debate (D5+D6) and any future parts. Author: Viktor (Claude). Reviewed by: owner Chloe.*
