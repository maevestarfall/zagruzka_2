# DEVIN_EDITOR_01_FOUNDATIONS.md (compact reference for TF-IDF cosine gate)

This file is a compact, anonymised reference for the acceptance test in
PRAVILO_DEBAT_v3 §12.3. It captures the implementer-brief style we want the
final spec to match.

## Files to create / modify
- bot/coder/persona.py
- bot/coder/comm.py
- data/schema.sql
- .env.example

## Hard rules (do NOT violate)
1. NO shared SQLite or install root with sibling bots.
2. NO cross-service HTTP between Coder personas; mailbox only.
3. Boot-time assertion: persona namespace disjoint set.

## Out of scope (do NOT touch)
- Migrating sibling bots into new namespace.
- Cross-host UDS hot path.

## Definition of done
- [ ] assert_persona_namespace() runs at boot.
- [ ] coder_mailbox table with index on (to_persona, read_at).
- [ ] Empty-poll backoff: 1s -> 2s -> 4s -> 8s max.
- [ ] LILUSH_INSTALL_ROOT env required; boot fails fast if unset.

## PR title
feat(coder/topology): isolate coder namespace and mailbox

## Public API
from bot.coder.persona import assert_persona_namespace, list_personas, PERSONA_KEY_RE
from bot.coder.comm import Mailbox

## Threat model
Persona collision with sibling bots could corrupt shared state. Mitigation:
disjoint set assertion at boot and per-process install root env var.

## Fail-closed mode
If LILUSH_INSTALL_ROOT is unset or namespace assertion fails, process exits
with non-zero status and refuses to bind to Telegram polling.

---

## Files to create / modify
- bot/coder/boss/orchestrator.py
- bot/coder/boss/policy.py
- bot/coder/boss/lease.py
- data/schema.sql
- schemas/tasks.schema.json

## Hard rules (do NOT violate)
1. NO active-active Boss replicas; singleton via lease_until timestamp.
2. NO Boss writes to repo paths; only coder_artifacts.
3. NO infinite restart loop; max 3 within 30 minutes then safe mode.

## Out of scope (do NOT touch)
- Hot standby Boss.
- Worker-count tuning.

## Definition of done
- [ ] coder_boss_leases table with TTL renewal.
- [ ] boss_restarting flag suppresses cascade restarts.
- [ ] Boss restart re-validates lease, replays from snapshot.
- [ ] Planning artifacts validate against schemas/tasks.schema.json.

## PR title
feat(coder/boss): add singleton lease and bounded recovery

## Public API
from bot.coder.boss.orchestrator import Orchestrator
from bot.coder.boss.policy import Policy

## Threat model
Two Boss processes writing concurrently could split-brain the queue.
Mitigation: TTL lease in SQLite with renewal heartbeat.

## Fail-closed mode
On lease expiry without renewal, current process drains in-flight jobs
and exits; supervisor restarts a fresh process which re-acquires lease.

## Files to create / modify
- bot/coder/worker.py
- bot/coder/worker_pool.py
- tests/test_worker_pool.py

## Hard rules (do NOT violate)
1. Workers communicate via mailbox only.
2. No worker writes outside its persona artifact dir.
3. Worker count bounded by LILUSH_CODER_WORKERS env.

## Out of scope (do NOT touch)
- Cross-region failover.
- Hot standby workers.

## Definition of done
- [ ] worker_pool spawns LILUSH_CODER_WORKERS workers.
- [ ] graceful_shutdown drains mailbox before exit.
- [ ] worker_heartbeat ledger persisted to SQLite.
- [ ] crash_count exceeding threshold halts pool.

## PR title
feat(coder/worker): bounded worker pool with mailbox-only IPC

## Public API
from bot.coder.worker_pool import WorkerPool

## Threat model
Runaway worker count could exhaust memory or disk. Mitigation: env-bound
pool size, per-worker artifact dir quota.

## Fail-closed mode
On crash_count exceeding threshold within window, pool halts and emits
a Telegram alert; manual intervention required.
