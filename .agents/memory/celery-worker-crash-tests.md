---
name: Celery worker crash tests
description: Test-harness guidance for integration tests that launch and terminate real Celery workers.
---

Real-worker lifecycle tests should use a dedicated Celery client for task submission rather than mutating the application's shared Celery instance.

**Why:** Other tests may temporarily change the shared app's broker or result-backend configuration; reusing it makes crash tests depend on test order and can send work to the wrong broker.

**How to apply:** Keep the worker subprocess configured through its environment, and create an isolated client pointed at the test Redis instance for publishing tasks and reading results.