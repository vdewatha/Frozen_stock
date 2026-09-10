# API authentication

Set distinct server-only `AUTH_VIEWER_KEY`, `AUTH_RESEARCHER_KEY`,
`AUTH_OPERATOR_KEY`, and `AUTH_ADMIN_KEY` secrets. Leave unused roles empty.
Each configured key must contain at least 32 characters; generate random keys
with `openssl rand -hex 32`. Missing, short, or duplicate keys block every API
request except `GET /health`. Never put keys into NEXT_PUBLIC variables.

In Render, configure the backend AUTH_* secrets and set CORS_ORIGINS to a JSON
array containing the exact HTTPS frontend origin, such as
`["https://your-frontend.onrender.com"]`. The frontend accepts Render's bare API
hostname and normalizes it to HTTPS. Missing secrets intentionally produce 503
until configured. The manual broker-order endpoint returns 409; paper orders
must use the strategy signal flow with readiness and risk checks.

Enter the appropriate key in the frontend sign-in screen. It lives only in
JavaScript memory. Reload or sign-out clears it and unmounts the dashboard.
Bearer tokens are sent in Authorization, not cookies, so cookie CSRF does not
apply. Deploy both UI and API behind HTTPS; XSS can access in-memory tokens,
so avoid untrusted scripts and rotate credentials after exposure. API docs and
OpenAPI endpoints are disabled. CORS permits configured origins only.

Viewer can access explicitly classified reads. Researcher can train and import
data. Operator can submit paper signals and stop strategies. Admin is required
for risk changes, resume, kill-switch disable, promotions, and unclassified
routes. GET routes that may refresh/persist data require research or admin.
New routes default to admin until classified in `core/security.py`.

Structured `trading.security` logs include role principal, generated request ID,
policy reason, required role and HTTP result. Bodies, URLs, query strings, and
credentials are excluded. Configure log collection and retention in deployment.
The limiter allows 120 requests/minute per role principal (unauthenticated:
per direct client IP), per process. Use gateway-wide rate limiting for multiple
workers and protect the API from direct bypass of that gateway.

This initial scheme identifies role service principals, not individual humans.
Individual identity, credential lifecycle, and durable human-approval records
are required before multi-user live operation. Restart services when rotating
keys. No authentication-disabled local mode exists.
