# Dead-code report

Audit date: 2026-07-29

## Removed

| Component | Evidence it was dead | Change | Verification |
|---|---|---|---|
| Second assignment to `totals["agentic_executions_by_cascade"]` in `shelfwise_eval.full_system._build_report` | It immediately overwrote the same key written by the preceding sorted assignment, had no intervening read, and discarded deterministic ordering | Removed the redundant overwrite; retained the canonical sorted receipt | Full harness tests included in 951-test pass; isolated browser suite 12/12 |

No module, route, agent, tool, worker, schema object, migration statement, dependency, frontend
surface, or asset was deleted merely because static analysis could not see a reference.

## Reachability checks performed

- `git ls-files`: 536 tracked files; zero tracked zero-byte files.
- Ruff `E,F,I,B,UP,SIM,RUF`: no unused-import or unreachable-quality findings.
- Capability discovery: 249 source-backed records; no declaration-only records.
- OpenAPI discovery: 141 method/path pairs from `app.py`, intelligence/multimodal routers, and
  every `routes_*.py` module.
- Registry checks: 11 agents, 22 tools, 9 event types, 9 consumers, 3 workers, 9 connectors,
  14 frontend surfaces, and 9 workflows remain registered and test-linked.
- Repository reference scans covered imports, route decorators, registries, event branches,
  workers, scripts, Compose, GitHub Actions, frontend navigation, feature flags, schema DDL,
  and tests.
- No commented-out function/class implementation was found in production source.

## Investigated and retained

| Candidate | Why it is not dead |
|---|---|
| Empty exception classes (`InferenceError`, `PreflightFailure`) | Typed public failure contracts used by callers/tests |
| `pass` after numeric/date parsing failures | Deliberate fall-through to the next parser or safe fallback |
| `NotImplementedError` in connector base classes | Required abstract-method enforcement |
| `SHELFWISE_WORLD_MODE=continuous` rejection | Explicit unsupported experimental reservation; it fails loudly and directs users to the implemented full-system rotation |
| Module-local `_ensure_schema` paths | Zero-config/local compatibility; production migration uses the canonical schema and disables unsafe superuser use |
| Edge-device and webhook tables without RLS | Intentional pre-tenant identity lookup; tenant-scoped management queries and encrypted/HMAC secrets enforce the boundary |
| Historical plan/audit documents | Provenance and evidence history; current truth is clearly identified in `IMPLEMENTATION_STATUS.md` and the generated manifest |
| Deterministic cascades beside agentic proof routes | Supported normal ingest path and live-required proof path have different responsibilities; neither duplicates the other |

## Safe-removal conclusion

Only one component met the deletion standard in this pass. Removing additional candidates would
either break supported dynamic paths, erase schema/history compatibility, or confuse deliberate
simulation/live boundaries with dead code.
