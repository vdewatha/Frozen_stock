# Database lifecycle

Run commands from `backend/` with the intended DATABASE_URL in the environment.
Application startup validates the schema and does not create or alter tables.
For a new empty database, run `alembic upgrade head`, then
`python -m app.db.schema`, then start the application. Render runs the upgrade
as the backend pre-deploy command. Coordinate worker deployments after migration;
never run concurrent migration jobs against the same database.

Supported versioned upgrades: revision 0001 with or without provenance columns,
revision 0002, and either revision with the journal/scanner tables previously
created by `create_all`. Revision 0002 backfills unknown provenance; this does not
make those historical prices trusted. Existing trusted source values are retained.
Revision 0003 adds the missing tables and lookup indexes. Online migration is
required because compatibility checks inspect the existing database.

Before upgrading an existing deployment: stop writers, take a database backup,
restore that backup into an isolated staging database, run upgrade and schema
validation there, and verify record counts and representative trading records.
Only then apply the same migration to the deployment and restart writers.
The migration tests never open the project database; every test has a new
temporary SQLite file.

An unversioned nonempty database is deliberately not automatically stamped.
Restore a copy in staging and inventory its schema with an operator. If it is
exactly the 0001 schema (possibly with journal/scanner tables and provenance),
the operator may stamp 0001 on the staging copy and run the documented upgrade.
Any other schema requires a reviewed data migration. Do not blindly stamp head:
that records a claim without applying schema changes. Startup checks both the
revision and actual ORM schema (columns, types, nullability, indexes, uniqueness
and foreign keys); server-default differences are intentionally ignored because
the ORM uses application defaults for several fields.

Rollback: revision 0002 -> 0001 retains provenance columns and data because 0001
already declares them. Revision 0003 is forward-only to protect journal evidence.
Restore a verified backup for rollback; do not automatically drop audit tables.
SQLite is exercised locally. PostgreSQL 16 was also validated in a disposable
Docker container: fresh chain through 0003, upgrade to 0004, repeated upgrade,
ORM schema parity, registry rollback and concurrent registration all passed.
This found and repaired redundant PostgreSQL unique constraints on assets and
strategies: uniqueness remains enforced by their ORM-aligned unique indexes.
No live database was used. The test database, container and its anonymous volume
were removed afterward.

Revision 0004 adds `research_model_runs` for unique experimental model identities,
complete training manifests and artifact digests. It includes database checks
that reject live status and trading eligibility. Like 0003, it is forward-only;
rollback requires a verified backup. Fresh upgrades and upgrades from 0003 run
the same create-table migration. A legacy `create_all` fixture must represent
historical tables, not include new tables from future ORM versions.

To reproduce PostgreSQL validation, start a disposable `postgres:16-alpine`
container with no user mounts, `POSTGRES_HOST_AUTH_METHOD=trust`, and an ephemeral
port bound only to `127.0.0.1`. Discover its assigned port with `docker port`, then
run `python backend/scripts/verify_postgres_registry.py --port PORT` from repository
root. The script creates a unique database and drops only that database in its
cleanup block. Remove the exact test container and anonymous volume afterward.
This passwordless setup is exclusively for an isolated local test container.
