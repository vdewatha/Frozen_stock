---
name: Independent watchdog launch
description: How the API stack must launch the stock watchdog without losing the backend import path.
---

The independent stock watchdog is launched from the backend directory but its script lives under scripts; launch it with `PYTHONPATH=.` so `app.*` imports resolve.

**Why:** Running the script directly without the backend directory on `PYTHONPATH` caused the API workflow to fail after migrations completed.

**How to apply:** Preserve the explicit `PYTHONPATH=.` prefix whenever the local stack starts the watchdog as a separate process.