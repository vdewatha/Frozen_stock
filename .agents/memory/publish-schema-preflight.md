---
name: Publish schema preflight
description: Why release validation must compare checked-out ORM and migration expectations to development before trusting a publish diff.
---

Validate the development database against the checked-out migration head and ORM before treating an empty development-to-production schema diff as release readiness.

**Why:** A Git import can bring in a migration without applying it to development. Both databases can then lack the new table, producing an empty publish diff even though the new application fails its startup schema check. Import-only builds and isolated test databases do not detect this.

**How to apply:** During release preflight, use read-only migration-head and schema checks without running application startup hooks or workers. Apply any approved pending migrations through the development-side migration flow, then inspect the publish diff again. Let Publish apply managed production schema changes; never add production DDL to build or startup commands.
