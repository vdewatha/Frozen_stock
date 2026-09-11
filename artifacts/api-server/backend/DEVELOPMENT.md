# Local preview authentication

The API always requires four distinct, random role keys of at least 32
characters: `AUTH_VIEWER_KEY`, `AUTH_RESEARCHER_KEY`, `AUTH_OPERATOR_KEY`,
and `AUTH_ADMIN_KEY`. This applies to local preview and production.

Store these values only in Replit Secrets. Never put them in browser
environment variables, source files, logs, or documentation. Live trading
remains disabled by default.