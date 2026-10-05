# Remaining-blocker solutions

## Current acceptance audit: 2026-10-03

The original research below is retained as a dated roadmap. Current evidence
does not establish the complete autonomous-profitable-trading objective:

| Requirement | Current evidence | Still required |
| --- | --- | --- |
| Multi-symbol forward learning | AAPL/MSFT/QQQ/SPY, 1,264 validated direction forecasts; scheduled collector observed running | More prospective sessions and a demonstrated improvement over benchmarks |
| Economic strategy evaluation | 24 non-overlapping return-challenger pairs; every challenger action was cash | Traded prospective observations, defensible costs and portfolio-level evidence; cash-only zero returns are not trading profit |
| Paper broker execution | Three previously completed bounded integration round trips; six fills, all positions closed at last review | Strategy-driven execution qualification; integration fills alone do not qualify a strategy |
| Accounting | Reviewed cash/inventory policy and current observed-research qualification | Complete account-specific cost evidence, separate activation and recovery gates |
| Reliability | Tested worker locks, crash recovery, encrypted local restore and macOS startup support | Remote deployment, off-host recovery and actual host-reboot proof remain unestablished |
| Profitability | Direction Brier scores exceed the neutral 0.25 benchmark on all four symbols | No profitable strategy is demonstrated; additional workers cannot supply this evidence by themselves |

At 22:08 Eastern, Alpaca's paper clock reported closed, next open October 5 at
09:30 Eastern. The launch API showed feed verification pending a regular session,
accounting/provider qualification failures, risk kill switch and recovery gates.
Scheduler, notification and audit-chain checks passed. No gates were bypassed.

An intervening broker-read failure made observed research qualification stale.
The existing transport review preview passed with fresh reconciliations
9144/9142. Applying the same checked workflow produced reconciliations 9148/9146
and restored current research qualification, without clearing the halt, claiming
known costs or granting activation/execution authority. The journal remained
unchanged. This was operational recovery, not a new strategy approval.

Next decision point is new prospective market evidence and the outstanding
account-specific qualification contract, not repeated optimization of this small
sample until a profitable-looking result appears. Existing collection remains
running. No new orders were submitted during this audit.

Researched 2026-09-29 against the local implementation and current upstream
documentation. This is an implementation roadmap, not a claim that these
integrations are installed, qualified or deployed. No orders, account changes,
paid resources or third-party setup scripts were run for this review.

## 1. Alpaca accounting and paper venue qualification

Use the [official Alpaca SDK](https://github.com/alpacahq/alpaca-py) where its
typed interfaces fit, but keep provider-specific accounting in our adapter.
The [US account-activity contract](https://docs.alpaca.markets/us/docs/account-activities)
does not promise a commission field on each FILL; nontrade activities may have
only a date. Fees can be separate activities. An SDK swap alone cannot fix this.

Implemented: `0049_alpaca_activity_ledger` and the opt-in `alpaca-activities-v2`
ledger contract use execution timestamps for fills, effective dates for cash,
and dated reconciliation observations. A frozen observed baseline supports full
history replay, including late events, without assuming an account began empty.
Existing accounts retain legacy behavior. Account-level fees are not attributed
to fills, missing history/corrections halt, and costs remain unknown. See README
for the disposable real-account probe. Remaining: account-specific cost evidence,
provider qualification and a controlled actual paper-fill exercise. Matching
cash/inventory alone is deliberately not trading authority.

Acceptance: replay the same complete activity history twice without duplicate
cash/positions; test partial fills, separate fees, dividends, corrections and
date-only cash entries. Reconcile broker cash, positions and orders, then run
account-specific venue qualification. Synthetic fixtures prove parser behavior,
not that the connected account is qualified. A later controlled paper execution
exercise needs actual fills; read-only connectivity cannot supply them.

## 2. Free data versus execution-feed requirements

[Alpaca's FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) documents free
live IEX and historical SIP queries with an end at least 15 minutes old.
[Tradier](https://docs.tradier.com/docs/market-data) distinguishes delayed sandbox
data from real-time brokerage-account data. Neither is evidence that our current
credentials have real-time consolidated-feed entitlement.

Implemented: separately labeled delayed-SIP importer and control-room comparison
panel, with explicit 16-minute cutoff, bounded pagination, provenance, redacted
entitlement errors and execution-feed isolation. The actual credential probe
imported 372 observations twice without adding duplicates. IEX remains the input
to the forward learner; delayed bars cannot leak into its outcomes or satisfy
the live SIP gate. Corporate-action/adjustment lineage and any expanded model
training/evaluation contract still need separate validation.

For execution, either verify an entitled real-time source or deliberately design
and qualify a separate IEX-only paper contract. The latter must disclose the
signal/fill-feed mismatch and cannot silently relax existing SIP requirements.
No GitHub repository removes a vendor entitlement restriction.

Acceptance: unavailable or stale data blocks the relevant execution lane; source,
delay and adjustment metadata survive snapshots, reports and restart.

## 3. Free persistent hosting

Use [Oracle's maintained Terraform provider](https://github.com/oracle/terraform-provider-oci)
to provision a repeatable VM/network deployment, then deploy the existing ARM64
Compose stack. This avoids adopting an unrelated hosting framework.
[Current Oracle limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
list A1 free allowances equivalent to 2 OCPUs/12 GB for Always Free tenancies.
Confirm account-wide usage and the console's eligibility before provisioning.
Capacity failures remain possible; Oracle recommends another availability domain
in the home region or trying later. Terraform cannot create missing capacity.
Idle free instances may be reclaimed. No guaranteed uninterrupted free host was
established by this review.

Keep databases private, restrict SSH, use private credentials and protect Terraform
state. Do not put secrets in committed variables or cloud-init. Start with one
learning process and benchmark resource use before adding concurrency.
[Render free web services](https://render.com/docs/free) spin down after inactivity,
so they are not a substitute for this always-running worker stack.

Acceptance: approved free resources, healthy remote stack, reboot recovery,
external access restricted, restore drill and monitoring. A local running stack
or successful Terraform plan is not a cloud deployment.

## 4. Backup and disaster recovery

Implemented since this review: a consistent dump/model restore drill plus
digest-pinned Restic encryption, full repository checking, exact-snapshot
decryption and hash comparison. Wrong-password and corrupted-pack tests pass.
Storage is still local; off-host recovery and automatic scheduling remain open.

Start with PostgreSQL 16 [pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)
and restore into an isolated disposable database. Add
[pgBackRest](https://github.com/pgbackrest/pgbackrest) for WAL archival and
point-in-time recovery when the deployment is stable; use
[restic](https://github.com/restic/restic) for encrypted model/report file backups.
Storage capacity and off-host storage are separate from free software licensing.

Coordinate database and model-artifact backups with a manifest of artifact
hashes and a documented recovery point. A live filesystem copy of PostgreSQL is
not a substitute for a database-aware backup. Do not blindly replay restored
broker-order queues; recover durable intent and reconcile broker state first.

Acceptance: restore database and referenced model files, verify hashes and
ledger invariants, demonstrate no duplicate order submission, measure data loss
and recovery time. A successful backup command alone does not clear recovery.

## 5. Worker and risk reliability

Implemented since this review: an isolated, digest-pinned Toxiproxy 2.12.0
integration harness testing the actual job wrapper against Redis outages and
latency, PostgreSQL outages and connection resets, lease exclusion and recovery.
The drill exposed and reproduced two bugs: failure notifications committed
pending failed work, and notification outages masked the original exception.
The wrapper now rolls back before failure reporting and preserves the original
error when notification persistence fails. This does not certify broker replay,
silent database blackholes, or host-reboot recovery.

Keep the existing Celery queues, leases and independent risk controls. Review
[Celery task semantics](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
and [Redis visibility timeouts](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html)
against the version actually pinned here before changing acknowledgement policy.
Redelivery can happen; database idempotency must protect side effects.

Use [Toxiproxy](https://github.com/Shopify/toxiproxy) in an isolated test stack to
exercise Redis/PostgreSQL disconnects and delayed responses, plus explicit worker
termination tests. For an ambiguous broker response, query the stable client
order ID before retrying; never assume a timeout means the order was rejected.

Acceptance: stale feeds, lost worker leases, account discrepancies and breached
portfolio limits block new orders; restart never duplicates them. Existing risk
and recovery gates require evidence, not a replacement risk-library toggle.

## 6. Better research and realistic evaluation

Use [Qlib](https://github.com/microsoft/qlib) as an isolated challenger benchmark,
not a wholesale rewrite. Compare on identical immutable data and cost assumptions,
preserve temporal purging and keep the final holdout untouched during selection.
Use bounded rolling validation to compare candidates rather than repeatedly
testing the same final holdout until something appears profitable.

[LEAN](https://github.com/QuantConnect/Lean) supplies an independent reference
engine and [fee/fill/slippage model examples](https://github.com/QuantConnect/Lean/blob/master/Algorithm.CSharp/CustomModelsAlgorithm.cs).
Its value here is checking our execution assumptions. Keep that benchmark separate
from broker-verified outcomes; open-source code does not include all market data.

Acceptance: reproducible baseline comparison, cost sensitivity, data-leakage
tests, portfolio-level risk checks and prospective paper evidence. Learning can
be made more efficient; persistent profitability cannot be promised or obtained
by adding workers alone.

## 7. The nonworking AI research endpoint

[Ollama](https://github.com/ollama/ollama) provides a local option with a
[compatible chat endpoint](https://docs.ollama.com/api/openai-compatibility).
Use an explicitly local model and a private container-reachable hostname, not
the current localhost proxy from another host. Container localhost refers to the
container itself. Model licensing and RAM/latency must be checked independently;
cloud models are not assumed free.

Keep LLM research optional and isolated from order credentials. Require structured
outputs, timeouts and provenance; treat generated code as untrusted. CPU inference
should not starve the trading scheduler. A small Oracle VM may be better used for
collection and classical models, with optional LLM work on the Mac.

Implemented: the paper/research deployment enables an explicitly labeled,
deterministic technical-ensemble fallback when the optional LLM endpoint is
unreachable. It uses only the immutable daily-price snapshot, exposes momentum,
mean-reversion and risk-filter votes, records `provider_available=false`, and
remains research-only with `eligible_for_trading=false`. Malformed provider
responses still fail closed; this fallback does not manufacture an LLM result or
relax any execution gate.

Acceptance: measured inference on target hardware, safe timeout/malformed-output
handling, no secrets in prompts, and no trading failure when the LLM is offline.

## Suggested order

1. Rotate credentials previously shared in chat; do not commit replacements.
2. Implement and test the versioned Alpaca ledger contract.
3. Complete backup/restore and failure-injection drills.
4. Provision and verify Oracle within the account's free allowance.
5. Validate data entitlement and the explicit paper execution contract.
6. Collect prospective outcomes; compare Qlib/LEAN reference results separately.
7. Add optional local LLM research only after resource and safety checks.

These are targeted candidates, not security-audited dependencies. Pin releases,
review licenses and transitive dependencies, and add integration tests before
adoption. No safety or qualification gate was changed by this research.
