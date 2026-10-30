# [Project name]

_Replace the heading above with the project's name, and this line with one sentence describing what this app does for users._

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

_Populate as you build — short repo map plus pointers to the source-of-truth file for DB schema, API contracts, theme files, etc._

## Architecture decisions

- One database per paper broker account. The workspace (development) and the published deployment each bind their own Alpaca paper account; neither reconciles an account whose orders the other places. Secrets are shared between environments, so development binds its account through `DEV_PAPER_ALPACA_API_KEY` / `DEV_PAPER_ALPACA_API_SECRET` / `DEV_PAPER_BROKER_ACCOUNT_ID` (read only when `ENVIRONMENT=local`, all three required together); production keeps `PAPER_ALPACA_API_KEY` / `PAPER_ALPACA_API_SECRET` / `PAPER_BROKER_ACCOUNT_ID`. The legacy `ALPACA_API_KEY` / `ALPACA_API_SECRET` pair is market-data only and never places or reconciles orders.
- Switching a database to a different paper account goes through `POST /stock-paper/account-transition` (admin). It archives the old ledger with an audit event and never resets it; a fully broker-imported book may be handed off, but any open order or position that this database reserved itself blocks the transition.

## Product

_Describe the high-level user-facing capabilities of this app once they exist._

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
