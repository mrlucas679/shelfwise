# Plan 004: Add relevance-aware bounded chat context

> **Executor instructions**: Preserve the 8k model context budget, tool grounding, tenant isolation,
> and fail-closed live behavior. Do not increase hard limits as the primary fix.
>
> **Drift check**: `git diff --stat 9c907b3..HEAD -- src/shelfwise_backend/app.py src/shelfwise_backend/chat.py src/shelfwise_backend/tools/mcp_surface.py tests/test_chat_state_bounding.py`

## Status

- **State**: COMPLETE (2026-07-13, local verification; live soak pending)
- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: plan 001
- **Category**: correctness / architecture
- **Planned at**: commit `9c907b3`, 2026-07-13

## Why this matters

To keep live prompts below 8k tokens, chat currently receives only two recent pending decisions, one
resolved decision, five learning events, six thresholds, and one trace
(`src/shelfwise_backend/app.py:2058-2062`). This solved overload but conflicts with README's
"whole store" claim: an older high-risk decision or SKU-specific event can be omitted even when it is
the best answer. The fix is deterministic relevance selection plus explicit coverage metadata, not a
larger prompt.

## Current state

- `_bounded_chat_decisions` at `app.py:2065` selects by status and recency.
- `_bounded_recent` at `app.py:2138` ignores question relevance and risk.
- Conversation history retains 12 messages at `app.py:975`.
- Agentic chat can call read-only tools and enforces calculator grounding.
- Live agentic failure receives one fresh network retry at `chat.py:225-250`; preserve this behavior.

## Commands

| Purpose | Command | Expected success |
|---|---|---|
| Focused tests | `python -m pytest -q tests/test_chat_state_bounding.py tests/test_chat_conversations.py tests/test_model_tool_calling.py` | all pass |
| Full gates | Plan 001 commands | all pass |
| Soak | `python -m shelfwise_eval.full_system --duration-seconds 900 --live-required --output-dir reports/chat-relevance-soak` | PASS |

## Scope

**In scope**: chat context selection, filtered read-only decision/learning tools, coverage metadata,
and tests.

**Out of scope**: vector databases, larger model context, changing decision persistence, removing the
live retry, or allowing ungrounded answers.

## Steps

### Step 1: Introduce a deterministic context selector

Create a small module that scores tenant-scoped candidates using exact SKU/product/location matches,
question terms, pending status, risk tier, exposure, and recency as the final tie-breaker. Return a
bounded selection plus counts describing total, matched, and omitted rows. Keep pure functions and
stable ordering so tests are deterministic.

**Verify**: unit tests with 100+ decisions select an older matching high-risk item over unrelated
recent items and never cross tenants.

### Step 2: Expose bounded filtered tools

Extend decision and learning read tools with validated query/filter/limit inputs. Enforce a small
maximum limit and return aggregate counts with rows. Do not expose raw storage objects or permit
writes. Ensure the authenticated tenant override still wins over model arguments.

**Verify**: tool tests cover SKU match, status/risk filters, maximum limit, empty match, and tenant
override.

### Step 3: Build prompt context from relevance, not recency alone

Use the selector for decisions, learning events, thresholds, and traces. Include coverage metadata so
the model can say when details were omitted and call a tool. Preserve compact field projections and
the existing context ceiling. Add a deterministic prompt-size estimator/gate before network calls.

**Verify**: worst-case fixture stays below the configured input budget and contains the targeted row.

### Step 4: Add answer-completeness tests and soak scenarios

Add unseen questions for an old critical decision, a SKU-specific learning event, no-match behavior,
and "highest risk across the store." A response must either use the relevant evidence/tool or state
that the bounded context is insufficient; it must never confidently claim the omitted item does not
exist.

**Verify**: focused tests and the 15-minute live soak pass with zero 400/context errors.

## Test plan

- Selector unit tests for relevance, risk, deterministic ties, bounds, and tenant isolation.
- Agentic tests proving tool escalation when prompt context omits details.
- Regression test retaining the 8k budget under thousands of records.
- Live 15-minute harness with targeted questions mixed into generated cycles.

## Done criteria

- [ ] Older relevant high-risk records beat unrelated recent records.
- [ ] Prompt input is deterministically bounded below the model limit.
- [ ] Coverage/omission metadata is available to the model.
- [ ] Tenant and grounding tests pass.
- [ ] Fifteen-minute live soak passes without context failures.

## STOP conditions

- Selection requires embedding/model calls before the main model request.
- A proposed shortcut leaks cross-tenant candidate metadata.
- Prompt sizing cannot guarantee room for tools and output tokens.

## Maintenance notes

Review selector weights when new decision types or risk fields are added. Stable deterministic
selection is more valuable here than opaque semantic ranking.
