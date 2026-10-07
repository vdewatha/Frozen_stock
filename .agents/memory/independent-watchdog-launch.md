---
name: Backend script import path
description: Preserve backend package imports when the API stack launches Python scripts directly by path.
---

When launching a Python script directly from the backend directory, set `PYTHONPATH=.` so `app.*` imports resolve. Python sets the script directory, not the backend directory, as the leading import path.

**Why:** A direct launch of the Celery beat helper failed with `ModuleNotFoundError: app`, even though the module-based Celery workers started correctly.

**How to apply:** Keep the explicit prefix on standalone `scripts/*.py` launches in the local stack, including the watchdog and beat helper.