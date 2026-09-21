# Alpaca Paper Venue Qualification Package — 2026-09-21

## Decision

**Qualification workflow implemented; no account-specific package is accepted
by default.** The active venue remains unchanged and no order, account reset,
credential read, or provider switch was performed by this package.

Alpaca Paper Trading is the explicitly selected candidate because it has a
separate paper API boundary and an account-activity pagination path. A future
qualification must be recorded against the exact paper account and then
activated by a different authorized operator. Changing the configuration value
alone never activates a venue.

## Required review evidence

The durable package requires affirmative evidence for:

- account identity and configured account binding;
- complete orders and executions;
- explicit commissions, including explicit zero where reported;
- every cash-affecting activity;
- precise, timezone-qualified provider timestamps;
- complete order and activity pagination with a bounded report period;
- delayed activity capture;
- identical restart and session-expiry replays with no lost or duplicated
  activities.

Missing, null, malformed, date-only, or unknown evidence remains blocked. Raw
broker payloads and account identifiers are not stored in the review package;
the account binding is represented by a SHA-256 digest.

## Activation boundary

Qualification and activation are separate immutable records. The activation
record binds the selected provider to the exact qualification report digest,
requires a distinct authorizer from the reviewer, is paper-only, and cannot
grant live authority. The one-session approval and learning-cycle admission
gates require this activated package in addition to the existing reconciled
ledger and safety checks.

## Current disposition

No account-specific evidence package has been approved in this environment.
Until a fresh package passes and is separately activated, the broker
qualification gate remains **FAIL** and another autonomous paper run cannot be
approved.