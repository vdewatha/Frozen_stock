# Live compliance readiness

This checklist is a launch gate, not legal, tax, investment, broker, or
regulatory advice. Technical readiness cannot substitute for qualified external
review. The application must remain blocked until the record below is completed
and the required reviewers sign off.

## Current disposition

**Blocked.** The repository does not assert an account owner, jurisdiction,
broker account type, broker permissions, legal/compliance review, tax review, or
broker signoff. Live execution must remain disabled.

The redacted machine-readable record is:

`backend/reports/live-compliance-readiness-20260915.json`

## Required inventory

- Account owner and organizational entity
- Jurisdictions and applicable regulatory/compliance scope
- Broker account type and permissions
- Explicit prohibited activities: unsupported instruments, leverage, shorts,
  options, and unrestricted automation
- Broker agreements, account permissions, pattern-day-trading constraints,
  margin restrictions, market-data terms, and retention obligations

## Records and disclosures

- Authoritative broker statements and tax records must be identified by a
  qualified tax/accounting reviewer.
- Application audit, safety, approval, reconciliation, and recovery evidence
  is supporting evidence; it does not replace broker statements.
- User disclosures must accurately cover automation, model uncertainty, loss
  risk, scope limits, and emergency controls.
- Operator disclosures must define approval, monitoring, incident, shutdown,
  investigation, and periodic-review responsibilities.

## External signoff

The following statuses must be recorded as `signed_off` before the compliance
gate can pass:

- Legal/jurisdictional review
- Tax/accounting review
- Compliance review
- Broker/account-permission review

Signoff records should contain only safe references and reviewer role metadata
in the application checklist. Do not put credentials, account numbers, private
documents, or sensitive personal data in the API payload.