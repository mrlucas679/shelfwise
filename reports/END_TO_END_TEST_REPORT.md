# End-to-end test report

Audit date: 2026-07-29
Branch: `developers`
Scope: ShelfWise only

## Final local gates

| Gate | Result |
|---|---|
| Ruff (`src`, `tests`, `scripts`) | Passed |
| Capability generation and drift check | 249 records; passed |
| Full Python suite | 951 passed, 21 skipped, 1 deprecation warning |
| Frontend TypeScript | Passed |
| Frontend production build | Passed; 288 modules transformed |
| Chromium Playwright suite | 12 passed |
| Python dependency resolution | `pip check` passed |
| Node production advisories | 0 vulnerabilities |
| Database/schema/config subset | 32 passed, 11 Postgres-environment skips |

The first full-suite attempt lost the shared Windows pytest temp directory during execution:
909 tests had already passed, then 42 fixture setups raised `FileNotFoundError` for the missing
temp root. No test removed that root. The complete suite was rerun with
`--basetemp=tmp/pytest-final` and passed 951/951 executed tests. This is recorded as an
environment retry, not hidden or counted as an application failure.

## Browser workflows

The browser suite started disposable ShelfWise servers on backend port 8017 and frontend port
5187 so the unrelated MediChain service on the normal Vite port was not touched.

1. Chat console and approval queue navigation.
2. Staff account activation from the signed invitation fragment.
3. One-time first-owner setup.
4. Resumable guided onboarding through readiness.
5. Receiving-exception count and delivery badge agreement.
6. Every populated simulation workspace opens from navigation.
7. Adaptive attribution appears through the existing trace registry.
8. Golden decision approval clears the queue and records the outcome.
9. Grounded chat answer uses live ShelfWise tools and real seeded data.
10. Owner self-serves an ERP credential connection.
11. Owner self-serves a camera/sensor credential.
12. Owner self-serves a signed till webhook endpoint.

All 12 passed. Windows emitted `ConnectionResetError [WinError 10054]` messages while Playwright
terminated its disposable Uvicorn server after the successful run. There was no failed browser
assertion or running application outage.

## Exact-head GitHub evidence

Implementation commit `abe6924` passed all four workflows triggered for its SHA:

- [Push CI](https://github.com/mrlucas679/shelfwise/actions/runs/30484271638):
  971 tests passed, 1 skipped, 12 browser flows passed; fresh Postgres and Redis, wheel import,
  frontend build, Compose validation, production topology, public-origin smoke, deployment
  shakedown, and fail-closed Track 3 gate passed.
- [Pull-request CI](https://github.com/mrlucas679/shelfwise/actions/runs/30484275094): passed.
- [Push Capability Contract](https://github.com/mrlucas679/shelfwise/actions/runs/30484272250):
  passed.
- [Pull-request Capability Contract](https://github.com/mrlucas679/shelfwise/actions/runs/30484275116):
  passed.

## Coverage boundaries

- The 21 full-suite skips are environment-gated; 11 are explicitly Postgres integration checks
  without `SHELFWISE_TEST_DATABASE_URL`.
- Local Docker inspection timed out on the resource-constrained workstation, so no fresh local
  image build is claimed.
- GitHub Actions supplied the clean Postgres/Redis/wheel/Compose/deployment evidence above.
- Direct MI300X/Fireworks generation, hardware training, retailer connector acceptance, and
  production rollback drills require external systems or credentials and are not represented
  as local passes.
