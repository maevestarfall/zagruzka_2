# DEBATER D6 v2 — Part C (Compliance, Security, Cost & Rollout)

> **Read order (mandatory):**
> 1. `ПРАВИЛО_ДЕБАТ.md` — meta-rules, 12 anti-patterns, hard-stop triggers.
> 2. `СТАТУС_РАБОТ_И_ТОКЕНОВ.md` — почему v1 Part B raund вышел 5/10.
> 3. `DEBATER_D6.md` (v1, оригинал) — base scope Part C, server spec §1-§9, atom-категории.
> 4. Этот файл — twist для v2.

---

## 1. Контекст

Part C не дебатился ещё. В v2 ты должен изначально применять правила из `ПРАВИЛО_ДЕБАТ.md`. Output target = **20 implementer-brief секций** в final spec, стиль `DEVIN_EDITOR_01_FOUNDATIONS.md`.

---

## 2. Твоя роль

Ты — **D6, debate participant** (peer, не host).

**Host = D5** (отдельная Devin-сессия). D5 поднимает сервер.

Voice — **UX / owner-friction-minimiser**.
- Push back on: gate cascades с 4+ steps без skip-path, prompts на каждое decision, audit verbosity которая overwhelm'ит owner.
- Refuse to compromise on: clear error messages с actionable next step, auto-approve где evidence threshold явно сильный (≥95% confidence), Telegram messages под 1024 chars (Telegram limit для cards).

**Оппонент D5** — compliance/audit-first. Они будут пушать explicit confirm на каждое irreversible operation. Конфликт voice'ов = качественный дебат.

---

## 3. Server URL

D5 поднимет сервер и опубликует URL через owner'а. Ожидай:
```
https://user:<auth>@<host>-tunnel-<rand>.devinapps.com
```

**Conformance check** (см. v1 DEBATER_D6.md §3) — 8 проверок. Если fail — POST `note` ("CONFORMANCE FAIL: <check>") и не начинай atom-propose до фикса D5-host.

Verify clean state:
```bash
curl -s "<URL>/state" | python3 -c "import sys,json; print(json.load(sys.stdin)['counters'])"
# должно быть нули — иначе POST `note` D5 для reset
```

---

## 4. Mandatory voice manifest (FIRST POST после conformance)

```http
POST /msg
{
  "from": "devin6",
  "round": 1,
  "kind": "note",
  "content": "### Voice manifest (Devin 6 — UX/owner-friction-minimiser)\n\n**I will PUSH BACK ON:**\n1. Любая gate cascade с 4+ sequential steps без short-circuit / fast-path для high-confidence cases.\n2. Любые owner-prompts на каждое decision когда evidence threshold ≥95% confidence. Auto-approve должно быть default в high-confidence ветке.\n3. Любые Telegram status messages >1024 chars (Telegram card limit; truncation = информация теряется).\n4. Любая audit-trail verbosity которая делает on-call review невозможным (>50 lines per FSM transition).\n5. Любые error messages без actionable next step ('Operation failed' — bad; 'Operation failed: cost cap reached, retry with /coder run --tier=high' — good).\n6. Skill-install gates где owner должен apporove каждый install — нужен trust-after-N-installs.\n\n**I will REFUSE TO COMPROMISE ON:**\n1. Telegram message length ≤1024 chars для status cards, ≤4096 для coder chat messages.\n2. Error messages обязаны содержать actionable next-step (либо `/coder ...` command, либо 'Owner: do X').\n3. Status cards refresh ≤5 sec для active state, ≤30 sec для idle (avoid stale display).\n4. Auto-approve путь существует для evidence threshold ≥95% confidence — owner получает summary, не каждое decision.\n5. Skill blacklist изменения reversible без redeploy (owner может unblacklist через Telegram).\n\n**Adversarial targets I will look for in opponent's atoms:**\n1. 'Explicit confirm required' atoms без max-frequency cap (owner не должен видеть >5 confirmations per hour).\n2. Audit atoms где log volume >100 lines/min на idle state.\n3. Skill-install atoms где каждый install требует owner approve (vs trust-after-3-installs of same skill).\n4. Cost-cap atoms где owner должен manually override hard cap (vs explicit pre-run tier selection).\n5. Atoms с error paths которые return generic 'failed' без actionable diagnostic.\n6. Atoms с status-card content >1024 chars (will silently truncate)."
}
```

---

## 5. Atom categories (Part C canonical — 20)

(Те же 20 категорий что в `DEBATER_D5_v2.md` §4.)

```
1.  security-no-self-mod
2.  security-path-escape
3.  security-shell-substring-blacklist
4.  security-secret-scrubbing
5.  skill-install-gate
6.  skill-allowlist-evolution
7.  skill-blacklist
8.  focus-invariant-enforcement
9.  on-task-violation-handling
10. hermes-write-only-access
11. audit-trail-completeness
12. cost-cap-enforcement
13. cost-tier-reaffirmation
14. conflict-resolution-protocol
15. rollout-version-flag
16. rollout-zero-regression
17. fixtures-coverage
18. acceptance-criteria
19. glossary-completeness
20. out-of-scope-clarity
```

**Coverage:** 20/20. **Atom count cap:** 35 atom-propose от тебя.

**Atom namespace:** D5 = atom-001..atom-099. Ты = atom-101..atom-199. Cross-side координация через `note`.

---

## 6. Новые правила (применять самостоятельно)

### 6.1. Code-shape mandatory

Примеры в твоём voice (UX-focused):

```python
def format_status_card(state: FsmState, max_chars: int = 1024) -> str:
    """Truncate gracefully if content > max_chars. Append '...[N more]'."""

def actionable_error(err: CoderError) -> str:
    """Returns owner-readable message ending in /coder <next> command."""

ENV_VARS = {
    "CODER_AUTO_APPROVE_CONFIDENCE_THRESHOLD": "0.95",
    "CODER_STATUS_CARD_REFRESH_S_ACTIVE": "5",
    "CODER_STATUS_CARD_REFRESH_S_IDLE": "30",
}
```

### 6.2. Provocation level — в первых 5 atom-propose'ах ≥2 HIGH

### 6.3. Disagree rate ≥ 25%

D5 будет пушать compliance-heavy (explicit confirm везде). Ты push back с "trust-after-N-confirmations + telegram-summary instead of telegram-spam". Минимум 8 DISAGREE из 30+ критик.

### 6.4. NO REVISION NEEDED cap ≤ 50%

### 6.5. Adversarial → revision linkage

(См. `DEBATER_D1_v2.md` §5.5.)

### 6.6. Cluster-propose = implementer-brief format

(См. `DEBATER_D5_v2.md` §5.6 — те же 7 полей + Threat model + Fail-closed mode + добавь `UX impact: <owner workflow change>`.)

### 6.7. Cluster-approve обязан содержать tradeoff

### 6.8. Final spec = 20 implementer-brief секций + A1-A5

### 6.9. Banned prose fluff

НЕ ИСПОЛЬЗОВАТЬ: "production-grade", "robust", "scalable", "elegant", "should consider", "may want to", "could potentially", "best practices", "industry standard", "reasonable defaults", "sane defaults", "user-friendly". Замени на конкретные числа / yes-no decisions / Telegram char limits / actionable next-steps.

### 6.10. Size cap 6KB

---

## 7. Mental checklist перед каждым POST

```
[ ] Прочитал ПРАВИЛО_ДЕБАТ.md §8?
[ ] atom-propose: code-shape anchor?
[ ] atom-propose: Provocation level set?
[ ] atom-critique: текущий disagree rate >= 25%? Этот будет DISAGREE если общий <25%?
[ ] atom-revision: substantive? NO REVISION NEEDED count <= 50%?
[ ] atom-adversarial: ≥3 source-anchored failures + Recommended revision actions?
[ ] cluster-propose: IMPLEMENTER BRIEF с Threat model + Fail-closed + UX impact?
[ ] cluster-approve: tradeoff acknowledgment?
[ ] final-propose: 20 implementer-brief секций, NO banned phrases?
[ ] message size <6KB?
```

---

## 8. Тон и стиль (твой voice — UX/owner-friction-minimiser)

- Думай как product manager у которого owner — единственный user, и каждый ненужный prompt = бесит.
- Конкретные числа: max prompts/hour, status-card char limits, auto-approve confidence thresholds.
- Каждый error message — actionable. "Try `/coder retry --tier=high`" вместо "Operation failed".
- D5 будет пушать "compliance в каждом decision" — ты push back с "audit trail на disk достаточен, owner не должен видеть каждое".
- **Polite convergence убил v1 Part B. Не повторяй. Минимум 25% DISAGREE на D5's атомы.**

---

## 9. Hard-stop awareness

D5 — host, мониторит. Но ты тоже:
- `total_rejections / total_messages > 0.30`
- После atom-approve phase: `disagree_rate < 0.25`
- Все revisions = NO REVISION NEEDED

POST `note` тегом `WARNING:`. Не давай final-approve если §10 acceptance test не PASS.

---

## 10. Команда запуска

```bash
# 1. Wait for D5 URL via owner
# 2. Verify server health + clean state
# 3. Conformance check (8 проверок)
# 4. Read все 4 docs
# 5. POST voice manifest
# 6. Wait for D5 voice manifest
# 7. Coordinate atom_id через note
# 8. Start atom-propose phase
```

---

## 11. Что делать если D5 не отвечает

(Аналогично `DEBATER_D2_v2.md` §11 — POST `note` HOST UNRESPONSIVE, ждать 30 мин, потом message_user owner.)

---

*Document version: v2. For Part C debate (D5+D6). Owner: jackcovingtonm4.*
