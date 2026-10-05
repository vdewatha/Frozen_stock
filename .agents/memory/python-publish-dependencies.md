---
name: Python publish dependencies
description: Dependency source of truth and import verification for the Python API on Replit.
---

Python runtime additions must be included in the root Python project manifest
and lockfile, not only in the backend requirements list.

**Why:** Backend requirements were updated without the root install manifest.
Compilation passed, but the published API crashed while importing a missing
NYSE calendar package. Byte compilation does not resolve imports.

**How to apply:** When adding a backend dependency, check both dependency
definitions and use the supported package installer to update the root lock.
Keep an API module import check in the build without invoking startup hooks,
database mutations, external provider calls, or trading actions.
