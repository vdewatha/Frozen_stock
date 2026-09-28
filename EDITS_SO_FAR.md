# Edits so far

This document records the edits made in the recent paper-readiness, production
sign-in, and first-session work (September 21, 2026). It is based on the
workspace history for those work packages and the resulting reports. It is not
an inventory of every change ever made to this project by every contributor.
No secret values are included.

## Main-environment paper readiness

| File | Edit |
| --- | --- |
| `artifacts/api-server/backend/reports/main-paper-readiness-20260921-task218.md` | Added a redacted NO-GO report: read-only preflight results, failed venue/account/recovery/audit gates, the SPY-only default versus the required four-symbol cycle, and prerequisites for approval. |
| `artifacts/api-server/backend/scripts/run_production_api.sh` | Added a temporary production launch script while diagnosing deployment startup, then removed it after replacing that launch path with the production authentication gateway. It is **not** part of the current tree. |
| `artifacts/api-server/package.json` | Changed the production build to compile Python and bundle the Node gateway; changed `start` to run that gateway instead of the local stack script. Added the gateway's Clerk and proxy dependencies. |

The report did not approve a run, create a cycle, initialize an account,
reconcile broker balances, clear the kill switch, switch providers, or trade.

## Production sign-in and API boundary

| File | Edit |
| --- | --- |
| `artifacts/api-server/src/production-server.ts` | Added the public Node gateway. It validates Clerk sessions, strips incoming internal-identity headers, signs a short-lived identity for the loopback FastAPI process, defaults new users to `viewer`, and permits higher roles only via an explicit server-side mapping. The child process is forced to paper-only mode. |
| `artifacts/api-server/src/middlewares/clerkProxyMiddleware.ts` | Added production Clerk frontend-API proxy handling. |
| `artifacts/api-server/backend/app/core/config.py` | Added Clerk-gateway configuration and production validation; allowed the existing Tradier paper account ID to serve as the paper account binding when appropriate. Kept the existing separate production-identity mode. |
| `artifacts/api-server/backend/app/core/security.py` | Added verification of fresh, signed internal gateway identity headers in production; unauthenticated or forged identity remains rejected. |
| `artifacts/api-server/backend/app/api/routes.py` | Updated the public authentication configuration response to classify `clerk_gateway` as requiring an identity provider; the response already exposed the configured mode. |
| `artifacts/api-server/backend/tests/test_production_security.py` | Added a regression test for signed gateway identity and rejection of a forged elevated role. |
| `artifacts/strategy-control-room/package.json` | Added Clerk React/theme dependencies. |
| `artifacts/strategy-control-room/public/logo.svg` | Added the control-room logo used on the sign-in pages. |
| `artifacts/strategy-control-room/src/App.tsx` | Added the Clerk provider, branded sign-in and sign-up routes, and base-path-aware routing. |
| `artifacts/strategy-control-room/src/lib/api.ts` | Enabled cookie-based authenticated API requests for gateway mode while retaining local role-key behavior for local development. |
| `artifacts/strategy-control-room/src/pages/home.tsx` | Added a public landing state, automatic session loading after Clerk sign-in, and Clerk sign-out handling. The interface states that a new account has read-only access. |
| `pnpm-lock.yaml` | Updated dependency resolution for the new packages. |
| `.agents/memory/MEMORY.md` and `.agents/memory/production-clerk-gateway.md` | Recorded the security-boundary decision for later project work; these are agent memory notes, not runtime code. |

Replit-managed Clerk was provisioned as part of this work. The workspace
contains the required Clerk environment configuration; credential values are
not recorded here. An explicit operator/researcher/admin allowlist was **not**
configured. A sign-in does not authorize orders or a paper run.
The project history also records a subsequent publish action with no source
file changes; this document does not independently verify the published
runtime or its sign-in flow.

## First approved-session attempt

| File | Edit |
| --- | --- |
| `artifacts/api-server/backend/reports/first-approved-paper-session-20260921-task219.md` | Added an explicit **BLOCKED / INCOMPLETE** execution report after a fresh read-only preflight. It records no cycles, no paper-run approvals, no venue qualification or activation, an uninitialized account, failed risk/recovery/audit gates, no session outcomes, and no eligible learning results. |

No trial was activated, no legacy twenty-session trial was reused, and no
orders or fills were generated. The report is not a completed paper session or
evidence of model improvement. The first approved-session milestone still
requires substantive readiness, exact future-session authorization, bounded
execution, reconciliation, and later eligible labels.

## Verification performed at the time

- The focused production security, secondary approval, paper venue
  qualification, and migration tests passed (27 tests).
- API and control-room builds and TypeScript type checks passed.
- A production-mode gateway smoke check returned healthy paper-only API status;
  a forged internal admin identity received HTTP 401.
- `git diff --check` passed.
- The local development workflows were restarted and the login screen was
  visually inspected. These checks do **not** prove a later published deployment
  or a trading session is healthy.

## Current limits

The readiness and session reports reflect observations on September 21, 2026.
They should not be treated as a fresh assessment of the present environment.
Any future paper activation must first rerun exact-universe and cycle-specific
gates, obtain the required venue/account/audit approvals, and record the exact
one-session authorization through the application workflow. This document
does not grant that authorization.