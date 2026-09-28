# Edits so far

This document covers the project changes **after the September 10, 2026 import**
(`1015c9b`), through the September 28, 2026 history available when this file
was updated. The chronological inventory below includes **every post-import
commit on the current branch**; the detailed sections that follow explain the
most recent work. The repository records changes under both user and Replit
Agent author names, which are not reliable proof of who personally performed
each edit. To avoid leaving out assistant-assisted changes, the inventory
includes both, without claiming sole authorship. The original imported code
is not presented as a post-import edit. No secret values are included.

## Post-import change inventory

Each entry is a recorded change or publication event, in chronological order.
The short ID lets you inspect the exact per-file edit with `git show <ID>`.
Commit descriptions are concise history labels, not a claim that each feature
is currently enabled or safe to use. Several records below are reports,
tests, or fail-closed gates, **not** actual trading sessions.

### September 10 — workspace migration

- `a8219fb` — Added Colab research training workflow.
- `ca5c135` — Migrated the imported project into the pnpm workspace/artifact scaffold.

### September 11 — initial Replit deployment, access, data, training, trials

- `a34cc85` — Updated API server configuration and dependencies.
- `ac9c137` — Added Replit deployment troubleshooting notes.
- `cdb5557` — Hardened deployment authentication and health checks.
- `898e84c` — Configured the background stack for ephemeral local Redis.
- `16e79f5` — Restored trusted readiness data flow.
- `302b16e` — Added strategy control-room documentation assets.
- `01b1a21` — Displayed the active access role and hid controls beyond its authority.
- `2d369c5` — Connected fail-closed Alpaca SIP intraday market data.
- `2e2ad70` — Refactored backend API routes and trading schemas.
- `1ed09f5` — Completed reproducible model training and immutable paper activation history.
- `4800b90` — Added a migration to merge the stock database branches.
- `e1a3ac6` — Added forward-trial and lot-tracking infrastructure.
- `88b12f6` — Recorded a publication event; no source edit is attributed to the event.

### September 12 — evidence, monitoring, accounting, audit, learning

- `e4ccde7` — Added secure authenticated forward-trial browser coverage.
- `7585f4d` — Added stock promotion-readiness reporting.
- `7254ef3` — Updated stock training backend and model logic.
- `7616c9d` — Added continuous stock monitoring and database support.
- `4201099` — Restored fail-closed SIP preflight and operator-gated trial resume.
- `cbc139d` — Added paper trading ledger and backend monitoring logic.
- `297a4ef` — Protected promotion reports with append-only database triggers and migration tests.
- `a2dd48d` — Added the audit event chain and operational-hardening features.
- `5971b2c` — Governed the automated learning cycle with durable evidence, fail-closed gates, explicit lifecycle actions, and control-room visibility.

### September 13 — ledger, recovery, migration, background work

- `35280b9` — Updated Alembic revisions and learning-cycle task logic.
- `c7d439a` — Added broker activity tracking and ledger changes.
- `11e57cc` — Added stock recovery and migration changes.
- `c872efc` — Refactored stock ledger and recovery services.
- `81ad4fd` — Updated backend trading tasks and API routes.

### September 14 — bounded operations and regression protection

- `9fe993e` — Hardened accounting-residual recovery and required operator revalidation.
- `c8210b3` — Required monitoring evidence generated after a recovery pause.
- `182d1f0` — Bounded intraday backfill and one-minute task processing.
- `ec13e0b` — Hardened cycle authorization and scheduled challenger regressions.
- `6463b82` — Tested persistent monitoring degradation reaching authorized recovery.
- `a230cb9` — Added redacted, role-gated broker recovery evidence with digests.
- `e514828` — Displayed persisted monitoring-preflight blocks in recovery UI.
- `d8bd0dd` — Added automatic-recovery monitoring-boundary tests.
- `578d75c` — Showed the next eligible NYSE session when trial resume is blocked.
- `8743189` — Isolated one-minute imports in a dedicated Celery queue and worker.
- `80e8ad8` — Exposed bounded intraday repair backlog state.
- `3f5e7d9` — Added interrupted intraday poll recovery tests.
- `46526fd` — Added a recovery safety-notice browser regression.
- `239de30` — Showed persisted clear monitoring evidence in recovery UI.
- `85edd26` — Made recovery timestamp-boundary tests deterministic.
- `a90ebbb` — Aligned automatic-recovery responses across callers.
- `7316670` — Added DST and observed-holiday next-session tests.
- `3955996` — Showed the next eligible NYSE session in SIP preflight.
- `00bf0ff` — Added multi-session forward-paper evidence and immutable report retrieval.
- `54215f4` — Bounded forward-paper evidence and report history loading.
- `9256d24` — Tested authenticated byte-stable immutable report downloads.
- `1716676` — Bounded trial decision and metric history loading.
- `23b6322` — Tested cross-trial promotion-report download isolation.
- `c066775` — Added read-only missing-report download coverage.
- `508860d` — Added a bounded paper-only champion learning loop.
- `d9057c7` — Added durable scheduled paper-trial handoff.
- `065f0b2` — Added browser coverage for cycle promotion and recovery states.
- `24c6627` — Blocked trial resume on unresolved accounting and tested the route.
- `da8666e` — Checked the dedicated intraday worker in deployment monitoring.
- `99c684c` — Added Celery queue-isolation tests.
- `5ddcab7` — Added Redis-backed worker crash-recovery integration coverage.
- `7c0429e` — Added an operator pause for scheduled paper learning.
- `11f452a` — Added PostgreSQL concurrent handoff/idempotency coverage.
- `0fad35f` — Added the stock paper ledger UI and supporting tests/utilities.
- `1c79a41` — Halted active forward trials when accounting becomes unsafe.
- `706cada` — Tested operator boundaries for forward-trial lifecycle actions.
- `f62fcf7` — Added real Celery beat/worker crash recovery coverage.
- `4d32135` — Required Redis-backed crash-recovery validation.
- `e16f68b` — Deferred competing scheduled cycle admissions.
- `e9b56b3` — Tested concurrent scheduled handoff isolation.
- `9684f32` — Retried deferred scheduled cycles after paper trials resolve.
- `f10fec2` — Added a bounded autonomous paper-soak launcher, lineage report, and isolated-runtime verification.

### September 15 — controlled evidence, production and live safety boundaries

- `1bbbd6b` — Ran/enforced the controlled paper interruption matrix.
- `f4fcd41` — Added frozen forward-evidence readiness safeguards.
- `4fede81` — Added fail-closed live safety state, audited transitions, readiness checks, migration, documentation, and tests.
- `aa7222a` — Hardened production identity, authorization, credentials, and control-room mutations.
- `67b6413` — Added leakage-resistant point-in-time accuracy evaluation.
- `87324fb` — Governed challenger comparisons and rejection.
- `9d19366` — Added an isolated live broker boundary with guarded dispatch and reconciliation.
- `72020d3` — Added live risk gates and evidence-based recovery.
- `c39ca8b` — Added bounded, redacted live-operations evidence and control-room views.
- `11558ee` — Added controlled, bounded live-pilot approval safeguards.
- `73678d8` — Updated forward-trial functionality and frontend evaluation panel.
- `9e3df66` — Added production security identity and certification changes.
- `95ed679` — Added production security configuration and certification tests.
- `5faccab` — Added live-broker reconciliation certification logic and tests.
- `df94e81` — Added external account-activity certification and tests.
- `7ac30db` — Added live risk/recovery certification and reporting.
- `fbc36ba` — Added production operations certification and tests.
- `16bd3ef` — Restored fail-closed paper trading runtime and documented NO-GO evidence.

### September 16 — compliance reports, market data, Tradier boundary

- `a22a119` — Added live compliance-readiness service and documentation.
- `fb33a39` — Added stale-notification policy and initial paper report.
- `9bad3a1` — Added an API-server paper-deployment report.
- `8dcaaaf` — Updated operational hardening, certification tests, and documentation.
- `f46ac5a` — Verified/audited the Alpaca SIP entitlement preflight.
- `c1fdcec` — Updated backend settings and intraday data services.
- `06c9cd3` — Preserved paper residual halts when broker timestamps are unstable.
- `6b010ff` — Tested successful four-symbol SIP handoff.
- `94a2011` — Added Tradier paper ledger tracking and a report.
- `596d16c` — Aligned readiness with the active venue and separate complete-accounting gate.

### September 17 — venue evidence, exact approvals, research evaluation

- `ae4cd42` — Read-only assessment of Alpaca Paper candidate; recorded NO-GO.
- `2cc38bf` — Tested null Tradier history and fail-closed ledger entry points.
- `f2df926` — Added fail-closed Alpaca cost/timestamp qualification fixtures.
- `664900a` — Added paper-launch readiness report files.
- `35bd247` — Added autonomous paper-learning report files.
- `6189703` — Required complete paper-run approval before scheduled handoff.
- `28ff4d0` — Tested scheduled handoff with an unqualified broker.
- `eb79e82` — Added paper-execution evidence qualification reports.
- `8d0d2b5` — Updated paper-launch readiness reports.
- `bde8a2d` — Updated the autonomous paper-learning report.
- `64d3ad7` — Added read-only launch gates and fail-closed approval eligibility.
- `bc67da9` — Rejected blocked approvals using fresh prerequisites, blocker provenance, and artifact validation.
- `c3b30d1` — Updated paper ledger logic and tests.
- `abb5c87` — Updated the paper-execution qualification report.
- `73bfc6b` — Serialized approvals with cycle transitions and tested PostgreSQL races.
- `e583d00` — Enforced exact one-session paper bounds.
- `0e64a64` — Documented first-session NO-GO from fresh main-environment evidence.
- `23c359f` — Rechecked exposure caps at dispatch and audited denials.
- `3c978b5` — Showed expiry position handling and approved stop policy.
- `fec3e1f` — Showed per-symbol paper exit progress in cycle UI.
- `9777338` — Added agent research-run tracking and evaluation documentation.
- `509c85c` — Prevented stale disposition display after reconciliation refresh.
- `3cdca2a` — Added immutable exact-match shadow comparisons, paired metrics, provenance, history UI, and tests.
- `e30ed96` — Added managed-role shadow-research browser and API lifecycle tests.
- `eed2521` — Added an IBKR paper provider and an audit-disposition **proposal**, not approval.
- `d026e60` — Added another autonomous paper-learning evidence report.

### September 21 — account-specific venue qualification, sign-in, blocked session

- `939a668` — Updated database schema and local-stack configuration.
- `4e9cac8` — Added account-specific paper venue qualification and fail-closed activation gates; this did **not** accept or activate a venue package.
- `b2e2020` — Added main paper-readiness NO-GO report and an interim production script.
- `303097e` — Added production Clerk gateway, secured API boundary, control-room sign-in, tests, dependencies, and removal of the interim script (file-by-file details below).
- `3df9b67` — Added blocked/incomplete first approved-session report; no session was run.
- `e760764` — Recorded a publication event without a source-code diff; it does not establish current runtime readiness.

### September 28 — edit inventory

- `6fade93` — Added initial `EDITS_SO_FAR.md` and `.agents/agent_assets_metadata.toml` for the document card. This update expands the initial recent-work-only inventory.

For exact file lists and line-by-line edits rather than these summaries, use
`git show --stat <ID>` and `git show <ID>` on the entries above. To inspect the
aggregate tracked tree difference since import, use
`git diff --stat 1015c9b..HEAD`. The inventory covers commit history on this
branch; it cannot prove uncommitted, reverted-without-commit, external account,
database, or deployment changes absent from that history.

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