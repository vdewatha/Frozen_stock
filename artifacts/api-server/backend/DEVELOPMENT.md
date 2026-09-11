# Local preview authentication

The API always requires four distinct, random role keys of at least 32
characters: `AUTH_VIEWER_KEY`, `AUTH_RESEARCHER_KEY`, `AUTH_OPERATOR_KEY`,
and `AUTH_ADMIN_KEY`. This applies to local preview and production.

Store these values only in Replit Secrets. Never put them in browser
environment variables, source files, logs, or documentation. Live trading
remains disabled by default.

## Stock-paper reservation concurrency tests

The stock-paper ledger tests use an isolated file-backed SQLite database. They
exercise idempotency and reservation accounting, but SQLite does not implement
PostgreSQL row-level `FOR UPDATE` locking. Do not treat those tests as proof of
multi-process locking behavior. A PostgreSQL locking test must run only against
an explicitly provisioned, isolated test schema/database; never point it at the
application or production database.