---
name: Production Clerk gateway
description: Security boundary for browser authentication in the published control room.
---

Production browser authentication uses Replit-managed Clerk through a Node
gateway. The gateway validates Clerk's cookie, strips caller-supplied internal
identity headers, and forwards a short-lived HMAC-signed identity to the
loopback-only FastAPI process. New Clerk users default to `viewer`; elevated
roles require an explicit server-side Clerk user-ID mapping.

**Why:** FastAPI's earlier production identity mode required an external token
issuer that did not exist, while directly trusting browser identity or granting
the first user an elevated role would weaken the paper/live safety boundary.

**How to apply:** Keep Clerk proxying and cookie validation in the public
gateway, keep FastAPI inaccessible from the public port, and never derive an
elevated role from client claims. Signing in must not grant trading authority.

Keep preview-only role-key mode out of published bundles even when shared
development variables are inherited. Do not assume hosting sets `NODE_ENV`
for a custom gateway.

**Why:** A shared local-paper auth flag hid the production Clerk sign-in UI,
while the canonical Clerk proxy requires a production process environment.
Managed Clerk keys were present; replacing credentials would not address this
configuration mismatch.

**How to apply:** Enforce the authentication environment at the production
build/start boundary and test against inherited preview settings. Preserve
local preview access and require explicit production user-ID role mappings.