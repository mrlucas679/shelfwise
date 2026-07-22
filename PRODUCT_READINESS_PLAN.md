# ShelfWise Product Readiness Plan — From Proven Codebase to Sellable Product

Date: 2026-07-23 · Branch: `developers` · Author basis: repo audit (`IMPLEMENTATION_STATUS.md`,
`Things that needs to be implemented.md`, current diff), the engineering-library standards
(`CODING_STANDARDS.md`), and external market research (cited at the end).

## The honest starting position

The engineering is not the gap anymore. The application inventory shows 214 contract-verified
capabilities, ~761 tests green against real Postgres/Redis, nine connectors, RLS tenancy, HITL
governance, deterministic decision math, and a fleet-scale run at ~21k rows/s. What has **never
happened** is the thing that makes a product real:

**No real shop's real data has ever flowed through this system, and no real shop owner has ever
acted on one of its recommendations.**

Everything else in this plan is ordered around closing that one gap first, because every book in
the library says the same thing in different words: feedback from reality beats internal proof
(XP's feedback value; Pragmatic Programmer's tracer bullets; User Stories Applied — the story is
a placeholder for a conversation with a real user we have not yet had).

## What the market research says (why this product, why now)

- Purpose-built grocery inventory systems credibly claim 20–30% food-waste reduction and
  measurable shrink reduction from expiry/rotation visibility — that is the outcome category
  ShelfWise's batch/FEFO/expiry math already computes, with evidence receipts the incumbents
  don't have.
- Incumbent SMB pricing anchors: Lightspeed Retail $89–289/month; Square/Clover free–$40/month
  with shallow inventory. None of them offer evidence-gated AI decisions with HITL approval.
  ShelfWise's differentiator is not "AI chat" — it is *auditable decisions with money attached*.
- South African independents (the first market): net margins 8–15% before load-shedding losses;
  spoilage and theft are named top risks; technology adoption is limited by cost, connectivity,
  and skills. Implication: the first-shop product must be **cheap, low-bandwidth-tolerant,
  near-zero-training** (the chat-first UI is the right call), and must prove its value in rand
  recovered per month, not in features.

## Product thesis (one sentence)

For an independent grocery/convenience retailer, ShelfWise watches every SKU, batch, and fridge
and puts a short, ranked, evidence-backed list of money-saving actions in front of the owner —
who approves each one — and then proves the rand value it recovered each month.

The pitch metric to a multimillion-rand client later is the same metric the single shop sees:
**value recovered per month vs. subscription cost**, with an audit trail. Build the measurement
now; it is both product feature and sales collateral.

---

## Phase 0 — Stabilize and stand the product up for real (days)

Goal: a deployed, live-model, operator-ready instance. Nothing new; finish what's staged.

1. **Land the in-flight work.** ~1,050 uncommitted lines across 29 files plus two new boundary
   test suites are sitting on the working tree. Get the full gate green, review against
   `CODING_STANDARDS.md`, commit to `developers`.
2. **Recreate the production host and pass the live-model acceptance.** The MI300X/AMD-cloud
   droplet is destroyed; `docs/mi300x-recreate-runbook.md` + `DROPLET_BOOTSTRAP.md` are the
   sequence, `scripts/track3_prescreen.py` + one live agentic cascade are the acceptance gate.
   Until a live model answers over public HTTPS, there is no product to put in front of anyone.
3. **Decide the sustainable inference economics.** An always-on GPU for one shop is not a
   business. The architecture already reserves LLM calls for ranked exceptions; measure and
   record **cost per decision** and **model calls per 1,000 SKUs per day** on the live stack.
   Owner decision needed: pilot-phase provider mix (AMD cloud hours vs. Fireworks fallback as
   primary for the pilot) — this is a cost decision, not an architecture change.

Exit criteria: green CI on `developers`, live public deployment answering a real cascade, a
written cost-per-decision number.

## Phase 1 — One real shop (weeks 1–4): the design-partner pilot

Goal: one shop using it weekly, with measured value. This phase *is* the product validation.

**1. Pick the shop deliberately.** Ideal profile: independent grocery/convenience store already
on Yoco or Lightspeed/Square (a connector we have), 500–5,000 SKUs, meaningful perishable
share (dairy/bread/produce), owner willing to meet 30 min/week. Offer: free during pilot, in
exchange for feedback and a case study. One shop, not three — feedback depth beats breadth.

**2. Onboarding runbook, then measure it.** Write the operator-side runbook for taking a shop
from zero to live: tenant creation, owner login, connector credentials or CSV import of product
master + stock + 90 days of sales, twin onboarding of fridges/fixtures, policy defaults per
category. Target: **first useful recommendation within 48 hours of getting the data.** Every
manual step in that runbook is the Phase 2 automation backlog, discovered from reality instead
of invented (Use-Case 2.0: slice by verifiable increments).

**3. Two weeks shadow mode.** The system ingests live data and produces recommendations, but the
owner treats them as read-only. We measure, per week:
   - useful-actions surfaced (owner says "I'd do that") vs. noise (precision, not volume)
   - false urgency rate and duplicate rate (the alert-fatigue metrics already specced)
   - data-quality holes the shop actually has (missing expiry capture will dominate — the
     data-completion task type is about to earn its keep)
   - rand value at risk identified
4. **Two+ weeks live HITL.** Owner approves/rejects from the queue; learning loop runs; we
   measure **value recovered** (markdown before waste, stockout avoided, cold-chain saves) and
   time the owner spends per week (must be minutes, not hours).

**5. Weekly correction loop.** Every wrong recommendation becomes either a policy fix, a Critic
rule, a data-completion task type, or an eval scenario — through the existing workflow slots
(new candidate type / policy / Critic rule / UI view), never a new architecture. The coverage
rule in `Things that needs to be implemented.md` stays binding.

Exit criteria (the "works for one shop" bar): 4 consecutive weeks of live use; ≥70% of surfaced
actions rated useful; a signed-off rand figure for value recovered in month one; onboarding
runbook executable in under a day.

## Phase 2 — Productize what the pilot exposed (months 2–3)

Run these tracks in parallel only after the pilot is live; their content will be re-prioritized
by what the pilot actually hurt on.

**A. Self-serve onboarding.** Turn the runbook into product: guided CSV import with mapping
wizard and validation preview, connector connect-flows with credential checks, category policy
templates ("dairy", "bakery", "ambient") the owner confirms instead of configures. Target:
a competent owner onboards without us on a call.

**B. Multi-user reality.** Per-person staff accounts (already the recorded roadmap after the
owner account), role queues (cashier vs. manager vs. procurement), and the approval matrix the
HITL layer already models. One shop = several people on day one.

**C. Operate like a product, not a project.**
   - Automated backups with a *tested restore drill* (a backup that has never been restored is
     a hope, not a backup), documented RPO/RTO.
   - Uptime monitoring + alerting to us, not just `/health` for us to remember to check.
   - Versioned release process: staged deploy, rollback, migration double-apply already tested.
   - Support channel + incident log, even if it's one WhatsApp number and a spreadsheet at
     first — what matters is response-time discipline.
   - **POPIA compliance pass** (South Africa's data-protection act): data inventory, processing
     purpose, retention, breach procedure. Sales data is low-PII by design — keep it that way
     (PII minimization is already a stated rule; make it a checked gate).

**D. Commercial wrapper.** Terms of service, privacy policy, a one-page offer, and pricing to
test in the next phase: anchor **R699–R1,499/month** (≈$39–79, undercutting Lightspeed's $89
entry while claiming an outcome the POS can't), with the monthly value-recovered statement as
the retention engine. Billing can start as manual invoicing — do not build a billing system
before there are payers.

## Phase 3 — Repeatable sales (months 3–6): 5–10 paying shops, then up-market

1. Convert the pilot into a **case study with real numbers** (waste % down, rand recovered,
   minutes/week). That document is the entire early sales kit.
2. Sell to 5–10 shops in the same segment before touching bigger clients — repeatability at one
   segment beats breadth (each new shop must reuse the same onboarding path; any per-shop custom
   work is a product bug to fix, not a service to bill).
3. Only then pitch mid-market/multi-store groups. The multitenancy, RLS, per-tenant scheduling
   seams, and the 10K-user scale decisions on record mean the codebase is ready to *grow into*
   that pitch; the case-study proof is what was missing, not the architecture.
4. Enterprise conversations will demand: SSO, uptime SLA, pen-test report, data-processing
   agreement, and a reference deployment — put them on the roadmap the day the first mid-market
   lead asks, not before (avoid speculative generality; the library is unanimous on this).

## Engineering standards thread (applies to every phase)

- Every pilot-driven change goes through the existing workflow slots — new candidate type,
  policy, Critic rule, tool, task type, or view — before any architecture change is considered.
- Keep the two-codebase truth discipline: a thing is "done" when it runs in `src/`/`frontend/`
  with tests, never when a document says so.
- Failure paths first (Writing Effective Use Cases): every onboarding and connector flow gets
  its failure/alternate paths enumerated and tested, because the pilot shop's data *will* be
  dirty in ways the synthetic world was not.
- Reliability, scalability, maintainability stay separately checked (DDIA) — the pilot pushes
  reliability work (backups, monitoring) ahead of further scale work; don't let 500k-row
  benchmarks crowd out a restore drill.
- No temporary fixes; no AI attribution; free/MIT-clean dependencies; cloud inference only —
  all standing rules unchanged. Note one standing-rule tension for an explicit owner decision:
  "never add anything that bills" was a hackathon constraint; a production pilot requires paid
  hosting/GPU-hours. That is a deliberate owner-approved spend, tracked in the procurement
  appendix — not a rule violation by stealth.

## The single most important number

From the first live week onward, the app must be able to answer: **"How much money did
ShelfWise recover for this shop this month, and can you show the receipts?"** The decision
economics, accountability joins, and learning events already exist to compute it. Elevating it
into a monthly owner-facing statement is the highest-leverage product feature in this plan —
it is simultaneously the retention mechanism, the pricing justification, and the pitch deck.

## Market research sources

- [Ply — grocery inventory management outcomes](https://www.getply.com/blog/grocery-store-inventory-management-software/)
- [FitGap — grocery inventory software landscape 2026](https://us.fitgap.com/search/inventory-management-software/grocery-stores)
- [ConnectPOS — top grocery inventory systems 2026](https://www.connectpos.com/grocery-store-inventory-management-software/)
- [ITQlick — Lightspeed Retail pricing 2026](https://www.itqlick.com/lightspeed-retail-pos/pricing)
- [Vecosys — best SMB POS systems 2026](https://www.vecosys.com/best-pos-systems-small-business-2026/)
- [TechFinancials — spaza shops' economic impact](https://techfinancials.co.za/2026/05/12/the-spaza-shops-invisible-impact-on-the-south-african-economy/)
- [Business Day — spaza economics and margins](https://www.businessday.co.za/opinion/2026-07-07-vusi-vokwana-the-spaza-myth-how-black-south-africa-is-being-sold-a-counterfeit-economy/)
- [Zawya — spaza regulation vs. practicality](https://www.zawya.com/en/economy/africa/south-africa-regulating-spaza-shops-policy-versus-practicality-mpynuesa)
