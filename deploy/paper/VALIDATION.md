# Validation

## 2026-10-05: Invalid Pending Forecast Isolation

The online learner now validates pending forecast payloads before attempting to
score them. A malformed pending row is marked `invalid` and cannot contribute
to training, metrics, shadow returns or return-challenger summaries; valid rows
for other symbols continue through the same scoring pass. The cohort reporter
also handles malformed payloads without raising. Seven corruption variants
were reproduced before the fix; the focused research, return and observation
suite passed **86 tests** after the fix. The rebuilt backend was deployed to the
local paper-only Compose runtime and `/api/health` returned `paper_only=true`
and `live_trading=false`; the paper account remained halted with no positions.

## 2026-10-05: Oracle Access Preparation, Deployment Not Yet Created

With the user's explicit approval, generated a dedicated Ed25519 deployment key
under `.local/oracle/frozen-stock-paper` (private file mode 0600, parent 0700).
Both key paths are ignored by Git. The existing trading VCN's default security
list was changed from world-accessible TCP/22 to the owner's then-current public
IPv4 /32. The saved security-rules table was read back and confirmed; all other
rules were left unchanged. Evidence: `output/oracle/ssh-restricted.png`.

The unsubmitted instance form used Ubuntu 24.04 Minimal aarch64,
VM.Standard.A1.Flex with 2 OCPUs / 12 GB, and the existing trading subnet.
The public key in that form also included a matching `from` source restriction.
The account UI confirmed Free Tier and zero A1 usage against limits of 2 OCPUs
and 12 GB. The root-compartment block-volume list was empty, but boot-volume and
account-wide storage verification was not completed. The estimator displayed
$2/month for the default boot volume; this has NOT been established as a charge
or waived allowance. No instance submission or paid-account upgrade occurred.

Both Oracle tabs subsequently redirected to sign-in. Reauthentication is needed
before verifying remaining storage allowance, home region, final cost and host
capacity. Recheck the source IP before reuse; a changed address will require an
explicit firewall/key restriction update. No server connectivity, remote restore,
24/7 hosting, or cloud application deployment is claimed. Local runtime and
paper-only trading safety gates were not changed during this setup.

## 2026-10-04: Combined Regression and Bounded Audit Verification

The initial combined backend run had 1,293 passes and one queue-isolation test
failure at a two-second result deadline. The isolated test passed, but a wider
rerun still failed to start its general task after timing headroom was added.
The test now uses a dedicated in-memory Celery app with production routing and
real task bodies instead of mutating a previously used global application's
broker/backend configuration. Bounded waits still require the intraday task to
finish while the general task remains blocked. Production deadlines, leases and
queue routing were not changed.

Browser validation exposed backend restarts and HTTP 502s during a burst of
per-cycle launch prerequisite requests. Audit verification loaded the entire
history into memory for each request. It now iterates every event in batches of
100 using [SQLAlchemy yield_per](https://docs.sqlalchemy.org/en/20/orm/queryguide/api.html#fetching-large-result-sets-with-yield-per),
closing the cursor even when detecting a breach. No event is skipped, cached or
rewritten. A regression test verifies all 250 events with at most 101 loaded
audit ORM objects and detects tampering after the second batch. The restart's
exact OS-level cause was not established; no OOM diagnosis is claimed.

Deployed backend manifest
`48b6fa07f8b8d794aaf16582b644a585bd87f4ffa5abad1ec65470adf763daae`.
The build resolved Python base digest
`6f31d6e9ba2b0a787a3f81c37b004155b87b9efa1b771182bd550c1615745be5`;
the validation image was rebuilt from this production image before final tests.

Subsequent browser checks exposed a stale collector snapshot, then actual HTTP
429s from the existing 120/minute per-identity limit. The smoke harness now spaces
browser API requests by 750 ms without replacing responses or retrying failures.
Production rate limits remain unchanged. Final validation passed all 27 sections
at 1440/390px, automatic viewer access, write protection, temporary permissions,
market-data controls and zero runtime page errors. Backend restart count remained
zero and health passed during this sweep. A sampled memory reading was 312.6 MiB;
this is not a peak measurement or capacity certification.

Final full backend suite: **1,295 passed, 203 subtests passed, one skipped**, 28
existing warnings, **423.96 seconds**. The skip is the SQLite variant of the
PostgreSQL row-lock test. All **31 frontend component tests** and TypeScript
checking passed. Disposable validation services were removed; runtime services
remain running. Health confirms paper-only mode and live trading disabled.
No orders were submitted and no profitability or launch qualification is claimed.

## 2026-10-03: Launch Audit and Verified Transport Review

Read the actual launch, research and paper-account APIs. The collector was
fresh, but research qualification was stale following an isolated broker-read
failure. Ran the existing two-reconciliation transport review in preview mode
(events 9144/9142), then applied it with fresh reconciliations 9148/9146. Both
passed the unchanged previously reviewed journal requirement. The actual status
API then confirmed qualification_status=current, qualified_for_observed_start=true,
activation_authorized=false, execution_authorized=false, costs_known=false and
status=halted. No safety gate was cleared and no orders were submitted.

The broader acceptance audit is recorded at the top of
UNFINISHED_TASK_SOLUTIONS.md. It distinguishes closed integration probes from
strategy qualification, 24 cash-only economic observations from actual profits,
and local reliability work from unestablished remote hosting/recovery. Alpaca's
paper clock confirmed next open October 5 at 09:30 Eastern. No application code
changed in this turn, and no new test-suite run is claimed.

## 2026-10-03: Preserve Research Observation Times Across Refreshes

Found unchanged research-bar re-fetches overwriting ingested_at, causing
previously on-time outcomes to expire if the learner resumed after its deadline.
Three new regression cases failed before the fix. Identical OHLCV and provenance
now retain the existing observation time for Alpaca IEX and delayed SIP research
feeds. Any changed bar keeps the new ingestion time, so late corrections cannot
inherit earlier eligibility. Tradier execution-feed refresh semantics are
unchanged. Existing overwritten timestamps are not backdated or reconstructed.

Focused validation passed **150 tests and 13 subtests**, with three existing
warnings, including a simulated collector refresh before delayed learner
resumption, late corrections, both research feeds and unchanged Tradier behavior.
The full suite was not rerun for this change.
Deployed backend manifest
`45c53a146f507c414b2b182f73c64add7350635de24c983e3078726c71dcaad4`.
A read-only runtime check sampled 20 bars per symbol, then observed the scheduled
collector advance from 2026-10-04 02:06:00.328147 UTC to 02:07:00.031455 UTC.
All 80 unchanged bars retained their observation times. This verifies the
deployed collector path without manually submitting a collection or order.
Health confirms paper-only mode and live trading disabled. This fix preserves
future evidence; it does not establish profitability or restore lost history.

## 2026-10-03: Validate Direction Outcomes Before Learning

Six regression cases reproduced direction reporting trusting inconsistent
labels, malformed feature vectors, invalid probabilities, changed losses,
missing loss fields and invalid closing prices. Two cases crashed the report;
others incorrectly contributed metrics. The shared known-outcome check now
validates bounded finite features/probabilities, positive prices, binary labels
consistent with observed prices, and recomputed model/baseline losses, in
addition to existing temporal checks. Training and headline metrics exclude
invalid records without rewriting history. The API counts excluded records as
invalid_scored_observations within the latest scored-record window.

Focused research tests passed **109 tests**, with one existing dependency
warning. No full-suite rerun was performed for this change.
Deployed backend manifest
`3c2749f3890318d1109e1e8feeadc20e2abdcfc23d224219ba4a778e8cb904ad`.
Initial API/browser checks started before deployment completed and failed.
After Compose completed and backend health passed, both reruns succeeded.
Actual/fixture browser checks covered desktop/mobile cost selection and sample
reporting with no page errors or overflow.

All **1,264** existing scored forecasts passed validation: AAPL 334, MSFT 318,
QQQ 281, SPY 331. Respective Brier scores were 0.2500072602, 0.2515023458,
0.2511926395 and 0.2511610123, all above the neutral benchmark of 0.25.
This does not establish forecasting improvement or profitable trading.
Health confirms paper-only mode and live trading disabled; no orders submitted.

## 2026-10-03: Reject Ambiguous Activity History

Reviewed Alpaca's [activity pagination and creation-date contract](https://docs.alpaca.markets/us/reference/getaccountactivities-2)
and [separate regulatory-fee posting](https://docs.alpaca.markets/us/docs/regulatory-fees).
Next-day fee creation means a missing fill commission is not complete zero-cost
evidence. No cost-completeness or trading gate was relaxed.

Found and fixed activity ingestion accepting malformed object responses as
empty history and silently discarding changed duplicate IDs. Exactly one
supported envelope key must now contain a list; bare lists remain supported.
Identical overlaps are deduplicated, but conflicting copies are rejected before
any history is returned. Initial regression run had eight failures and four
passes. Final focused validation passed **172 tests and six subtests**, with
two existing warnings. This includes a persisted reconciliation case proving
that a malformed later page halts without importing the earlier page, changing
the frozen baseline, or advancing the last successful reconciliation time.
The full suite was not rerun for this change; its preceding run is below.

Deployed backend manifest
`9fbb7c48ef417344e0d8aadfbbcb2c62839bed5c8486a5cb119e4e0ae80588df`.
A read-only request through the deployed Alpaca paper adapter returned 14
activities: six FILL, seven FEE, one JNLC. Reported fee subtotal remains $0.07;
all six fill commissions are absent. No raw descriptions, credentials or account
identifiers were emitted. No orders were submitted. Health confirms paper-only
mode with live trading disabled. The disposable validation stack was removed.

## 2026-10-03: Reject Nonfinite Research Returns

Three new regression cases failed before the implementation: finite input
prices produced infinite basis-point returns, and three valid 1e308-bps
observations overflowed a naive sum before averaging. Shadow scoring now
validates the computed gross and cost-adjusted returns before recording them.
Invalid observations remain unavailable rather than entering training or being
clipped into apparently valid targets. Return-challenger reporting also rejects
nonfinite prediction errors. Both reports use standard-library statistics.mean
to avoid intermediate sum overflow. No strategy threshold or admission gate
changed, and this is not economic plausibility validation for extreme prices.

Primary references reviewed: [scikit-learn finite-input validation](https://github.com/scikit-learn/scikit-learn/blob/main/sklearn/utils/validation.py)
and [CPython 3.11 statistics implementation](https://github.com/python/cpython/blob/3.11/Lib/statistics.py).
No additional dependency or copied external implementation was needed.

Focused research validation passed **73 tests**. The full isolated backend suite
passed **1,270 tests and 203 subtests**, with one skip and 28 existing warnings,
in **423.97 seconds**. It included real PostgreSQL ownership checks and the
full-duration Redis crashed-worker lease test. The validation stack was removed
without touching runtime volumes.

Deployed backend manifest
`5a2eaf24358768484cb872de5ba9d80ebcf6c1a0687e6d8eb0b81ad599c9da23`.
The deployed research API retained 983 shadow pairs and 248 return-challenger
pairs across AAPL/MSFT/QQQ/SPY, with zero invalid observations in these reports;
the entire response passed strict finite JSON serialization. Browser smoke
checks passed for actual data and browser-only fixtures at 1440/390px, including
cost selection, sample coverage, no overflow and no page errors. Health confirms
paper_only=true and live_trading=false. No orders were submitted in this work.
Accounting/cost qualification and profitable forward evidence remain incomplete.

## 2026-10-03: Precise Paper Ledger Launch Diagnostics

Replaced the generic unavailable-ledger reason with separate missing-account,
required-reconciliation, unverified-accounting/cost, and non-reconciled-status
blockers. The original admission predicate is unchanged. This read-only
projection does not certify matching balances or complete costs.

The full learning-cycle test module passed **59 tests** (one dependency
deprecation warning), including seven diagnostic/admission combinations.
Built and deployed backend image manifest
`f62fb089651b93ea298e9cda4ab311be3e03a64817b1c97cadce4d27b2d58d6f`.
The deployed launch API reports reconciliation_required=false,
accounting_verified=false, status=halted, with both applicable blockers.
The paper broker clock at 12:54 Eastern confirmed market closed and next open
October 5 at 09:30 Eastern. No order was submitted in this check.
Live trading remains disabled; profitability is not established.

## 2026-10-03: Delayed MSFT Fee Accounting Review

The live audit found a broker-read halt. The transport-only recovery preview
correctly refused because accounting flags were unresolved. Fresh reconciliation
then showed two additional FEE activities of -$0.01 dated October 2, bringing the
reported fee subtotal to **$0.07** and observed paper cash to **$99,999.93**.
The account remains flat with six orders/fills. Six fills still lack explicit
commission fields, so the subtotal is not certified complete cost evidence.

Before applying review, encrypted snapshot
`457e7a710243e8653aec663ae75f8f4517f5e14833fe3e49ceef2bc4eba89007`
passed an isolated restore of **85 tables, 76,605 rows and eight model files**.
A separate monetary replay preview passed for new journal
`00fe5f9e174a432a0c0ef424d3eb010de700cbf710401aef8b5df48fdf068c9d`,
with the existing $0.003446928480 unrounded residual fully explained by the
already-reviewed cent-rounding policy. Explicit monetary-only review applied.
Subsequent live status reported observed accounting ready, no reconciliation
requirement, no unexplained residual, zero positions and six orders.

Research-only qualification **2** was recorded against the new journal with hash
`ade60f0316f3ea1c8292eaad162fc0276025513c51acd120ffcf4f05bfdfaf44`.
Its status is current; activation, execution, legacy admission and live authority
all remain false. The accounting/recovery halt, unknown costs and strategy gates
were not bypassed. Health remained paper-only/live-disabled. No new trades or
application code changes were made in this accounting-review step.

## 2026-10-03: IEX Crash-Recovery Lease

The minute-cadence IEX collector retained the generic 900-second Redis lease
despite having a 120-second hard task limit. Added named IEX task-limit constants
and a dedicated **180-second lease**, retaining a 60-second margin after the
hard limit. Existing Redis exclusion, PostgreSQL advisory locking, execution
controls and other jobs' lease durations remain unchanged. This bounds abandoned
lease blocking to three minutes; dispatch on the next minute tick and service
availability can add further delay. It is not a three-minute end-to-end uptime
guarantee.

Added a real subprocess-death test using a separate local Redis server with no
persistence. It verifies the production 180-second TTL, duplicate refusal before
and after killing the owner, then waits for natural expiry before acquiring and
releasing a replacement. The test never shortens/deletes the lease to simulate
recovery, and does not touch the retained app's Redis, database or broker.

The crash test and focused scheduling, PostgreSQL-lock, transaction and collector
regressions passed **43 tests and four subtests**, with one existing warning, in
**188.76 seconds**. The elapsed duration includes the actual 180-second lease
expiry. The backend image built with unchanged dependencies. No new full-suite
result is claimed for this narrowly scoped lease change.

Deployed runtime inspection confirmed `lease_seconds=180`; real queued collector
job `62db0560-e39e-4121-983b-26027b268d34` completed as `observed`, waiting for
fresh session bars without stale predictions. Health remained paper-only with
live trading disabled. No production worker was killed and no broker orders
were submitted for this verification.

## 2026-10-03: Uncertain Advisory-Lock Acquisition

Extended the real PostgreSQL regression to interrupt both before and after the
acquisition statement executes. The after-execution interruption reproduced a
remaining lock leak: acquisition had not returned a boolean to the runner, so
the previous cleanup returned an uncertain connection to the pool. The business
callback was correctly withheld, but the database lock survived.

The runner now tracks explicit acquisition confirmation and discards the owned
connection if confirmation never arrived. A confirmed refusal still follows the
ordinary duplicate-skip path. Both injected interruption cases now pass; neither
executes business work, and another PostgreSQL connection can acquire the key
afterward. **88 focused tests and four subtests passed**, with one existing
warning, including all eight real PostgreSQL lock cases. The preceding revision
passed the full 1,255-test suite; no new full-suite count is claimed here.
The runtime rebuilt with unchanged cached dependencies.

Deployed worker verification completed through Celery job
`2111e16e-f96f-4873-9a10-fd7b4b22b1b5` with status `observed`. The learner correctly
waited for fresh session bars while the market was closed. `/api/health` reported
healthy, paper-only and live trading disabled. No broker orders were submitted.

## 2026-10-03: Scheduled Job Advisory-Lock Ownership

A real PostgreSQL reproduction exposed a shared runner bug: `_run_job` acquired
a session-level advisory lock through the ORM session, then the callback's commit
returned that connection to the pool. Borrowing it elsewhere made final cleanup
use a different connection and leak the original lock. Both success and failure
reproductions failed before the fix. This can interfere with later workers;
passing sequential SQLite tests did not cover it.

The runner now retains a dedicated PostgreSQL connection for the advisory lock,
separate from the committing business-data session. It unlocks that exact
connection and discards it rather than returning it to the pool if unlock raises
or reports lost ownership. Redis coordination and job execution permissions are
unchanged. Primary behavior references:
https://docs.sqlalchemy.org/en/20/orm/session_basics.html and
https://www.postgresql.org/docs/17/explicit-locking.html .

Six real PostgreSQL cases now pass: success/failure combined with normal unlock,
forced unlock exception, or false ownership response. Each forces connection
borrowing between commits, proves duplicate-job rejection during work, and
proves another connection can acquire the lock afterward. Cleanup logging omits
the injected private driver detail. The complete isolated backend suite passed
**1,255 tests and 203 subtests**, one expected SQLite row-lock skip and 28 existing
warnings, in **230.03 seconds**. The runtime rebuilt with unchanged cached
dependencies. This does not claim recovery from every network or process fault.

Before deployment, real fixed-slot research reached **24 paired outcomes**, six
per symbol, with no pending, expired, missing or invalid observations. All
challenger decisions were cash. Always-long means after hypothetical 5 bps per
side were AAPL -8.8723, MSFT -1.5538, QQQ -8.1760 and SPY -8.9602 bps. These are
price proxies, not executed strategy profits; no trading allocation is justified
by this small sample.

Post-deployment Celery job `6d2d88a4-edeb-4a1b-b4fa-576724153183` completed as
`observed` through the updated runner. All symbols waited for fresh session bars
outside market hours; no stale forecasts or orders were generated. Actual and
browser-fixture research smoke checks passed at desktop/mobile widths. Encrypted
local snapshot `89d43aa31e0e5ece2b208a780d88661184bbb638aa5be140553ee88f5d5cfc20`
restored **85 tables, 69,950 rows and eight model files** successfully. This remains
local content restoration, not off-host or broker recovery certification.

## 2026-10-02: User-Level macOS Host Startup

Inspection found no app-specific login LaunchAgent. Added and installed
`macos_host_service.py`, using structured plist serialization and existing Colima
only. The startup entry invokes the `frozen-stock` profile with context activation
and config saving disabled. It exits after successful startup and retries failed
startup attempts with launchd throttling. The separate AC-only `caffeinate -s`
entry holds a system-sleep assertion; global power settings remain untouched.
No VM recreation, Docker commands, credential copies or broker calls are made
by the helper. Colima's primary autostart guidance was reviewed:
https://github.com/abiosoft/colima/blob/main/docs/FAQ.md . Foreground startup was
not used against the already-running VM because the implementation exits early
in that situation; see https://raw.githubusercontent.com/abiosoft/colima/main/cmd/start.go .

Six host-helper tests passed: fixed-profile/no-context-change arguments, AC-only
assertion, missing-profile rejection, idempotent installation, private plist
permissions, conflicting/symlink definition rejection and non-destructive removal.
Actual launchd bootstrap succeeded. Startup logged `already running, ignoring`
and exited zero; the awake agent remained running, and `pmset -g assertions`
confirmed its `PreventSystemSleep` assertion. Actual uninstall removed the agent
while `/api/health` remained healthy; reinstall and repeated install succeeded.
No reboot, cold start, login cycle, lid-close or battery transition was performed.
This is improved local hosting, not verified independent 24/7 availability.

At 19:02 UTC the automatic collector remained fresh. All four symbols now had
one prospectively declared ten-minute cohort member pending, including QQQ.
This verifies live cohort enrollment, not yet a mature cohort performance result.

## 2026-10-02: Prospective Non-Overlapping Evaluation

Added `iex-return-ten-minute-cohort-v1`. Each new forecast declares membership
before its outcome: issue minute divisible by ten UTC, with the target minute
finished by the next slot. Selection does not depend on return, long/cash action,
or eventual data availability. The first forecast in a duplicate slot keeps its
place even if it expires; later rows do not replace missing evidence. Old
forecasts without the declaration are not retroactively enrolled.

The new API/UI report separately displays selected, pending, expired, missing,
invalid and paired counts, plus same-cohort modeled returns. It uses the latest
1,000 forecasts including pending/expired rows, not only the successful scored
rows. This is a bounded-window sample of issued forecasts, not all market
opportunities, a portfolio, a proof of independent observations, cross-validation,
or trading qualification. The overlapping research report remains available.

Primary-source research reinforced the time-series dependence concern:
https://github.com/scikit-learn/scikit-learn/blob/main/doc/modules/cross_validation.rst
and https://github.com/hudson-and-thames/mlfinlab/issues/295 . The latter project's
current README identifies an all-rights-reserved license; no code was copied or
dependency installed. This change uses a small application-specific prospective
slot rule, not an imported or purported purged-cross-validation implementation.

Focused backend validation passed **68 tests**, including 11 new slot-selection
and end-to-end cohort cases. Seven frontend component tests and TypeScript
checking passed. No new full-suite result is claimed. Both images built with
the existing Vite warnings and unchanged Python dependencies.

Post-deployment API checks returned the new report for all four symbols with
zero selected historical rows, as required by no retroactive enrollment. Health
remained paper-only/live-disabled. Expanded actual/browser-fixture Playwright
checks passed at 1440/390 pixels, including cohort coverage counters, no overflow
and no runtime errors; the mobile screenshot was inspected. A mature live cohort
outcome has not yet been verified for this new declaration version.

## 2026-10-02: Minute-Cadence Forward Research

Changed IEX collection/learning from a five-minute interval to minute-aligned
Celery ticks, with queued tasks expiring after 55 seconds. Existing Redis
overlap protection and task time limits remain intact. No new execution
authority or additional broker orders were introduced. Poll freshness now
expires after 120 seconds rather than 600, with future timestamps still rejected.
The delayed-SIP schedule remains unchanged. More frequent forecasts are
overlapping observations, not five times as many independent experiments.

Reviewed Celery's primary scheduler guidance, including overlap and task expiry:
https://docs.celeryq.dev/en/v5.5.1/userguide/periodic-tasks.html

Focused scheduling, collector, learner and transaction tests passed **86 tests
and four subtests**, with one existing warning. This was a focused run, not a
second full-suite run; the preceding revision passed 1,237 tests. Rebuilt and
deployed the backend with unchanged cached dependencies. Health reported paper
only/live disabled. Desktop/mobile research smoke checks passed again.

A read-only process observed two fresh automatic audit records after deployment:
**18:50:00.035906 and 18:51:00.043666 UTC**, separated by **60.00776 seconds**.
These were actual successful scheduled collections, not manual job dispatches.
Sparse minutes continued to suppress some symbol forecasts rather than being
filled synthetically.

At the next maturity check, the first return-challenger observations scored for
AAPL/MSFT/SPY: one paired observation each, zero missing and zero invalid. All
three predeclared challenger actions were cash. The same-observation always-long
price proxy, after modeled 5 bps per side, returned +2.434946, -10.000000 and
-11.299250 bps respectively. These are not broker fills, portfolio profit or
evidence of an edge. QQQ had no paired new-challenger outcome at this check.
The learner therefore has verified end-to-end prospective scoring, but still
requires substantial forward evaluation before any strategy allocation.

## 2026-10-02: Prospective Cost-Aware Return Challenger

Added `iex-ridge-return-v1` alongside the existing direction learner. It reuses
the installed scikit-learn Ridge estimator (alpha 10, SVD solver), fits each
symbol separately on up to the existing 1,000-row history window, and requires
50 valid known forward entry/exit observations. Training targets are clipped to
500 bps; evaluation returns are not clipped. Features retain their existing
fixed percent units. These parameters are declared experimental choices, not
optimized or proven profitable settings.

Each new forecast freezes its training-data digest, sample count, return estimate,
model parameters, and long/cash decision. The long hurdle is 10.0050025 bps gross,
derived from hypothetical 5 bps costs on both entry and predicted exit notionals.
Missing or future observations, wrong-symbol/version rows and invalid shadow
evidence are excluded. Old observations may train the model, but old forecasts
are never retroactively enrolled in its performance cohort. The separate UI
reports paired outcomes, missing evidence, return error versus a zero-return
baseline and after-cost results versus always-long on the same observations.
All are overlapping price proxies, not a portfolio, executable quotes, broker
fills, verified net profit, or permission to trade.

Reference implementation and method documentation:
- https://scikit-learn.org/1.5/modules/generated/sklearn.linear_model.Ridge.html
- https://scikit-learn.org/1.5/modules/sgd.html
- https://github.com/scikit-learn/scikit-learn/blob/main/doc/modules/linear_model.rst

The focused backend run passed 57 tests, including 20 new challenger cases.
Six frontend component tests and TypeScript checking passed. The complete
isolated backend suite passed **1,237 tests and 203 subtests**, with one expected
SQLite row-lock skip and 28 existing warnings, in 258.34 seconds. Both images
built successfully, with existing Vite sourcemap/chunk-size warnings. Python
dependencies were cache-identical to the previously verified test runtime.

The deployed Celery job `ccdb05c4-690d-4cda-9317-34f6dd490ffe` completed and
recorded prospective AAPL/MSFT/SPY forecasts at 18:42:35 UTC. QQQ was withheld
for missing fresh contiguous bars. The next scheduled collection recorded new
AAPL/MSFT/SPY forecasts at 18:43:36, with training counts increasing to
193/192/190. All selected cash. At 18:45 UTC none had reached outcome maturity;
no positive performance or new learner broker trades are claimed.

Actual and browser-only fixture checks passed at 1440/390 pixels, including the
new return panel, cost controls and no page overflow/runtime errors. A first
broad UI run reached a dashboard/auth HTTP 429 rate limit, confirmed in Nginx
logs. The subsequent standalone rerun passed all 27 sections at both widths,
viewer write protection and temporary role access. Rate limits were not changed.

An authorized $10-capped **MSFT paper integration roundtrip** also completed at
18:43:36 UTC under run `55971380-378e-4dc5-b406-a9362673d5a1`: buy and sell both
filled for 0.019395045 shares at 515.08 and 514.99, respectively. Gross fill
loss was $0.00174555405 before unverified costs, not strategy profit. This brings
the observed account to six filled orders, three closed integration roundtrips
and zero positions. All six fills still lack explicit commissions. The new
activity journal hash is
`87d600babca9c07617dc22fa1a5f304f10b554390ad77988050536cbae078e1b`.
Independent replay explained the $0.003446928480 aggregate unrounded cash
residual under the existing reviewed cent-rounding policy. A read-only probe
completion preview passed; the explicit completion review then applied with
report hash `7c471dd8f11980aca3be956ac35c7ef305b54698f7911b2806fa41081886378c`.
No automatic resume, live authority, cost qualification or strategy activation
was granted.

The post-change encrypted local snapshot
`3519bfb8d0968fc94c7273bac64d144b9894f2ea7a437845078f518b1278369a`
passed an isolated restore of **85 tables, 63,450 rows and eight model files**.
It is still not off-host durability or broker/queue recovery certification.

## 2026-09-29: Delayed SIP Research

Implemented the documented delayed historical SIP path using the existing pinned
Alpaca SDK transport, not a new scraping dependency. Added a separately labeled
collector, five-minute job and leases, viewer/researcher API boundaries, and a
control-room table comparing same-minute SIP/IEX coverage and close-price gaps.
Requests and storage enforce a 16-minute delay. Live execution and the IEX
forward learner remain isolated from these rows.

At **15:19:17 UTC**, the actual credential probe returned **93 bars per symbol,
372 total**, for AAPL/MSFT/QQQ/SPY in the 13:30-15:03 UTC window. Repeating the
import in a fresh SQLite session kept exactly 372 rows, with no duplicate bars.
The existing execution-feed check stayed blocked. No broker orders or application
database writes occurred in this disposable probe. It proves access to that
delayed historical window, not real-time SIP entitlement or profitability.

Focused backend validation passed **73 tests and 11 subtests**. Cases include
cutoff boundaries, early closes, invalid/duplicate observations, pagination
exhaustion, redacted 401/403/422/429 failures, provider separation, idempotency,
disabled collection, queue routing and role mapping. **11 frontend tests** and
TypeScript checking passed. Both Docker images built successfully; existing
Vite sourcemap/chunk-size warnings remain visible.

The complete isolated backend suite passed **824 tests and 197 subtests**, with
**zero failures or skips**, in **233.60 seconds**. Existing 28 warnings were not
suppressed. The test project's PostgreSQL/Redis containers were removed afterward.

Deployed both images locally and ran the actual Celery task through Redis. At
**15:23:27 UTC** it stored **388 bars (97 per symbol)** in PostgreSQL. The status
endpoint reported 97 same-minute IEX matches for AAPL/MSFT/SPY and 94 for QQQ;
the three missing IEX minutes remained missing. This is a source comparison,
not a simulated trading result. The five-minute collector is enabled locally.
Without another manual trigger, the scheduler collected again at **15:28:22 UTC**;
PostgreSQL then contained **408 delayed-SIP rows**. This verifies the automatic
collection path as well as the explicit queued job, not just its schedule config.

Post-deployment Playwright passed login, IEX, delayed-SIP and learning views,
mobile layout, viewer control restrictions and logout with zero runtime errors.
Screenshot review exposed a wrapping mobile header; after fixing it, the expanded
smoke test passed one-line headers, horizontal access to the last comparison
column and no page overflow. Eleven frontend tests passed again after that fix.

The post-deployment restore drill matched **83 tables, 12,734 rows and 8 model
files**, completing in **5.57 seconds**. Private archive/report:
`.paper-backups/8d7bdf830d10/`. This is still local content restoration, not an
encrypted off-host backup. Live trading remains disabled, the selected broker
remains Tradier sandbox, and no execution qualification was changed.

Oracle was checked directly in the in-app browser: the tenancy sign-in page
requires authentication. The sign-in tab was retained and user sign-in/home
region requested. No cloud resources were created or claimed as deployed.

## 2026-09-29: Persistent Alpaca Activity Reconciliation

Added the opt-in `alpaca-activities-v2` contract to the actual paper ledger,
API initialization and recovery boundaries, plus migration
`0049_alpaca_activity_ledger`. Existing accounts stay `legacy-v1`. V2 freezes
an observed baseline after two consistent broker reads, persists normalized
activities with nullable execution time for dated cash events, and replays full
history against that baseline. It rejects missing activity IDs, immutable-record
changes, unsupported corporate actions, ambiguous fees and cross-account order,
activity or fill ownership. A mismatch remains halted until reviewed; a matching
equation never supplies missing all-in costs or grants order authority.

The real Alpaca GET-only probe passed at **14:41:54 and 14:48:04 UTC**. Each run
initialized a disposable SQLite ledger and reconciled twice in fresh DB sessions
and API clients. Both equations matched with zero cash residual, one cash
activity, zero fills, and no duplicate rows. The running application's database
and provider selection were untouched. No broker order/account writes were made.
This is evidence about the current observed account, not complete inception
accounting, actual fill handling at this broker, cost qualification or profit.

The first broad regression run passed **797 tests and 197 subtests**. Additional
populated migration checks passed on both SQLite and PostgreSQL, preserving old
payloads/default contract and allowing date-only entries. The new isolated-probe
fixture initially omitted required `status` and `last_equity`; both omissions
were corrected without changing the production account validation. The final
focused run passed **45 tests**, including the corrected probe. The final complete
run passed **801 tests and 197 subtests, with zero failures or skips**, in
**227.92 seconds** in the isolated PostgreSQL/Redis stack. Its 28 existing
warnings were not suppressed. The interim run with the incomplete probe fixture
was stopped and replaced by this clean full run. Nine frontend tests and
TypeScript checking also passed. The disposable validation services were removed.

The rebuilt image was deployed locally and the persistent database is at
`0049_alpaca_activity_ledger`. The production schema/ORM comparison passes.
All 11 runtime services are running, with backend/web/PostgreSQL/Redis healthy.
Post-deployment Playwright passed login, IEX and learning views, mobile layout and
logout with **zero runtime exceptions**. The selected provider remains
`tradier_sandbox`, the production ledger remains `uninitialized`, and live trading
is disabled. V2 was exercised only in the isolated probe, not silently activated.

Before the application upgrade, a real restore drill verified **83 tables,
11,859 rows and 8 model files** in **6.35 seconds**. Private backup/report:
`.paper-backups/243241fe5f8f/`. Its scope remains data/model content restoration,
not off-host encryption, roles/ACLs or broker recovery.

The running forward learner was inspected during the market session: **11 scored
forecasts per symbol** for AAPL, MSFT, QQQ and SPY, with two pending per symbol.
At that observation, only SPY beat its baseline Brier score; the other three
trailed. These 44 scored forecasts are real forward research outcomes, not
executed trades, and are far too few to establish profitability. Live trading
and the existing execution qualifications were not enabled by this work.

## 2026-09-29: Recovery and Activity Contracts

Implemented a reproducible local restore drill using PostgreSQL's exported
snapshot and custom-format dump. The first actual run restored **83 tables,
10,586 rows and 8 model files**, matching every table's source-snapshot hash and
every archived model file's hash. It completed in 7.83 seconds. Its disposable
database had no network access and was removed. Private archives and the report
are retained under `.paper-backups/b5179b45e0f1/`, excluded from Git. This verifies
database content/model-file restoration, not off-host durability, roles/ACLs,
broker reconciliation, queue replay or automatic recovery authorization.

Added a versioned Alpaca activity normalizer and shadow replay to the read-only
broker probe. The real account returned one cash activity, no orders, fills or
positions; both reads produced the same normalized journal digest. Opening
baseline and execution-cost evidence remain unavailable. The parser tests cover
cash dates, partial fills, fee separation, idempotency, conflicting IDs,
nonfinite values, mixed fee attribution and missing baselines. The existing
execution ledger was not migrated and no order authority was granted.

The broad regression run exposed incomplete authentication fixtures, an outdated
legacy migration fixture, missing Redis in the test image, and missing PostgreSQL
client tools in the runtime image. These have been corrected without loosening
production identity, broker or risk checks. A real migration bug was also found:
the Alembic environment rewound populated legacy databases to revision 0030 after
running later migrations. Removed that rewrite and verified two successive
upgrades of the populated legacy fixture. Existing databases already stamped
incorrectly require separate inspection; no blanket restamping repair was used.

Validation stack definition: `compose.test.yaml`, with separate PostgreSQL/Redis,
an internal network, no published ports or injected broker credentials, and a
test-only Redis server binary for the real worker-termination tests. Five archive
security tests and eighteen activity-parser tests pass. Nine frontend unit tests,
TypeScript checks and the browser smoke test pass. The final full backend suite
passed **772 tests and 197 subtests**, with no failures or skips, in 251.10 seconds
inside the isolated validation stack. The 28 warnings concern existing upstream
deprecations, a Pydantic name warning and SQLAlchemy's cyclic-FK sort warning;
these warnings were not suppressed. The temporary test services were removed.

A second restore after rebuilding the runtime verified **83 tables, 10,636 rows
and 8 model files** in 5.67 seconds; report/archive directory
`.paper-backups/1cf0b1236c1f/`. The runtime backup-tool probe now returns `clear`.
The drill uses PostgreSQL 16 tools in the database container, matching the server;
installed runtime client-tool availability is a separate check.

Post-rebuild browser testing exposed stale Nginx backend DNS caching (HTTP 502).
Added the documented Nginx `resolve` upstream option and Docker DNS resolver,
then verified a forced backend-IP change using the actual web image in a separate
internal test network. The same web container recovered without restart and the
production API returned HTTP 200. Browser login, IEX/learning display, mobile
layout and logout then passed again with zero runtime exceptions. The web health
check now probes the API rather than static HTML. The expanded proxy drill also
passed healthy -> unhealthy -> healthy transitions during the forced outage and
recovery. It touched no production containers and removed its own fixtures.

Cloud and optional LLM status: local inspection found no OCI CLI configuration,
Terraform/OpenTofu/OCI CLI, or Ollama executable. Oracle sign-in/home-region input
was requested. Neither Oracle deployment nor local-model inference is claimed.

## Earlier Verification (2026-09-28)

## Forward-learning follow-up

- Added migration `0048_online_research` and deployed it successfully to the
  persistent local PostgreSQL database. All 11 runtime services are running;
  backend, web, Redis and PostgreSQL health checks pass.
- The real queued IEX job returned `observed` and ran all four symbol learners.
  Outside session hours, each returned `waiting_for_fresh_contiguous_bars` with
  zero scored forecasts. No historical predictions were backdated.
- 86 focused backend tests and 11 subtests passed in Linux, including nine new
  online-learning test cases. They cover delayed scoring, model updates,
  duplicate replay, stale/unfinished inputs, missing minutes, feed isolation,
  future ingestion, missing targets and late-backfill expiration.
- TypeScript checking and both Docker image builds passed. Playwright passed
  viewer login, IEX display, per-symbol learning display switching, desktop and
  mobile layout, logout and zero runtime exceptions. Its initial learner locator
  incorrectly assumed a region role on a labeled div; corrected the test locator
  and reran successfully.
- Native tests initially ran from the repository root, whose shared frontend
  `.env` is not a backend settings file. Rerunning from the backend directory
  passed all 32 selected tests. Production verification used the Linux image.
- No new live-session outcomes or profitability evidence yet. Existing venue,
  accounting, risk and cloud deployment limitations below remain unchanged.

## Latest integration verification

The browser console is now deployed at http://127.0.0.1:8088. It uses the private
local role keys, not Clerk or a paid hosted service. Both application images were
built on Linux ARM64 and recreated on the dedicated local Docker context.

- Adopted `alpaca-py==0.44.0` and `exchange-calendars==4.13.2`; upgraded
  `yfinance==1.7.0`. See OPEN_SOURCE_REVIEW.md for the 11-project survey.
- The actual Celery IEX job collected **1,558** real one-minute bars for
  2026-09-28: AAPL 390, MSFT 389, QQQ 389, SPY 390. Sparse minutes were not
  filled. Scheduled recollection and service recreation preserved the same
  row counts. The feed remains research-only and cannot pass SIP execution gates.
- The actual queued daily importer now retrieved **1,996** validated bars via
  **yfinance**, 499 per symbol. This is a fresh provider download, not a relabel
  of the old fallback. The earlier immutable Yahoo-chart training snapshot is
  retained unchanged.
- The existing challenger report now correctly says `did_not_beat_baseline`.
  No new holdout was consumed, binding changed, or model promoted.
- **276 focused backend tests passed** in Linux across two runs (107 and 169),
  including PostgreSQL concurrency and forward-trial regression tests, with no
  skips. **9 frontend component tests passed**, and TypeScript checking passed.
- Playwright verified real local viewer login, the IEX panel, mobile width and
  logout with no browser runtime exceptions. Screenshots are under
  `output/paper-ui/`. This is a focused smoke test, not the entire managed-Clerk
  E2E suite. The mobile grid overflow found by the smoke test was repaired.
- Docker builds passed with existing frontend source-map/chunk-size warnings.
  Native pnpm 11 refused ignored dependency build scripts; the reproducible
  frontend image uses pnpm 10.11.0 and built successfully. No broad script
  approval was added to work around that local toolchain behavior.
- At 20:48 UTC, all five infrastructure checks passed. The paper-only invariant
  passed, but trading remained blocked by ledger, qualification, risk, recovery
  and regular-session feed prerequisites. Alpaca read-only replay still showed
  zero orders, zero fills and zero positions. No orders were placed.

The deployment is local, not Oracle-hosted. It is a working research console,
not yet a fully autonomous qualified broker-trading product. Nothing here
establishes expected profit or readiness for real money.

## Earlier stack validation

## Scope

The new stack runs in the dedicated local `colima-frozen-stock` Docker context.
Its PostgreSQL database and volumes are separate from historical deployments.
No existing account ledger, audit history, or model registry was reset or
imported. This is local operation, not an Oracle/cloud deployment or a promise
of laptop uptime. The API is at http://127.0.0.1:8010/api/health.

## Implemented and exercised

- Persistent PostgreSQL, Redis AOF, model/report volumes, one leased scheduler,
  independent watchdog, and five separately scheduled worker groups.
- Full required-queue health coverage with positive worker ping evidence.
- Configured four-symbol scheduled challenger universe and matching preflight
  metadata; missing/inactive assets block rather than silently shrinking it.
- Read-only progress and dual-paper-broker comparison commands.
- Private root `.env`, mode 0600, excluded from Git. Broker configuration was
  loaded without enabling live trading or switching the active paper venue.

Final focused Linux validation: **143 tests passed**, with no skips, including
PostgreSQL concurrency, training, provenance, broker contracts, queue behavior,
and the new operations tests. Separately, **2 Redis-backed worker-crash tests
passed**. `git diff --check` and Compose configuration validation passed.

Two training tests initially encountered NumPy matrix warnings in the macOS
Python environment. They passed in the target Linux container without weakening
numerical validation. Linux is the verified runtime; native macOS training is
not claimed to be validated.

A controlled stop of the learning worker produced `blocked` with missing queue
`learning`. After restart, all five infrastructure checks passed again. This
is not a complete disaster-recovery or full interruption-matrix certification.

## Real historical training

The actual queued daily import retrieved 499 historical daily bars for each of
AAPL, MSFT, QQQ, and SPY, through the real `yahoo_chart` fallback (1,996 rows,
2024-09-30 through 2026-09-25). No synthetic market observations were imported.
The `yfinance` path did not supply those rows; their provenance was preserved.

A researcher-authorized API request queued and completed job
`719aaf1c-ecc7-4a4f-b6a6-3b38d759d14f`, producing frozen challenger
`2ac334e33951af066bdc22d3866697f601804e65fae4e726060b8f0a46f88aa1`.
The model remained a challenger; no binding, promotion, order, or live authority
was created. The reserved final holdout was evaluated once.

The selected logistic model's holdout Brier score was approximately 0.2585
versus the baseline's 0.2489, and log loss was 0.7108 versus 0.6909 (lower is
better for both). Training completion is therefore not a demonstrated predictive
improvement. Historical simulated return is not broker-verified profit, and
adjusted Yahoo prices are not point-in-time corporate-action evidence.

The generated private artifact `first-historical-training.json` contains the
full report. It is retained both here and in the reports volume.

## Broker checks

Both supplied paper configurations passed account and position reads. No
provider switch, account initialization, order submission, or cancellation was
performed. The generated `broker-comparison.json` contains redacted results.

The deeper Alpaca read-only probe replayed order/activity history consistently,
but found zero trade fills and therefore no fill-cost evidence. Qualification
was not established. Tradier's existing adapter explicitly cannot establish
complete historical accounting. Neither connectivity result authorizes trading.

## Remaining blockers

The exact scheduled four-symbol cycle was created and correctly stopped at
preflight. The final progress snapshot at 19:36 UTC had healthy infrastructure
but failed feed, broker qualification, venue qualification/activation, ledger,
risk, and recovery gates. No paper-run approval or forward trial exists for it.

Next work must supply qualified paper-venue/account evidence, reconcile through
the authorized workflows, establish the required real-time feed and adjustment
evidence, resolve recovery/risk gates, and approve an exact future session.
Do not bypass those gates to make a status turn green. Delayed real outcomes
and repeated forward sessions are still needed to demonstrate improvement.

The provided AI endpoint is a localhost proxy from another hosting environment
with a dummy key, not a configured AI research service in this stack. External
AI research, durable cloud hosting, off-host recovery validation, and any live
pilot remain outstanding. Credentials shared in chat should be rotated.

## Self-hosted encrypted backup validation (2026-09-29)

- Running locally on Colima: all 11 services running; backend, PostgreSQL,
  Redis, and web report healthy. Compose configuration validation passes.
- Restic 0.19.1 official image pinned to the pulled manifest digest; no external
  installer scripts or privileged hosting control plane installed.
- A fresh database snapshot restored into isolated PostgreSQL: 83 tables,
  12,948 rows, and 8 model files verified. No broker requests or recovery-gate
  changes. Workers continued running during the snapshot.
- Encrypted snapshot `35197336752a5b318b409af35a6c8255d00370fc934cc70d68205be70b031947`
  passed full repository data checking, decryption, restore verification, and
  SHA256 equality for the dump, models archive, and original verification report.
- 13 operations tests passed, including actual Docker encryption/restoration,
  wrong-password rejection, deliberately corrupted disposable pack detection,
  unsafe archive rejection, and Linux/macOS context selection.
- Initial test issues were corrected: Colima cannot mount the default private
  macOS temporary path, so fixtures use `.local/restic-test-tmp`; Restic packs
  are read-only, so the corruption fixture explicitly changes its own pack mode.
- Browser smoke passed login, IEX, delayed SIP, learning, mobile, and logout
  checks with zero runtime errors. Vault password and repository verified ignored
  by Git. No backend trading logic changed in this backup work; the full backend
  suite was not rerun for these operations-only changes.
- Not verified/configured: a separate Linux host, host reboot recovery,
  unattended backup scheduling, off-host storage, key escrow, or 24/7 uptime.
  The current repository and key share this Mac. No live orders were enabled.

## Network fault and job transaction validation (2026-09-29)

Added the official Shopify Toxiproxy 2.12.0 image, pinned by digest, to an isolated
fault-test stack. No production ports, volumes, root `.env` mount, or broker
credentials are supplied. Each run has a unique project and bounded cleanup.

Before the fix, two new regression cases failed against real PostgreSQL:
failure reporting committed a flushed record from failed work, and a database
outage replaced the original job exception with a notification persistence error.
The shared job wrapper now rolls back pending work before recording failure,
preserves the original exception, and logs only exception types if recording
the notification fails. Previously committed work is not undone.

Verified after the fix:

- Final-code full backend suite: 827 tests and 197 subtests passed, zero failures
  or skips, in 289.89 seconds. The 28 existing dependency/schema warnings remain.
- Seven real-network fault tests pass: duplicate Redis lease exclusion; Redis
  disconnection and latency; PostgreSQL disconnection and reset; recovery;
  rollback of failed pending work; preservation of the original exception.
- Three new standard regression tests pass, covering flushed/unflushed work,
  preservation of committed evidence, and redaction of notification-failure logs.
- All 13 backup tests pass again, including actual Restic encryption/restoration,
  wrong-password rejection, and corruption detection.
- All 11 frontend tests and TypeScript checking pass. Browser smoke passes login,
  IEX, delayed SIP, learning, mobile, and logout with zero runtime errors.
- Proxy recovery drill passes changed-backend-IP and health-outage detection
  without restarting its web container. Disposable resources were removed.
- Rebuilt backend image deployed locally. Running `jobs.py` SHA256 matches the
  source: `9021b83139065b79cb149acdbd59a5571b8eb16c6b5fdd8f8f0584e040ccb90f`.
  Five Celery workers answer ping, all 11 services run, and the four configured
  container health checks pass. Live trading remains false.

These tests do not certify broker exactly-once effects, ambiguous order timeout
recovery, silent PostgreSQL packet blackholes, host reboot, a remote deployment,
off-host backups, or trading profitability. No broker orders were submitted.

## Workspace redesign and direct local access (2026-09-29)

- Replaced the long dashboard with an overview and seven focused destinations,
  containing 27 deep-linkable sections. Only the selected section mounts its
  data panels. Existing risk, broker, provenance and recovery evidence remains
  available; legacy quarantine cards were removed from the overview.
- Local web access now opens automatically as Viewer. Only unauthenticated
  GET/HEAD requests receive a server-side viewer credential. Explicit invalid
  credentials remain invalid; absent write credentials return 401. Direct API
  access still requires authentication. No backend authorization was disabled.
- The web proxy receives only a dedicated viewer-key environment file. The file
  is Git-ignored and 0600; four provisioning tests pass, including root override,
  credential isolation and invalid-key rejection. No role keys appear in the
  served browser JavaScript. Production identity-provider access remains intact.
- Temporary role elevation lives at System > Access; a real researcher-key
  browser check passed, and reload returned to Viewer without persisting the key.
- Fourteen frontend tests and TypeScript checking pass. Browser smoke covers all
  27 sections at 1440px and 390px, after network settlement, with no page overflow
  or runtime errors. Real IEX symbol switching, delayed SIP data, mobile menu,
  deep-link reload, anonymous write denial and direct API denial are checked.
- Desktop and mobile screenshots are in `output/paper-ui/`. Overview, market,
  learning and mobile portfolio layouts were visually inspected. The actual
  proxy image also passed the isolated changed-backend-address recovery drill.
- Production web builds succeed; existing Vite sourcemap/chunk-size warnings
  remain. Backend trading logic was unchanged, so the full backend suite was
  not rerun for this presentation/proxy change. No orders were submitted.
- This automatic viewer configuration must remain loopback-only. It is not a
  public anonymous dashboard deployment or a grant of trading authority.

## Alpaca initialization contract and replay evidence (2026-09-29)

The ledger UI now offers an explicit `alpaca-activities-v2` choice when the
selected broker is Alpaca; legacy remains the default and Tradier cannot select
an Alpaca-only contract. The control remains admin-only. Unknown providers
cannot initialize. No provider switch or production-ledger initialization was
performed by this change.

The read-only probe now requires equal activity/fill/event counts and journal/
baseline hashes across its two independent replays. Previously, two separately
matched reconciliations could pass even when the history changed between them.
Changed evidence now blocks the unchanged-replay claim; it does not imply a
broker failure or certify all costs.

Verification: 51 focused backend tests and 17 frontend tests passed, TypeScript
checking and the production web build passed. A browser fixture selected v2 and
verified the exact intercepted POST payload; no broker writes occurred. The
updated probe source was executed inside the backend container via stdin using
real allowlisted GET requests and a disposable database: one cash activity,
zero fills, matched cash/inventory and unchanged hashes on both replays.
Costs and qualification remain unverified. The updated web image is deployed;
the probe change was initially in workspace source only and was subsequently
baked into the backend image during the deployment described below.

Sources: https://docs.alpaca.markets/us/docs/account-activities and
https://github.com/alpacahq/alpaca-py. Separate dated fees are not assumed to be
zero or attributed to fills merely because an SDK call succeeds.

## Persistent Alpaca account and minute-aligned ingestion (2026-09-29)

The deployed stack now explicitly selects `alpaca_paper` in shared Compose
configuration. Its persistent account was initialized with `alpaca-activities-v2`
and reconciled without submitting orders. A subsequent read-only API check
confirmed reconciled status, seven equity snapshots, zero orders, zero fills,
and unverified accounting. The dashboard shows simulated equity of $100,000;
that amount is not profit. Live execution remains disabled and the paper kill
switch remains enabled. Account-specific qualification is still missing.

Readiness at 18:05 UTC exposed a collector phase mismatch: ingestion occurred
near second 57, but expected completed bars advanced on the minute. The numeric
60-second Celery interval inherited the scheduler startup phase. The collector
now uses Celery's built-in `crontab()` minute schedule, retaining the same request
frequency, task expiry, exclusive lease, and strict completed-bar requirement.
Feed repair metadata also now ends at the completed-bar cutoff instead of
requesting future minutes. No missing bar is fabricated or treated as present.

Regression checks failed against the prior implementation before the fixes.
Afterward, 40 focused tests plus 15 subtests passed, including actual Celery due
calculations for multiple startup seconds and continued rejection of incomplete
feeds. The updated backend image was built and deployed successfully.

A read-only runtime probe sampled readiness 15 times from 18:08:15 through
18:11:07 UTC. All samples reported the intraday feed ready with no missing
intervals and live orders disabled. Ingestion at 18:09, 18:10, and 18:11 occurred
within 30 milliseconds of the minute boundary (provider calls completed later).
This is a bounded observation, not a guarantee of future provider availability.
The final readiness check still blocked accounting and risk, warned on model
freshness, and prohibited both paper and live orders. All 11 retained services
were running; backend, web, PostgreSQL, and Redis reported healthy.

The full backend suite then passed: 834 tests and 201 subtests, zero failures
or skips, in 227.95 seconds. Its 28 warnings concern existing dependency
deprecations, a Pydantic protected namespace, and cyclic schema-sort metadata.
Tests ran against the isolated validation PostgreSQL/Redis project without
broker credentials, not against the persistent paper ledger.

## Cash-flow-adjusted observed change (2026-09-29)

Added a separate observed-performance projection to the persistent paper ledger
and Portfolio > Ledger. It derives dollar change from the first frozen-baseline
equity snapshot, latest reconciled equity, and full persisted activity journal.
Cash deposits and withdrawals are excluded; dividends, interest, and reported
fee effects remain in equity. Fees are displayed as a breakdown, not deducted
again. A generic cash journal is not guessed to be funding or investment income.
The calculation requires matched cash/inventory equations, matching snapshot
boundaries, and the latest reconciliation journal digest. It does not turn
unknown fill commissions into zero, certify all costs, or change trading gates.

The actual deployed account returned a provisional zero change, zero new net
funding and zero reported fee expense, from the 18:01:25 UTC initialization
observation to the latest snapshot. No orders or fills exist. Zero change is not
evidence of a profitable strategy. Qualified dashboard P/L remains unavailable.

Focused checks passed: 53 backend tests, followed by seven persistence/digest
tests after adding the digest comparison. Twenty frontend tests and TypeScript
checking passed. Backend and web images built and deployed. The browser probe
checked actual API values at 1440px and 390px, then injected an invalid-boundary
fixture and verified that monetary results disappeared. No runtime errors or
horizontal overflow occurred. Both screenshots were visually inspected.

The full backend run passed 861 tests and 201 subtests in 214.59 seconds, with
the same 28 warnings noted above. The later-added digest-tamper test passed in
the separate seven-test persistence run. The complete browser smoke also passed
all 27 sections at desktop/mobile widths, authentication boundaries, real feed
checks, and temporary-role reset, with zero runtime errors. Final readiness
still has accounting/risk blocked and model freshness warning; both paper and
live order admission remain false.

Provider references consulted: https://docs.alpaca.markets/us/reference/getaccountportfoliohistory-1,
https://docs.alpaca.markets/us/docs/account-activities, and
https://github.com/alpacahq/alpaca-py/blob/master/alpaca/trading/client.py.
The observed-baseline calculation is local ledger logic, not a claim that
Alpaca's portfolio-history endpoint certifies this app's trading costs.

## Live model lineage gate (2026-09-29)

Inspection of the actual persisted training report confirmed a successful
training job with verified artifacts, but final holdout status
`did_not_beat_baseline`: model Brier score 0.25847 versus baseline 0.24887,
and log loss 0.71082 versus 0.69088 (lower is better). Its metadata explicitly
states `eligible_for_trading=false`. Training success is not model qualification.

The live lineage gate previously allowed absent eligibility flags and an empty
metadata challenger exception, checked only that digest strings existed, and
read the immutable registry lifecycle instead of the mutable current state.
It now requires literal true eligibility flags, checks the current lifecycle,
binds the model to the exact snapshot, and invokes the existing dataset and
model artifact validators. Corruption errors are redacted from the public gate
projection. No account/model flags or approvals were changed to obtain a pass.

Seventy focused tests passed across live safety, broker/operations, training
artifact validation, and secondary-approval identity. They cover absent,
nonboolean and false eligibility, legacy challengers, current demotions,
cross-snapshot bindings, both artifact-verifier failures, and successful
verifier invocation. The positive gate unit test uses isolated verifier doubles;
it is not evidence that any deployed model qualifies for live trading.

The backend image was rebuilt and deployed. A fresh report request returned
HTTP 200 after its real artifact checks, confirming the same verified snapshot,
failed baseline comparison, and false trading eligibility. Readiness still
reports no active bound live lineage and prohibits paper and live orders.

Source consulted: https://scikit-learn.org/1.6/model_persistence.html. Its
immutable training-data/code/version guidance supports reusing the repository's
artifact verification rather than accepting hash-field presence as integrity.

## Recovery evidence freshness (2026-09-29)

New regressions reproduced six false passes: future-dated monitoring/feed
snapshots, future-dated recovery heartbeats, and recovery heartbeats older than
the existing ten-minute timeout. The live recovery gate formerly checked only
timestamp presence. It now evaluates both heartbeat ages against the same
evaluation timestamp, requires ages from zero through the existing timeout,
and exposes those ages in its evidence. Monitoring and data gates now reject
negative ages as well as expired evidence. No timeout was relaxed and no
cooldown, accounting review, or approval was cleared.

After the fix, 74 tests passed across live safety, recovery, and approval
identity, including all 18 new freshness boundary cases. One existing Starlette
deprecation warning remains. An initial broader test command used a nonexistent
recovery filename and ran no tests; the corrected invocation passed.

Source consulted: https://docs.celeryq.dev/en/stable/userguide/monitoring.html.
Heartbeat presence alone does not establish liveness; this implementation uses
the application's own monitor/watchdog timeout, not Celery worker-event timing.

The rebuilt backend was deployed and a real readiness request returned HTTP
200 with monitor age about 218 seconds, watchdog age about 18 seconds, and
the 600-second threshold. Recovery remained blocked by its existing cooldown;
live order admission remained false. Isolated test containers were removed.

## Alpaca order transport semantics (2026-09-29)

Before attempting any paper execution, transport regressions reproduced 11
failures: successful HTTP 204 cancellations were parsed as JSON and raised an
error; authentication, rate-limit, server, timeout, and invalid-payload lookup
failures were incorrectly returned as an absent order. The adapter now accepts
empty DELETE/204 acknowledgments, carries structured HTTP status codes on its
redacted exception, and returns absent only for an actual 404. It does not
retry submission or infer cancellation completion from an acknowledgment.

105 tests plus six subtests passed across the HTTP transport, stock ledger,
Alpaca activities, paper recovery and Tradier adapter. The tests use HTTPX's
mock transport and assert every request targets the paper host. Two existing
warnings remain. A bounded simulated buy/sell test was proposed to the user;
no broker write was performed while awaiting that answer.

After rebuilding/deploying the backend, an authenticated GET confirmed the
paper account was active. A random nonexistent client-order ID returned a
structured HTTP 404, and the public lookup method returned None for that same
ID. This real-provider probe performed three GETs and zero broker writes;
actual cancellation acknowledgment remains covered by the HTTP fixture, not
an executed cancellation against the account.

References: https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1,
https://docs.alpaca.markets/us/reference/getorderbyclientorderid, and
https://docs.alpaca.markets/us/docs/working-with-orders.

Sources: https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html,
https://github.com/celery/celery/blob/main/celery/schedules.py,
and https://docs.tradier.com/reference/brokerage-api-markets-get-timesales.

## Authorized Alpaca paper round trip (2026-09-29)

After explicit simulated-money authorization, the bounded probe passed 22
focused probe/transport tests. It was executed once from repository source via
stdin in the deployed backend (the new script is not baked into that image).
The probe required the fixed paper endpoint, matching reconciled account,
open regular session, sufficient cash, no positions or pending orders, unique
client IDs, and live trading disabled. It journals before each submission and
does not retry POST after an uncertain response.

Run: `25a48ccc-38d2-428e-bf44-723d273a8846`. Durable private journal in the
backend data volume: `/data/reports/paper-roundtrip-25a48ccc-38d2-428e-bf44-723d273a8846.jsonl`.

- Buy order `8a7b3143-b9c3-47e0-8fc3-53fd9308e8c0`: $10 notional SPY,
  filled 0.013060394 shares at $764.908 at 19:07:49 UTC.
- Sell order `4aa03249-fa18-4088-8f82-8109818c2dc5`: sold exactly the same
  0.013060394 shares at $764.902, also at 19:07:49 UTC.
- Broker activity history independently contained both fills. The account
  was flat afterward, with reported cash and equity both $100000.
- Fill-price gross change is -$0.000078362364 before unverified costs.
  Both activities omitted commission and net amount. Costs remain unknown.

End-to-end ledger validation FAILED, despite both broker orders succeeding.
The first authenticated reconciliation persisted two orders and two fills,
with zero positions, but halted on a cash residual of $0.000078362364.
The broker-reported balance may involve precision or timing behavior; this
test does not establish the cause, so no tolerance was relaxed.

A second reconciliation halted on immutable order fields. Direct comparison
confirmed both stored quantities were 0.01306039 while broker quantities were
0.013060394. The order model uses Numeric(20, 8), losing the ninth decimal.
This requires a separately tested precision migration and evidence-preserving
repair before repeat reconciliation can pass. Existing rows were not rewritten.

Readiness still reports the global kill switch enabled, live trading disabled,
and paper/live admission blocked. No approval, qualification, accounting gate,
or baseline was overridden. This is execution-plumbing evidence only, not a
profitable strategy or live-readiness result. Isolated test containers were
removed; the running paper stack was retained.

## Fractional quantity storage (2026-09-29)

Alpaca documents up to nine decimal places for fractional quantities:
https://docs.alpaca.markets/us/docs/fractional-trading.
New migration 0050 widens paper position, order, fill, trial-lot quantity and
exited-quantity columns from Numeric(20, 8) to Numeric(21, 9). The integer
range is unchanged. Downgrade refuses silent precision loss. Existing values
and raw payloads are preserved; this schema change does not repair old rows.

The new regression failed on both SQLite and PostgreSQL before the change.
Afterward, 122 focused tests plus six subtests passed, including fresh and
repeat migrations, legacy preservation, nine-decimal order import across
fresh sessions, and PostgreSQL numeric boundary round trips. The initial test
fixture needed an explicit non-null flag and a nonconflicting legacy primary
key; these fixture failures were corrected before counting the regression.

Before deployment, encrypted backup snapshot
419fcffbd75cfbb543f9675042b4bc8061abb29fa6fd218505d395415d8b3e0c
passed database/model restore and file-hash checks (83 tables, 16887 rows,
eight model files). The system Python attempt lacked hashlib.file_digest;
the successful rerun used .local/paper-runtime/bin/python. This is still a
local backup, not off-host disaster recovery.

The rebuilt backend/workers were deployed. Runtime schema/ORM validation
passed with migration 0050 and all five quantity columns Numeric(21, 9).
Both API checks returned HTTP 200; the account remained halted, zero positions,
two orders and two fills, with paper and live admission false.

The full backend run produced 916 passes, 201 subtests and three failures:
forward-trial projections expected eight rather than nine decimal places.
Those exact-output assertions were updated without changing numerical
expectations. The entire affected forward-trial file then passed: 75 tests
plus 18 subtests. The full suite was not rerun after these test-only changes.
Existing dependency and cyclic-FK metadata warnings remain.

Read-only post-migration comparison still shows stored test order/fill
quantities 0.013060390 versus preserved raw quantities 0.013060394; fresh broker
order reads agree with the latter. Both fills also lack a local order_id even
though their broker_order_id identifies the corresponding imported order.
Next work must test and audit repair of these derived rows and links, then
resolve broker cash precision/fee semantics. No raw activity, frozen baseline,
accounting gate or approval was rewritten, and no new order was submitted.

## Fill attribution and evidence-backed repair (2026-09-29)

Six new regressions reproduced missing same-batch links with production's
autoflush=False session, missing late-order links (including commission
enrichment), and acceptance of conflicting symbol/side or existing links.
Order import now explicitly flushes before fill import. Fill import validates
account, symbol, side and any existing link; unchanged historical fills can
recover a missing local link with an audit event. Raw evidence is not replaced
to manufacture a match. Focused validation passed 181 tests plus 24 subtests.

The separate repair_paper_probe_precision script only targets a named UUID
probe's two imported paper orders and fills in a flat, halted account. It
requires current schema, matching paper account, no pending orders, identical
preserved/fresh broker payloads, and a discrepancy exactly explained by old
eight-decimal rounding. It previews by default and logs before/after values
and evidence hashes on an explicit apply. Eight repair helper tests passed.

Before applying, encrypted snapshot
bb5c31f4fdd0a1620f42be37d2da5f6c5e399d2b7fe033839f050c94c65f84ef
passed restore verification (83 tables, 17291 rows, eight model files).
The new backend/workers were deployed. Preview and apply each identified
exactly four corrections for probe 25a48ccc-38d2-428e-bf44-723d273a8846:
two order and two fill quantities from 0.013060390 to 0.013060394.
One repair event was persisted; subsequent reconciliation recovered both
links with two link-recovery events. Another preview returned zero changes.

Two authenticated reconciliations now get past order/fill immutability and
produce the same v2 replay: inventory matches, cash residual 0.000078362364,
unchanged journal hash 1530dd3e6c01cbd0ba1c6b1f8feccb76cfaa2ab1007a500084aa443d25654340.
Both quantities and links were independently queried from the deployed DB.
The account is still flat and halted on cash, not quantity or attribution.
Readiness confirms kill switch enabled and paper/live admission false.
No broker writes, raw-payload edits, baseline resets or permission changes
were performed during this repair.

Further provider research: Alpaca's customer agreement describes cent-rounded
fractional values/proceeds; its paper specification explicitly excludes
regulatory fees and dividends. These support investigating a separate paper
cash-rounding/cost contract, not silently declaring unknown costs verified.
Sources: https://files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf,
https://docs.alpaca.markets/us/docs/paper-trading,
https://docs.alpaca.markets/us/docs/broker-api-faq.

Final full-backend verification: 933 tests and 201 subtests passed in 256
seconds. There were 28 existing dependency/metadata warnings. The isolated
validation containers were removed afterward; the retained paper stack stays
running. This validates the repaired implementation, not strategy profitability
or authorization for live money.

## Cash precision diagnostic (2026-09-29)

Added a non-qualifying USD Alpaca-paper cash diagnostic to each v2
reconciliation report. It verifies terminal order evidence, fill quantity,
symbol/side and corroborating average price, then compares per-fill and
per-order cent rounding. Missing fills/orders, conflicting duplicate IDs,
half-cent ties and disagreement between rounding granularities make the
diagnostic unavailable. Separate cash fees and frozen-baseline adjustments
remain in the equation. A missing cent or inventory discrepancy cannot pass.
Neither raw activities nor the exact reconciliation status are changed.

135 focused tests and six subtests passed, covering the new diagnostic,
activity contracts, ledger and observed-performance paths. The first run
caught a decimal-format expectation and an incorrect fixture assumption that
the $10-notional buy had executed exactly $10; actual fill value is
$9.989999853752. Tests were corrected to use that evidence. The full backend
suite was not rerun for this additive diagnostic; it passed 933 tests before
this change.

After rebuilding/deploying, an authenticated reconciliation returned HTTP 200
and persisted diagnostic status consistent: buy rounds to -$9.99, sell to
$9.99, net rounding adjustment $0.000078362364, residual after rounding zero.
The original exact residual, journal hash and frozen baseline remain intact.
The account stays halted, flat, costs unknown and unqualified. A matching
rounding hypothesis is not a cost-completeness or launch approval.

Provider sources checked directly:
https://files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf
(fractional values/proceeds rounded to cents), and
https://docs.alpaca.markets/us/docs/paper-trading
(paper simulation excludes regulatory fees and dividends). Neither fully
specifies partial-fill allocation or half-cent tie handling; ambiguous cases
are not silently assigned a policy.

A read-only deployed research check at 19:35 UTC confirmed forward forecasts
issued at 19:34:59 UTC: AAPL 69 scored, MSFT 60, QQQ 55, SPY 69, with three
pending forecasts each and one expired MSFT forecast. All remain research-only
and execution-ineligible. Prediction Brier scores are not realized returns or
proof of profitable execution. Existing collection/learning remains running.

## Forward-learning clock integrity (2026-09-29)

Six new regression cases failed before correction: future, timezone-less or
malformed outcome observation timestamps could enter training history; source
bars could be ingested before their closing boundary; invalid target prices
could leave forecasts pending indefinitely; and no neutral probability
benchmark was exposed. Training now accepts only verified observation times
at or before the evaluation clock and after a valid forecast target. Every
source bar must have completed before ingestion. Invalid targets expire at
the existing deadline, without creating a label. Existing forecasts and
outcomes were not rewritten.

Research status and the IEX page now show the 50/50 probability benchmark
(Brier 0.25) alongside the historical-frequency benchmark. Ties do not count
as beating it; no observations produce an unknown comparison. This comparison
does not claim statistical significance, trade profitability or admission.

Verification: 41 focused backend tests, TypeScript checking and 20 frontend
component tests passed. Backend/web images were built and deployed. The real
leased IEX collection job returned observed and scored two forecasts per
symbol, then recorded one new forecast each for AAPL, MSFT, QQQ and SPY.
All 261 persisted scored forecasts passed the observation-clock check. At
19:39 UTC, AAPL/MSFT Brier scores were slightly below 0.25, while QQQ/SPY were
above it. Samples remain small and execution-ineligible.

Playwright passed all 27 sections at desktop 1440 and mobile 390 widths,
including explicit assertions for the new baseline/comparison labels,
viewer/write authorization and zero browser runtime errors. The desktop
and mobile IEX screenshots were visually inspected: labels wrap without
overlap. The full backend suite was not rerun for this scoped learning fix.
Isolated test containers were removed. No broker order was placed or live
permission enabled; accounting and qualification work remains outstanding.

## Paper qualification integrity (2026-09-29)

Inspection found that venue admission trusted persisted qualified/authorized
flags without checking the current account or either package hash. Ten new
regressions reproduced false admissions after account replacement, report or
hash mutation, changed approval identity/reason, blank authorizer, or activation
without an initialized matching account. These are now rejected.

Both activation and admission reassess the redacted qualification, compare its
complete report and digest, verify version and current broker-account hash,
and require a nonempty reviewer. Admission also recomputes the authorization
digest, checks distinct/nonempty reviewer and authorizer, and enforces paper
scope. The newest invalid authorization does not silently select an older
approval. A stored qualified flag with failed validation is reported invalid.
This verifies package integrity/binding, not independent truth of every
reviewer-supplied evidence claim.

138 focused ledger/trial/venue tests plus 24 subtests passed. The final
qualification file passed all 16 cases. Two additional scope fixtures initially
attempted to persist values already rejected by DB constraints; they were
corrected to test unflushed ORM state without disabling those constraints.
A full backend run that had loaded the old fixtures finished with 965 passes,
201 subtests and those same two fixture failures. The corrected 16-case file
was independently rerun successfully. No full-suite green run is claimed for
this turn; 28 existing dependency/metadata warnings remain.

The backend/workers were rebuilt and deployed. Authenticated readiness returned
HTTP 200, missing qualification, current-account evidence false, and both paper
and live admission false. No actual qualification or activation was created.
The separate provider-contract mismatch remains: the v1 gate requires explicit
per-fill commission and precise UTC cash timestamps, while observed Alpaca
paper activities omit commissions and include date-only cash events. The
existing v2 journal and cent diagnostic do not yet constitute a paper admission
contract. Solving that requires a versioned provider-specific accounting and
modeled-cost path, not marking absent fields complete or inventing approval.

## Versioned paper cost sensitivity (2026-09-29)

Added paper-turnover-stress-v1 to the persisted v2 reconciliation report and
the Portfolio / Ledger page. The assumptions are explicit additional turnover
costs of 0, 1, 5 and 10 bps per side, not estimated broker fees. The report
hashes those assumptions, identifies its source journal, and is always
research-only, costs-unverified and non-qualifying. Values use unrounded fill
prices; this is not a broker cash statement or realized net profit.

The evaluator requires a versioned USD baseline and flat beginning/ending
inventory, with the newly observed fills closing independently by symbol.
It preserves immutable baseline evidence, rejects conflicting duplicates,
ignores previously observed fills, keeps separate cash fees and explicit
commissions from being counted twice, and excludes funding/dividends from
trade-only cash change. Missing commissions remain unknown. Open positions
require a separate valuation/cost-basis contract and produce no scenarios.

109 focused backend tests plus six subtests, TypeScript checking and 23 frontend
tests passed. Backend/web images were deployed. A real authenticated
reconciliation persisted the expected model for the two SPY probe fills:
turnover $19.979921345140, gross fill cash change -$0.000078362364,
and modeled change -$0.01006832303657 under the 5-bps-per-side assumption.
The assumption hash is
b38be6e97156635f2118d5c5ed3783d1708c5fb959c50bba0ee50f39af4e7946.
The viewer API exposed the same result; the account remained flat, halted,
and costs unknown. Readiness confirmed kill switch enabled and both paper
and live admission false. No broker order or approval was submitted.

Playwright passed all 27 sections at 1440/390 widths, including four scenario
rows, explicit hypothetical labels, access boundaries and zero runtime errors.
Desktop/mobile cost-panel screenshots were inspected: sub-cent losses remain
visible, with no overlapping or overflowing table text. Existing Vite chunk
size and sourcemap warnings remain. This completes a modeled-cost component,
not the provider-specific paper admission contract or profitable strategy.

Final full-backend run: 980 tests and 201 subtests passed in 231 seconds,
including the corrected qualification fixtures from the preceding turn.
There were 28 existing dependency/metadata warnings. Isolated test containers
were removed after completion; the deployed paper/research stack was retained.

## Explicit paper cash comparison policy (2026-09-29)

Migration 0051 adds an exact-v1 default without opting existing accounts into
rounding. The optional alpaca-usd-unambiguous-cent-v1 policy accepts only the
existing strict Alpaca USD diagnostic, preserves unrounded residuals, and
requires the exact adjustment to explain the entire difference. Ambiguous
rounding, wrong currency/provider, and inventory mismatches remain blocked.
Repeated application is idempotent; exact comparison recovers the raw mismatch.

The adoption command refreshes broker evidence before preview/application.
It requires a fresh, baseline-bound v2 reconciliation, flat inventory, no
pending orders and live trading disabled. Adoption records an audit event;
it cannot clear accounting review, change the baseline, verify costs or
authorize execution. Observed-performance calculations use the same policy
but retain their independent account-review and unverified-cost restrictions.

133 focused tests and six subtests passed. Before migration, encrypted backup
b3a7e46134c7ddeab478667753ebcd34706d06db0356349181a9d98f74cb2e0c
verified restoration of 83 tables, 18,350 rows and eight model files. This
backup remains local, not off-host recovery. Backend/workers were rebuilt;
the retained database reports migration 0051_paper_cash_policy.

Fresh preview passed while the persisted policy was still exact-v1. Applied
the explicit cent policy once. Two authenticated broker reconciliations
returned HTTP 200 and matched cash/inventory, residual zero, preserved raw
residual 0.000078362364 and equal rounding adjustment. Journal and baseline
hashes remained unchanged. One cash_policy_adopted audit event exists.
The two SPY orders remain filled at quantity 0.013060394 each; inventory is
flat and broker cash/equity remain $100,000. No new broker order was submitted.

Account status remains halted, costs_known and accounting_verified false,
and prior accounting-review flags remain set. Readiness confirms paper/live
admission false and kill switch enabled. Intraday readiness separately reports
unverified Tradier entitlement and missing final-session intervals. Monetary
matching does not establish profitability or complete the paper-admission
contract. No safety approval or live-money authorization was fabricated.

Final full-backend verification: 998 tests and 201 subtests passed in 203.52
seconds, with 28 existing dependency/metadata warnings. Repeat adoption
preview succeeded against the already-adopted policy. Git diff whitespace
checks passed. Backend, web, PostgreSQL and Redis report healthy; all paper
workers, scheduler and watchdog remain running. No frontend files changed
in this cash-policy update; the prior UI smoke run was not rerun here.

## Independent monetary residual review (2026-09-29)

Added an explicit paper-only monetary review command. It requires a previously
adopted cent policy, fresh baseline-bound broker reconciliation, flat inventory,
no pending or uncertain orders, and an isolated historical monetary-review
halt. It independently replays persisted raw activities and order evidence,
then compares every replay field and the rounding diagnostic against the
fresh broker report. Missing/changed evidence or a newer reconciliation
failure cannot clear the residual. The caller owns the transaction.

This review resolves only unexplained_residual and records one audit event.
It preserves account halt, unknown costs, unverified accounting, recovery
review and execution controls. Repeated application is idempotent. The
reason now distinguishes an explained monetary difference from the remaining
cost/recovery qualification instead of requesting nonexistent fee enrichment
as the only possible explanation for cent rounding.

29 focused tests passed before deployment, including modified/missing fills,
changed orders, uncertain submissions, stale evidence, altered report digest,
wrong policy and unrelated halt rejection. A verified encrypted backup before
deployment restored 83 tables, 18,448 rows and eight model files. Snapshot:
84d924506c8427e57f7fb3ccee160a847667185e1e5e014f27d2bce014edd668.
It remains local, not an off-host backup. The fresh retained-account preview
independently explained 0.000078362364 with unchanged baseline/journal hashes.

The leased IEX collector also completed a real non-synthetic session refresh:
390 AAPL, 383 MSFT, 373 QQQ and 390 SPY observed minute bars. These are upsert
counts, not all newly collected bars. Absent minutes remain unknown. Forward
research now has 277 scored forecasts (75/66/61/75), zero pending and one
expired MSFT outcome. Only AAPL is slightly below neutral Brier 0.25;
MSFT/QQQ/SPY are above it. No model was promoted and no broker order submitted.

Alpaca's current primary documentation still excludes regulatory fees,
dividends and several execution frictions from paper simulation:
https://docs.alpaca.markets/us/docs/paper-trading
Paper monetary matching is not evidence of all-in live profitability.

Final full suite: 1,009 tests and 201 subtests passed in 247.17 seconds, with
28 existing dependency/metadata warnings. The updated recovery-preservation
assertions are included. Applied the reviewed residual after this pass.
Two fresh authenticated reconciliations returned matched monetary evidence,
zero cash residual, no positions, unchanged journal/baseline hashes and
unexplained_residual=false. A second review application was a no-op; exactly
one monetary_residual_review event exists. Broker cash/equity remain $100,000.
Account halt and unknown costs remain; recovery is still cooldown with
accounting_review_required=true and no reviewer approval. Readiness returned
paper=false, live=false and kill_switch=true. Test containers were removed;
the retained app and research workers remain deployed. Whitespace checks
passed. No frontend changes or new UI smoke run in this turn.

## Observed performance during a reviewed monetary halt (2026-09-29)

The reporting path now distinguishes an unresolved account from a reviewed
monetary-only halt. Provisional equity change is available for the latter
only with the expected explicit cent policy, a resolved monetary-review audit
record tied to the frozen baseline, no reconciliation-required/unexplained
flags, a matching latest report, matching equity-snapshot boundaries, and an
independent replay of the full journal and orders. This is a reporting-only
change: no execution gate, account state, recovery state or approval changes.

Tests exercise the full initialization -> fractional round trip -> policy
adoption -> monetary review -> reconciliation path. A restarted session
reports the same provisional result. Missing/altered review, changed policy,
unrelated halt, mismatched report, changed equity, and new cash discrepancies
withhold results. 77 focused backend tests passed, plus TypeScript checking
and all 24 frontend tests. Backend and web were rebuilt and deployed.

The retained API reports observed equity change 0E-8 ($0.00), funding zero,
reported fee subtotal zero and two fills without commission data. The
frontend explicitly calls this provisional, labels the fee amount a subtotal,
and states that total costs are unverified and the account remains halted.
It is not realized net profit or a profitable strategy result.

Playwright passed all 27 sections at 1440/390 widths with no runtime errors,
including the new observed-performance labels, funding/fee fields, and
viewer/write-access boundaries. Both observed-performance screenshots were
inspected: text wraps cleanly without overlap. Existing Vite sourcemap and
bundle-size warnings remain.

Final full-backend verification: 1,021 tests and 201 subtests passed in 220.37
seconds, with 28 existing warnings. The deployed readiness endpoint confirms
paper_trading_allowed=false, live_orders_allowed=false and kill_switch=true.
Whitespace checks passed. Isolated test services were removed after success;
the retained backend, web, research workers and scheduler remain deployed.

## Post-close consolidated data repair (2026-09-29)

Found the scheduled SIP importer called trading preflight directly, which
returns before ingestion outside regular hours. With the one-minute bar plus
60-second late-trade allowance, the final minutes could never be collected
by that schedule after close. A current leased job reproduced the timing
block; a separate leased authenticated ingestion then succeeded for all four
symbols. This disproves a credential failure as the cause of this observed
post-close gap.

Added a scheduled collection wrapper: during the session it uses the existing
authenticated preflight; for one hour after the exchange-calendar close it
uses bounded data repair, records sip_post_close_collection audit evidence,
and always returns ready=false and execution_eligible=false. Other periods
retain the existing blocked preflight behavior. Early closes use the actual
calendar close. Provider configuration/authentication failures remain failures;
the bar-completion allowance is unchanged. Future-dated ingestion timestamps
can no longer establish current entitlement.

The worker uses the new collection wrapper; its real crash-recovery harness
now substitutes at that same boundary. 38 initial focused tests and 17 subtests
passed, including final-minute collection, early close, overnight/weekend
exclusion, provider failure, regular-session delegation and future-clock
rejection. Additional missing-configuration and close-boundary cases were
added to the full run.

Real authenticated repair upserted the newest 60 minutes per symbol and left
zero missing session intervals. Independent database counts show 390 Tradier
SIP bars each for AAPL, MSFT, QQQ and SPY on September 29. Readiness reports
each feed market_closed, no missing intervals, paper execution false and live
orders false. These late-retrieved observations are not retroactively claimed
as timely forward-learning outcomes.

Final full suite: 1,028 tests and 203 subtests passed in 213.47 seconds, with
28 existing warnings, including the real worker-crash tests. Backend/workers
were rebuilt and deployed after the pass. Submitted a task to the real Celery
intraday queue: 79426914-e944-44c7-a935-0ec134b7f39b finished SUCCESS at
20:34 UTC with complete data for all four symbols, post_close_repair scope,
ready=false and execution_eligible=false. No broker order or trading-control
change occurred. Isolated test services were removed. No frontend changes
in this turn; the prior UI smoke run was not rerun.

## Submission-based order-history pagination (2026-09-29)

While examining the paper-qualification evidence contract, found both Alpaca
adapters used updated_at/created_at to advance an orders `until` cursor. The
provider defines sorting/filtering by submission time, so that implementation
could repeat or omit history above 500 orders. Primary reference:
https://docs.alpaca.markets/us/reference/getallorders-1

The reference also documents before_order_id. A real allowlisted paper GET
probe showed that parameter did not return the expected suffix and did not
return empty history after the oldest order. It is therefore not used. The
same two-order account passed exclusive submitted_at `until` and one-nanosecond
overlap probes. No broker mutations or live-endpoint probes were performed.

Replaced the duplicated adapter loops with a shared submission-time collector.
It uses pandas' existing nanosecond timestamp parser, never falls back to an
order's update/create time or the current clock, and overlaps the oldest
timestamp by one nanosecond. Identical overlap is deduplicated; conflicting
records, malformed pages, missing identities, unsorted submission times,
ignored cursors, unadvanceable full tied boundaries and page-limit exhaustion
raise instead of returning partial history. Optional paper `after` filtering
remains on every page. Endpoint/credential separation is unchanged.

96 focused tests and six subtests passed, including both adapters, 1,003-row
histories with tied nanosecond boundaries, and the existing ledger/transport
tests. The retained real account has only two orders: the multi-page scale
test is synthetic and is not described as a real large-history soak.
This fixes a prerequisite to reliable qualification; it does not change
cost completeness, qualification version, activation or recovery controls.

The first full run finished with 1,053 passes, 203 subtests and two failures
in older live-certification fixtures that supplied only updated_at and
expected the old cursor. Those fixtures now use submitted_at, exercise actual
overlap deduplication and reject an unsplittable full tied page. All 37 tests
in the corrected certification/pagination files passed independently.

The rebuilt image made two allowlisted GET-only order-history reads using
separate paper clients. Both returned two orders with identical raw-history
hash e6dae75801f160376c33aec2ac023fbb64b6cc41527b8ff63142230075d3a5c7.
No application database or broker account mutation was made by this probe.

Clean full-suite rerun: 1,055 tests and 203 subtests passed in 218.90 seconds,
with 28 existing warnings. Rebuilt and deployed backend/workers. A fresh
authenticated reconciliation returned HTTP 200, two orders, zero positions,
$100,000 cash/equity, matched monetary evidence and unchanged journal/baseline
hashes. Costs remain unknown and the account remains halted; readiness still
reports paper=false, live=false and kill_switch=true. No order was submitted.
Whitespace checks passed and isolated test services were removed. No frontend
changes; the earlier UI smoke run was not rerun for this adapter-only update.

## Paper-specific observed accounting contract (2026-09-29)

Added paper-research-accounting-v1 as a distinct closed-inventory accounting
observation contract, exposed under paper_research_accounting in the paper
status API. It is not the legacy all-in-fee qualification or an execution
approval. Missing fill commissions remain unknown rather than becoming zero.

The evaluator requires the live-disabled Alpaca USD environment, matching
broker identity/balances and explicit account permission fields, fresh
reconciliation, no unexplained residuals, no unrelated halt, flat inventory,
and no pending/uncertain orders. Persisted order and fill identities, links,
quantities, prices and fee fields must match broker evidence. The complete
activity journal is replayed against the frozen baseline and compared with
the latest persisted monetary report. Versioned closed-inventory cost stress
assumptions must match the same journal. All cost-verification and execution
authorization flags remain false.

Added a command that refreshes real paper evidence and appends a scoped
paper_research_accounting_check audit event. It cannot initialize/reset an
account, submit an order, clear a halt, approve recovery or qualify a venue.
19 focused tests passed, including stale/future timestamps, identity and
balance changes, missing/modified fills, uncertain orders, altered baseline,
changed report digest, and invented zero-commission rejection.

The newly built image ran the command against the retained account. It passed
observed accounting with three activities, two orders and two executions,
preserving the journal and baseline hashes. Both executions lack reported
commissions; missing_fees_treated_as_zero=false. The cost-assumption hash is
b38be6e97156635f2118d5c5ed3783d1708c5fb959c50bba0ee50f39af4e7946.
The result explicitly leaves venue activation, recovery revalidation, current
execution data, and approved strategy/risk limits outstanding. Integration
into a separately authorized paper-research execution contract remains work;
this observation alone never satisfies the legacy admission gate.

Full-backend verification: 1,074 tests and 203 subtests passed in 212.59
seconds, with 28 existing warnings. Deployed backend/workers and reran the
fresh evidence command. The viewer API returned HTTP 200 and the same report
hash as the command: accounting_observation_ready=true, costs_verified=false,
launch_authorized=false, account still halted and no positions. Readiness
remained paper=false, live=false and kill_switch=true. No broker orders,
venue approvals, recovery approval or live authorization were created.
Whitespace checks passed and isolated test services were removed. No UI
changes or new browser smoke run for this backend-only contract update.

## Separate paper-research qualification storage (2026-09-29)

Added paper-research-venue-v1 and migration 0052_paper_research_qual, with
separate qualification and authorization tables. Exact hashed accounting
evidence is bound to the account and revalidated against fresh observations.
An authorization references the exact qualification and requires a distinct
identified authorizer. Database constraints forbid non-paper/live approvals.
Neither record changes execution gates, verifies missing costs, or satisfies
legacy complete-fee admission. The status API exposes these distinctions.

35 focused tests passed. The first full run found a PostgreSQL migration
revision name exceeding its 32-character limit; 1,089 other tests and 203
subtests passed. Shortened the revision before deployment, then retested
SQLite and PostgreSQL migration preservation successfully. A clean full-suite
rerun passed 1,090 tests and 203 subtests in 232.10 seconds, with 28 existing
warnings.

Before deployment, encrypted backup and restore verification covered 83
tables, 20,893 rows and eight model files. Snapshot:
144e128018e5be550d725d443784f2d934acc037aaafd562f734814a208cd27e.
This backup remains local, not off-host. The corrected image built and
migrated successfully; backend and workers restarted.

Actual runtime qualification was refused: the retained account has the halt
"Alpaca paper broker is unavailable or returned invalid data". Subsequent
broker reconciliation succeeded, but that unrelated halt remains and is not
silently cleared by this service. No qualification or authorization was
created. Current API checks returned HTTP 200, zero positions, two historical
orders, paper_trading_allowed=false and live_orders_allowed=false. Recovery
review, current execution data and strategy admission remain outstanding.

The first browser smoke run exposed its assumption that provisional figures
would always be available. Extended it to also verify the genuine halted
state: exact API explanation visible, performance figures absent, execution
and live permission false. The rerun passed all 27 sections at 1440/390px,
viewer access/write protection and temporary permission checks, with zero
runtime errors. No new broker order was placed during this update.

## Reviewed paper broker-read recovery (2026-09-30 UTC)

Confirmed three generic Alpaca read failures after monetary review event 229;
latest failure event 342 occurred at 2026-09-29T21:34:42Z. Added an explicit
paper-only review command, not an automatic execution resume. It requires
independent current accounting replay, the unchanged previously reviewed
journal, two fresh matching reconciliation events after the last failure,
and no intervening manual halt or other failure class. It restores only the
monetary-review halt reason; costs, execution and recovery controls remain
unchanged. Dry-run is the default; applying appends a dedicated audit event.

46 focused tests passed after correcting timestamp normalization for SQLite
second-resolution event times. Rejection coverage includes stale/missing
observations, changed journals, unexplained residuals, live configuration,
blocked broker permissions, missing original review and unidentified actors.
Verified encrypted backup snapshot before applying:
329fdae9101f5504ea60aef085f8c3c3e60efc8bbc42f65b7746d2766b06e787
(85 tables, 20,991 rows, eight model files; still local-only).

Real-account dry-run passed with events 794/796. Explicit apply fetched again
and used events 800/802 at approximately 02:07 UTC, preserving journal hash
1530dd3e6c01cbd0ba1c6b1f8feccb76cfaa2ab1007a500084aa443d25654340.
Then recorded research qualification 1 with report hash
558de0b06ba903a3c0fe1f28d2f25ce2b1bf048c4c0dfed6e9d7d2efbfdda196.
The deployed API confirmed accounting readiness and current qualification,
but activation_authorized=false, execution_authorized=false and
live_authorized=false. Account remains halted with the monetary-review
reason, zero positions and two historical orders. No broker order or
independent activation approval was created. Qualification validity remains
dependent on fresh reconciliation; this is not permanent trading admission.

Full backend suite: 1,101 tests and 203 subtests passed in 251.08 seconds,
with 28 existing warnings. Deployed browser smoke passed 27 sections at
1440/390px, access controls, temporary permission checks and zero runtime
errors. Removed isolated validation services only; retained paper services
continue running. Whitespace checks passed.

## Paired forward strategy comparisons (2026-09-30 UTC)

Added iex-direction-comparison-v1 declarations to newly issued forecasts for
AAPL, MSFT, QQQ and SPY. Comparators are the existing per-symbol online SGD
learner, fixed five-minute momentum and reversal benchmarks, historical class
frequency, and neutral 50/50. Benchmark probabilities are deliberately fixed
weak forecasts (0.6/0.4 beyond +/-0.05 percent, otherwise 0.5), not calibrated
probabilities or executable trading strategies. Scoring uses identical
forward outcomes and reports paired Brier/log loss. No profit/return measure,
model promotion, fee assumption or broker order is created.

Declarations are persisted before outcomes and verified against their fixed
versioned rule on scoring. Legacy forecasts, expired targets, altered scores
and unpaired evidence are excluded. Prior SGD training behavior is unchanged.
Primary library reference checked:
https://scikit-learn.org/dev/modules/generated/sklearn.linear_model.SGDClassifier.html
and its linked scikit-learn GitHub implementation; retained the installed
SGDClassifier instead of replacing its incremental learning implementation.

20 focused backend tests passed. Two new UI rendering tests and TypeScript
checks passed using installed tools directly after the pnpm wrapper hit an
existing ignored-build-scripts approval error. Dependency policy was not
changed. Backend and production web images built successfully and deployed.
The API returned HTTP 200, collection enabled/fresh, existing scored counts
75/66/61/75 respectively, and zero paired observations for all symbols. This
is expected before the next eligible market-session forecasts and is not a
claim of real-world benchmark performance. New comparison data is visible
under each symbol's Forward Learning section, with an explicit empty state.

Full backend suite passed 1,106 tests and 203 subtests in 248.98 seconds,
with 28 existing warnings. Browser smoke passed all 27 sections at desktop
and mobile widths, including the new comparison region and no runtime
errors. Inspected desktop and 390px screenshots: the empty state and labels
fit without overlap. Populated metric rendering is covered by component
tests; real forward comparison outcomes are not yet available. No broker
orders or trading permissions changed in this update.

## Bounded multi-symbol broker probe (2026-09-30 UTC)

Extended the explicit Alpaca paper integration probe with --symbol restricted
to AAPL/MSFT/QQQ/SPY. Each invocation remains a single $10-notional buy followed
by an exit of exactly its confirmed fractional fill. This is a broker test,
not a strategy signal, parallel portfolio runner, or profitability claim.
The CLI still requires a reconciled ledger, unique durable run journal and
explicit simulated-money confirmation; no existing admission gate changed.

Hardened preflight to require explicit USD account permission fields, finite
cash/buying power, exact active US-equity asset identity and fractionability,
and a timezone-aware broker clock no more than 30 seconds old with at least
five minutes before regular close. Recheck that clock immediately before the
buy after order-history/client-ID lookups. Order polling now verifies side
and broker ID in addition to client ID and symbol. Lost POST acknowledgments
are still resolved by lookup, never by resubmission.

24 focused tests passed, covering all four symbols, lost responses, existing
orders/positions, account mismatch, permission failures, stale/future/naive
clocks, nonfinite balances, session closure during lookup, wrong order side,
and uncertain buy outcomes. Built the production image and invoked its
preflight against the actual account through a client that forbids non-GET
requests. All four symbols passed preceding account/asset/flatness checks
and stopped at the closed regular-session gate. No orders were submitted or
queued. Actual non-SPY round trips remain unverified until a permitted open
session and the ledger gate both pass.

Provider references checked:
https://docs.alpaca.markets/us/docs/fractional-trading
https://alpaca.markets/sdks/python/api_reference/trading/models.html
https://docs.alpaca.markets/us/docs/orders-at-alpaca

Full backend regression passed 1,122 tests and 203 subtests in 265.81 seconds,
with 28 existing warnings. Rebuilt/deployed backend workers. Post-deployment
paper status and readiness APIs returned HTTP 200, zero positions, the same
two orders, paper_trading_allowed=false and live_orders_allowed=false.
Whitespace checks passed. No frontend changes or new browser smoke run for
this CLI-only update. Removed isolated validation services, not retained data.

## Durable probe coordination and research admission (2026-09-30 UTC)

The standalone probe previously released its ledger session before broker
execution. Added reserve_probe, which shares the account row lock used by
normal order reservations, rejects persisted positions/pending/uncertain
orders, and commits a dedicated paper_probe_reservation audit and account
halt before the first possible broker POST. Recovery accounting review is
required and prior review is invalidated. A process failure after this
commit cannot leave the database advertising the old reconciled state.
Run IDs cannot be reserved again, even after a later ledger recovery.
The existing fsynced journal and no-POST-retry behavior remain in place.

Explicit $10 integration probes can now use either a reconciled ledger or
freshly passing paper research accounting. The latter permits the specifically
reviewed monetary state with unknown commissions, not unrelated halts. The
audit binds the accounting digest and labels the operation as an explicit
bounded integration probe. It does not authorize strategy trading, waive
broker/market-session checks, verify costs or grant live authority.

Focused tests: 33 passed, one SQLite-only row-lock test skipped. The equivalent
PostgreSQL concurrency test executed successfully: the second transaction
received SQLSTATE 55P03 while the first held the row lock, then a subsequent
attempt was refused against the durable halt. Tests also verify persistence
across sessions, no database mutation on closed-session refusal, single-use
run IDs, research-account admission and preservation of unrelated halts.

The built image refreshed actual paper evidence through a GET-only client.
Its AAPL reservation attempt passed research accounting and was refused at
the closed-session gate. Reservation count was unchanged and the account
retained its monetary-review halt. No broker order or reservation was created.
This verifies the closed-session path, not an open-session round trip or
complete crash recovery of a partially filled probe.

Full backend suite: 1,131 tests and 203 subtests passed in 314.73 seconds,
with 28 existing warnings and one expected SQLite row-lock skip. PostgreSQL
row-lock behavior was tested successfully. The image was deployed and the
backend became healthy. No frontend changes were made in this update.

## Interrupted probe recovery and GitHub reuse (2026-09-30 UTC)

Reviewed primary sources from similar open-source projects:
- https://github.com/alpacahq/alpaca-py/blob/master/alpaca/trading/client.py
- https://github.com/freqtrade/freqtrade/blob/develop/freqtradebot.py
- https://github.com/QuantConnect/Lean.Brokerages.Alpaca

Alpaca's SDK uses stable client-order-ID lookup and validated request models.
Freqtrade refreshes exchange order state before reconciliation and distinguishes
entry/exit cancellation and partial fills. LEAN's Alpaca integration is a
broader engine adapter, not a drop-in replacement for this Python service.
No Freqtrade or LEAN source was copied. Reused the already pinned official
alpaca-py 0.44.0 MarketOrderRequest validator (installed license metadata:
Apache-2.0), retaining original decimal strings rather than serializing the
model's float quantities. No dependency version or transport endpoint changed.

Added scripts.recover_paper_probe. Default invocation inspects the durable
journal and exact original client IDs without submitting. Explicit --apply
and --confirm SIMULATED_MONEY_ONLY can submit only a never-attempted sell
whose quantity exactly matches a terminal buy and the sole broker position.
The exact account-bound reservation and probe halt are required for apply.
Original and recovery processes take the same exclusive journal lock. Intent
is fsynced before the sell; lost responses are resolved by lookup, not repost.
No buy path exists. Costs remain unknown and account/recovery halts persist.

Unseen orders with durable intent remain uncertain, not assumed canceled.
Pending buys/sells and partial terminal exits are classified for further
reconciliation, not canceled or replaced automatically in this version.
This is not yet a complete autonomous cancel/replace/remainder-exit engine.
Replaced orders, mixed inventory, mismatched identities, unrelated open orders,
permission failures and closed sessions cannot trigger recovery submission.
Incomplete journal tails and concurrent journal access fail closed.

53 focused tests passed, one expected SQLite lock skip. Coverage includes
single-exit recovery, repeated invocation, lost acknowledgments, uncertainty,
pending/partial outcomes, ownership mismatches, permissions, journal locking
and exact wire-quantity preservation through the official SDK validator.
The newly built image inspected actual completed run
25a48ccc-38d2-428e-bf44-723d273a8846 and returned observed_flat for quantity
0.013060394, with buy_submitted=false, costs_verified=false and
automatic_resume=false. This actual check made no new broker order; the
apply branch is covered by simulated broker tests, not a new real-account exit.

Full backend regression: 1,151 tests and 203 subtests passed in 219.94 seconds,
with one expected SQLite lock skip and 28 existing warnings. The production
image built and deployed successfully. Post-deployment status returned HTTP
200, zero positions and the same two historical orders; the account remains
halted for separate accounting/recovery qualification. No frontend changes
or new UI smoke run. Whitespace checks passed and isolated tests were removed.

## Confirmed cancellation and partial exits (2026-09-30 UTC)

Extended reserved paper-probe recovery to cancel its own pending buy or exit.
A DELETE acknowledgement is not evidence of cancellation: recovery polls the
original client order ID until a terminal state confirms final cumulative fills.
Changed broker IDs, decreasing fills, replaced orders, missing order evidence
and cancellation timeouts fail closed without a follow-up sell.

After terminal partial exits, recovery subtracts confirmed fills and requires
the sole broker position to match the exact remainder. Up to three total exit
attempts use distinct deterministic client IDs and fsynced submission intents.
An existing intent is never reposted. The account-bound reservation, explicit
apply flag, permissions, journal lock and trading-session checks remain required.
No buy is submitted by recovery; no halt is cleared and costs remain unverified.

Focused regression: 64 passed and one expected SQLite row-lock skip. New
simulated cases cover fill/cancel races, lost cancel acknowledgements, missing
orders, partial exit remainders and the three-attempt bound. The built image's
read-only check against the completed actual SPY probe returned observed_flat
for 0.013060394 shares, with buy_submitted=false and automatic_resume=false.
No actual broker cancellation or new recovery exit was exercised in this update.

Full backend regression: 1,162 tests and 203 subtests passed in 213.03 seconds,
with one expected SQLite row-lock skip and 28 existing warnings. Deployed the
tested image locally; backend health and dashboard paper-status endpoints
returned HTTP 200. Post-deployment status remained alpaca_paper, halted,
costs_known=false, zero positions and two historical orders. Removed the
isolated test stack. No frontend changes or new UI smoke run in this update.

## Forward shadow economics (2026-09-30 UTC)

Reviewed primary-source guidance:
- https://docs.freqtrade.io/en/latest/lookahead-analysis/
- https://github.com/alpacahq/alpaca-skills/blob/main/skills/trading-api/backtest/SKILL.md

Used the principles of future-data invariance and explicit execution/friction
assumptions, not copied strategy code or a claimed profitable strategy. Added
iex-long-cash-shadow-v1 to newly issued IEX forecasts for AAPL/MSFT/QQQ/SPY.
The declaration freezes a strict probability >0.55 long-or-cash decision for
the existing five predictors, plus an always-long price baseline. Entry is the
next minute's open strictly after issuance; exit is the target minute's close.
Old forecasts are not retrospectively enrolled. Missing or late entry prices
at first target scoring remain unavailable and cannot be repaired into wins.

The report uses observed IEX bar-price proxies, not executable quotes or broker
fills. Hypothetical costs of 0/1/5/10 bps apply separately to entry and exit
notionals. The output is a mean per forecast, not compounded portfolio P&L:
windows overlap, observations are not independent and no capital allocation,
spread, market impact, partial fills or guaranteed execution is modeled.
No claim of profitability, model promotion or execution eligibility is made.

Added a dashboard table with cost selection, unavailable/invalid counts and
explicit hypothetical-return labels. Tests cover decision timing, changing
future data, price snapshots, stale/missing prices, altered evidence, symbol
isolation, legacy exclusions, cash decisions and two-sided cost arithmetic.
37 focused research tests passed. Full backend regression: 1,179 tests and
203 subtests passed in 202.24 seconds, one expected SQLite row-lock skip and
28 existing warnings. Four component tests and TypeScript checking passed.
The initial pnpm test wrapper failed on ignored dependency build scripts;
tests were successfully rerun using the already-installed tsx directly,
without changing dependency approvals. Backend and web image builds passed.

Deployed locally and verified actual API output: collector enabled and fresh,
277 existing direction outcomes across four symbols, zero new shadow outcomes.
Alpaca paper status remained halted, zero positions and two historical orders.
No broker orders, accounting approvals or recovery overrides were made.
Desktop/mobile shadow smoke checks passed for the actual empty state and a
browser-only fixture, including cost selection, no overflow and no page errors.
The fixture was never persisted as research data. Inspected screenshots:
output/paper-ui/shadow-fixture-390.png and shadow-actual-1440.png.
Full UI smoke also passed all 27 sections at 1440px and 390px, including
viewer/write protection and temporary permissions, with zero runtime errors.
Whitespace checks passed; the isolated validation stack was removed.

## Opening-session AAPL probe and delayed fees (2026-09-30 UTC)

Actual authenticated GET checks at 13:25 UTC: Alpaca latest SIP quotes returned
HTTP 403, IEX quotes returned HTTP 200, and Tradier production quotes returned
HTTP 200. These are distinct feeds; the denied SIP endpoint was not silently
replaced with IEX execution evidence. At 13:32:34 UTC the normal Tradier SIP
preflight ingested completed 13:30 bars for AAPL/MSFT/QQQ/SPY and returned ready
for all four, with no missing completed intervals. The launch feed gate passed.
Alpaca's documented latest-SIP subscription requirement is consistent with the
denial: https://docs.alpaca.markets/us/docs/market-data-faq

The actual SPY journal gained three delayed FEE events of -0.01 each; observed
cash became 99999.97. A prior cash mismatch had left the unexplained-residual
flag set. Independent journal replay and monetary-review preview passed under
the already-adopted cent policy. Created and restore-verified encrypted local
backup 92b618a51956a7e59d5f1a1cd21c80f0e2c3aef1292ed17106a27710e2c6a4aa
(85 tables, 24,916 rows, eight model files; not off-host). Applied the scoped
monetary review, then refreshed reconciliation. Research accounting passed;
costs remained unverified and strategy/recovery controls remained unchanged.
49 focused accounting tests passed, including three new delayed-fee cases
requiring exact cash and rejecting either an extra or missing cent.

First AAPL probe bd27b5e2-1641-4917-9761-cb9c83e2b317 stopped during preflight,
before reservation or order submission. Measurement showed broker time ahead
of container time by 0.021183 seconds. Added a bounded wait for positive clock
skew of at most one second, then re-read the local clock and apply the unchanged
0..30-second freshness requirement. Future evidence is not admitted; larger
skew and clocks remaining behind still fail. Explicit test clocks never wait.
83 focused probe/recovery/accounting tests passed, one expected SQLite lock skip.

Run 2bc19cbb-94ec-4a2a-9be3-f0a6cad641ce completed on actual Alpaca PAPER:
- Reserved the account and durably halted ordinary execution before submission.
- AAPL market buy with $10 notional cap filled 0.030055779 at 332.382.
- Sold the exact quantity at 332.328; fills observed at 13:32:10/11 UTC.
- Broker reported flat; subsequent read-only recovery inspection observed_flat.
- Gross price-based change was -0.001623012066, excluding unverified costs.

Fresh reconciliation imported both orders/fills and matched cash and inventory.
Account totals: four orders, four fills, zero positions, cash 99999.97, costs
unknown. Journal SHA256:
7bf25cee8670a8198b70d1c63c632d072fa7449de5c14f69db734500fc291745
The probe-review halt intentionally remains. No strategy launch, recovery
approval, live order, or claim of profitability was made. Provider qualification,
risk and recovery gates still block normal autonomous execution.

Full backend regression passed: 1,187 tests and 203 subtests in 321.70 seconds,
one expected SQLite row-lock skip and 28 existing warnings. All 27 UI sections
passed desktop/mobile checks after the trade with zero runtime errors. Updated
the smoke test's provisional-performance fee count to derive it from the API
instead of assuming two fills. No production frontend code changed this turn.

Post-deployment continuation check at 2026-10-01 20:48 UTC: backend health
returned HTTP 200, and the updated 27-section desktop/mobile smoke completed
with zero runtime errors. Collector polling was fresh. Actual stored direction
outcomes totaled 776; new shadow paired counts were AAPL 133, MSFT 132, QQQ 101,
SPY 130 (496 total), with three unavailable QQQ entries and no invalid entries.
All four momentum and reversal means were negative at hypothetical 5 bps per
side. The online learner took zero long shadow actions under the predeclared
0.55 threshold; cash returns are not evidence of a profitable trading model.
These remain overlapping price-proxy experiments, not broker or portfolio P&L.

Latest actual paper account remained flat with four orders. Cash was 99999.95;
the latest cash and inventory equations matched, but a subsequent cash change
had reinstated the historical-residual review flag and reconciliation-required
state. No additional monetary review or trade was applied in this final check.
Earlier Sep 30 readiness and cash figures above are historical observations,
not current approval. Whitespace checks passed and the test stack was removed.

## Preserve probe review across monetary review (2026-10-01 UTC)

Found that a later fee/cash mismatch could overwrite the probe halt with the
generic historical-residual reason. Monetary review then previously restored
the normal monetary-review reason without accounting for the probe reservation.
Moved PROBE_HALT to the ledger service and made monetary review explicitly
preserve account-bound reserved probe evidence (including its reservation ID).
An existing probe halt is also retained if its reservation cannot be found.
Preview does not mutate state; repeated apply is idempotent. This does not
discharge any probe reservation or authorize execution. A dedicated completed-
probe review is still needed before that separate halt can be resolved.

Alpaca documents that non-trade fees can be created after the trade date:
https://docs.alpaca.markets/us/reference/getaccountactivities-2
This supports retaining full-journal reconciliation rather than assuming that
fees are complete at fill time. No fee completeness claim was introduced.

76 focused tests passed with one expected SQLite row-lock skip. New cases
cover monetary, historical-residual and probe halt starting states, preview
nonmutation, repeated apply, and a probe halt lacking a reservation row.
Created encrypted, restore-verified local snapshot
1d4fccc988b7fb295a396ceb1efcc8885418903192ed22db2f8d10c4e4f4845f
(85 tables, 49,206 rows, eight model files; not off-host).

The new image's actual monetary-review preview and apply passed for journal
20f320d38d441694132758d54c2730e970f2821529331d7f71f6ca29abc1349b,
preserving probe reservation 1488. Unrounded residual 0.001701374430 exactly
matched the existing cent-policy adjustment. Subsequent reconciliation showed
cash 99999.95, zero positions, four orders, unexplained_residual=false and
reconciliation_required=false. The account remained halted for the reserved
probe; research accounting correctly refused admission under that halt.
No broker orders, fee approvals, strategy launches or live actions occurred.

Full regression: 1,191 tests and 203 subtests passed in 247.52 seconds, one
expected SQLite row-lock skip and 28 existing warnings. Deployed the tested
image; backend health and paper-status endpoints returned HTTP 200. Post-deploy
state retained the same probe halt, cash, four orders and zero positions, with
both unexplained_residual and reconciliation_required false. No frontend
changes or new UI smoke run in this update. Whitespace checks passed and the
isolated test stack was removed.

## Completed paper-probe review (2026-10-01 UTC)

Added scripts.review_completed_paper_probe and paper_probe_review. The command
holds the existing journal lock, rejects symlinks/incomplete or oversized
journals, refreshes broker reconciliation, and locks the paper account before
review. Preview is nonmutating except for normal reconciliation observations.
Apply requires --confirm PROBE_REVIEW_NOT_TRADING_APPROVAL.

Completion requires the latest exact bounded reservation, matching journal
reservation and client IDs, filled-and-flat recovery inspection, fresh full
accounting replay, matching probe orders and complete broker order history,
unchanged cash/equity and explicit broker permissions. It rechecks accounting
freshness after broker reads. Review code has no submit or cancel path.
An append-only hash-bound review references the reservation and journal; no
historical reservation or trade evidence is deleted. Invalid/altered reviews
do not discharge reservations. Later monetary review respects valid completion
records, while new reservations remain independent. Repeated completion is a
no-op and never clears a subsequently imposed unrelated halt.

The account remains halted under the normal monetary/research-review reason;
global recovery, strategy, risk, venue activation and live controls are unchanged.
The internal probe accounting scope does not change public research admission
or accept simultaneous transport/probe review scopes.

73 focused tests passed. Full backend suite: 1,216 tests and 203 subtests passed
in 320.88 seconds, one expected SQLite lock skip and 28 existing warnings.
Verified encrypted local backup before actual apply:
eded4899ec37b426fb50e96393d9d5651d5c86aa3f51c1f0d7779965afde1c36
(85 tables, 49,304 rows, eight model files; not off-host).

Actual AAPL run 2bc19cbb-94ec-4a2a-9be3-f0a6cad641ce preview and apply passed.
Reservation 1488 was reviewed by completion event 4090; repeat apply returned
already_reviewed and state_changed=false. Probe journal digest:
da4e81baef9785b541c241716ff75d144254ef84b7fdf42e500638ae0eca92ce
Completion report digest:
b16ec1b3f92b090344c843206fb53109a564e6678b083034b53c6c9a8b35a218
Research accounting then returned observed_ready. Paper cash remained 99999.95,
with four orders and zero positions. Provisional observed equity change was
-0.05 with reported fees 0.05 and four fills lacking reported commissions.
Costs remain incomplete, performance is nonqualifying, and no orders or live
actions were made by review. No independent strategy approval was fabricated.

Continuation on Oct 2: the pending UI smoke completed all 27 sections at
1440px/390px with zero runtime errors. An overnight isolated broker-read
failure had imposed a transport halt without changing orders or positions.
Added the same pending-probe preservation to transport recovery; 74 focused
review/accounting tests passed. Actual transport preview passed two fresh
matching observations, and apply preserved no pending reservation because
completion 4090 remained valid. Fresh research accounting returned observed_ready.
No execution or recovery approval was granted. Restore-verified local encrypted
backup before apply: 3e2c4c0eed773205081724f6265178b1758705103915459cf0f96e4ffd8ca948
(85 tables, 56,363 rows, eight model files; not off-host).

The Oct 2 rebuild resolved a newer upstream Python base and indirect package
versions. Rebuilt the isolated test image FROM the actual application image
sha256:aac382918a6a4f8a34ba3eac199ec60b88d71f979dd0633ed3c734739b55a895
to validate the runtime being deployed, rather than relying on an older cached
test runtime. No dependency file or intended feature version was changed.

Final rebuilt-runtime regression: 1,217 tests and 203 subtests passed in
249.59 seconds, one expected SQLite lock skip and 28 warnings. pip check found
no broken requirements. Deployed the final image and verified HTTP 200 health.
Post-deploy completed-probe inspection returned already_reviewed for event 4090
without changing state. Fresh research accounting passed; zero positions,
four orders, cash 99999.95, and no unexplained/reconciliation-required flags.
Normal monetary/research-review halt remains, with no execution authority.
Whitespace checks passed and the isolated validation stack was removed.
## 2026-10-05 Oracle instance creation attempt

- Generated and selected the dedicated SSH public key `frozen-stock-paper.pub` in the Oracle create-instance form.
- Configuration remained within the Always Free-eligible A1 limit: `VM.Standard.A1.Flex`, 2 OCPU, 12 GB RAM, Ubuntu 24.04 Minimal aarch64, public IPv4, and no extra block volumes.
- Submission was rejected by Oracle capacity, not by authentication or configuration: `Out of capacity for shape VM.Standard.A1.Flex`.
- Retried all three Ashburn availability domains (`AD-1`, `AD-2`, and `AD-3`); each reported the same capacity error.
- Verified the visible fallback `VM.Standard.E2.1.Micro` is Always Free-eligible but disabled for the selected aarch64 image. Using it would require changing the image and would provide only 1 GB RAM, which is not an adequate target for the planned Docker/Redis application without a deliberate low-memory deployment profile.
- No Oracle compute instance was created. No paid resource or account upgrade was selected.

## 2026-10-05 Oracle Always Free fallback submission

- Reconfigured the form to use the compatible x86 `Canonical Ubuntu 24.04 Minimal` image and `VM.Standard.E2.1.Micro` in `AD-2`.
- Oracle labels this shape `Always Free-eligible`; the configuration is 1 OCPU, 1 GB RAM, public IPv4, the existing restricted SSH rule, the selected deployment key, and no extra block volumes.
- Oracle accepted the create request and completed it successfully. The instance is `Running` in `AD-2`; SSH connectivity was verified with the generated key.
- The $2/month estimate is the standard boot-volume list estimate; no paid upgrade or paid shape was selected. Final cost/account verification remains required after provisioning.
- Installed Docker Engine 29.1.3 and Compose 2.40.3 on the VM and enabled a persistent 2 GB swapfile to protect the 1 GB host from immediate memory exhaustion.

## 2026-10-05 Oracle micro runtime trial and Replit redirect

- Provisioned fresh server database/role keys. Exported only paper Alpaca and Tradier market-data credentials, not live, local role, Clerk, AI, or Tradier trading keys. Added three passing export/permission/no-overwrite tests.
- Full-stack Compose safety checks passed: live disabled, loopback-only web/API ports, numerical-library threads limited to one, prefork task timeouts retained, and learning concurrency fixed at one.
- A disposable cloud test container passed 82 research/data/Celery tests and four subtests. The first test attempt lacked pytest; the corrected harness installed it only in the disposable container.
- First fresh Alembic migration failed because macOS AppleDouble `._*.py` metadata entered the package. Added Docker-context exclusions, repackaged without that metadata, rebuilt, and verified migration exit 0 and successful schema-checked API startup.
- Compiled the paper-auth web bundle locally (2.82 seconds) and built the nginx runtime image on the VM. Cancelled the resource-heavy remote frontend build.
- Private SSH dashboard tunnel returned paper-only health with live trading false. The full worker stack nevertheless produced a 20-second data-status timeout with 962 MB swap in use. This is not acceptable trading deployment evidence.
- Stopped the new cloud scheduler, intraday, learning, execution, risk, and watchdog containers. API, web, database, Redis, and idle market-data worker remain; no continuously collecting cloud deployment is claimed.
- Added an observation-only schedule/configuration; nine local Celery tests and four subtests passed, and the merged observer service/flag/queue checks passed. This profile has NOT been deployed or qualified remotely.
- User redirected to a paid Replit account. Existing `.replit` is Autoscale, which can scale to zero. Replit Reserved VM is the relevant always-on option; project access, persistent storage, production authentication, machine sizing, and an approved cost limit still need qualification.
- Cloud UI sweep, sustained-learning proof, backup/restore and reboot recovery checks remain unfinished. No profit, live-money readiness, or uninterrupted 24/7 operation is certified.

## 2026-10-05 Pre-push regression verification

- Deployment-matching Linux suite: 1,328 tests and 203 subtests passed, nine skipped, 28 warnings, in 415.54 seconds. Disposable PostgreSQL/Redis validation services were removed afterward.
- Frontend: all 31 component/navigation tests and all three role-boundary tests passed. Added the missing direct test-runner dependency and a complete frontend test script; dependency installation succeeded.
- Production frontend build and TypeScript verification passed. Existing bundle-size and sourcemap warnings remain.
- Native macOS suite is not fully passing: 1,274 passed, 41 skipped, and 22 failed. Two failures require an unavailable native Redis server; 20 involve numerical-library runtime warnings during training. The numerical protections were not bypassed; Linux verification above passed.
- Source staging excludes private environment files, keys, databases, generated models, local research output, and screenshots. Exact private-value and credential-pattern scans found no secrets in the staged source.
- These tests do not certify the published Replit runtime, sustained learning, profitability, or live trading readiness. Git synchronization is separate from publishing a new deployment.
